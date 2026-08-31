# reelscribe — complete build plan

**To Claude Code: read this file top to bottom before writing anything. It contains the full spec, a working reference implementation, and the environment details. Section 14 is the suggested work order. Stop at the checkpoint in section 14 and ask the user to verify before continuing.**

---

## 1. What this is

A local CLI. The user registers video links (mostly Instagram reels, but anything yt-dlp supports). The tool downloads the audio, transcribes it with Whisper, and writes a markdown transcript per clip. Content is a mix of French and English.

Why it exists: reels can't be read, searched, or quoted without a transcript. The user wants to paste a link, forget about it, and find readable text later.

Design principles, in priority order:

1. **Fully local and free.** No API keys, no hosted transcription, no accounts, no telemetry. Audio never leaves the machine.
2. **Resumable.** Interrupting a run must never lose queue state or force re-downloading finished work.
3. **Per-item failure isolation.** One dead link must not stop the other nine.
4. **Boring.** No daemon, no web UI, no database. A script and a JSON file.

## 2. Environment

- Python 3.10+
- macOS or Linux. Windows nice-to-have, not required.
- Third-party Python deps: `yt-dlp`, `faster-whisper`. Nothing else.
- System binary: `ffmpeg` (`brew install ffmpeg` / `apt install ffmpeg`). Not a pip dependency — check for it at startup.

### Costs — all zero

Everything is open source and runs on the user's hardware. Whisper model weights download once and cache in `~/.cache/huggingface/`; after that the tool works offline. Sizes: `small` ~500MB, `medium` ~1.5GB, `large-v3` ~3GB. `faster-whisper` is used rather than `openai-whisper` because it's roughly 4x faster at the same accuracy.

Rough speed for a 60-second clip on a modern laptop CPU with `medium`: 20–40 seconds. Apple Silicon and NVIDIA GPUs are substantially faster.

## 3. Deliverables

```
reelscribe/
  pyproject.toml           # installable, exposes a `reelscribe` entry point
  README.md                # install, usage, troubleshooting table, cron example
  src/reelscribe/
    __init__.py
    cli.py                 # argparse command dispatch, exit codes
    config.py              # env var resolution + defaults + dependency checks
    state.py               # queue persistence, URL normalization
    fetch.py               # yt-dlp wrappers
    transcribe.py          # faster-whisper wrapper
    render.py              # markdown output
  tests/
    test_state.py
    test_render.py
    test_fetch.py
    test_transcribe.py
    test_cli.py
    fixtures/              # sample yt-dlp JSON, fake segment objects
  scripts/
    smoke_test.sh          # hits one real public URL; NOT part of CI
```

## 4. Data layout

Root is `~/reelscribe/`, overridable with `REELSCRIBE_HOME`.

```
~/reelscribe/
  state.json               # the queue + status of every registered link
  audio/                   # extracted mp3s
  transcripts/             # output markdown
  run.log                  # append-only, written when run via cron
```

`state.json` schema:

```json
{
  "version": 1,
  "items": {
    "<normalized_url>": {
      "status": "pending | done | failed",
      "added": "ISO8601",
      "completed": "ISO8601",
      "title": "string",
      "language": "fr",
      "transcript": "path",
      "error": "string",
      "attempts": 2
    }
  }
}
```

Writes must be atomic: write `state.json.tmp`, then `os.replace`. A crash mid-write must never leave a corrupt queue. Include `version` from the start so a future schema change can migrate rather than break.

## 5. CLI contract

| Command | Behaviour |
|---|---|
| `reelscribe add URL [URL...]` | Normalize and register. Duplicates skipped with a message, not an error. |
| `reelscribe run` | Process everything `pending` or `failed`. |
| `reelscribe run --only URL` | Process one specific link. |
| `reelscribe list` | All items with status markers. |
| `reelscribe list --failed` | Failures only, with the error text. |
| `reelscribe retry` | Reset `failed` items to `pending`. |
| `reelscribe clean` | Delete audio for `done` items. Never touch transcripts. |

**URL normalization:** strip query strings, strip trailing slash, lowercase the host. `instagram.com/reel/ABC/?igsh=xyz` and `www.instagram.com/reel/ABC` must resolve to the same item. This matters because Instagram share links always carry a tracking parameter.

**Exit codes:** `0` everything succeeded, `1` some items failed, `2` bad invocation or missing system dependency.

## 6. Configuration

All environment variables, all optional:

| Var | Default | Purpose |
|---|---|---|
| `REELSCRIBE_HOME` | `~/reelscribe` | Data root |
| `REELSCRIBE_MODEL` | `medium` | tiny / base / small / medium / large-v3 |
| `REELSCRIBE_LANG` | unset (auto) | Force `fr` or `en` |
| `REELSCRIBE_TRANSLATE` | unset | `1` adds an English rendering of non-English audio |
| `REELSCRIBE_COOKIES` | unset | Browser name for `yt-dlp --cookies-from-browser` |
| `REELSCRIBE_DEVICE` | `auto` | `cpu`, `cuda`, or `auto` |
| `REELSCRIBE_DELAY` | `3` | Seconds to wait between items |

`medium` is the default rather than `small` because French accuracy degrades noticeably below it — dropped accents, mangled liaisons. The reference implementation below still defaults to `small`; change it.

## 7. Pipeline per item

1. **Metadata** — `yt-dlp --dump-single-json`. Capture `id`, `title`, `description`, `uploader`, `duration`, `webpage_url`. The `description` field is the post caption and is often the most useful text in the whole file — never drop it.
2. **Audio** — `yt-dlp -x --audio-format mp3` to `audio/<id>-<slug>.mp3`. If the file already exists, skip the download. This is what makes runs resumable.
3. **Transcribe** — `faster-whisper`, `vad_filter=True`. Load the model **once per process**, not once per item; model loading dominates runtime otherwise.
4. **Translate** — only when `REELSCRIBE_TRANSLATE=1` and the detected language is not `en`. Second pass with `task="translate"`. Roughly doubles per-item time.
5. **Render** — markdown to `transcripts/<id>-<slug>.md`.
6. **Persist** — update `state.json` after *every* item, so an interrupted run loses at most one item's progress.

Transcript sections in order: H1 title, metadata bullet list (source, uploader, duration, language, transcribed-at), `## Caption`, `## Transcript`, `## English translation` (conditional), `## Timestamped`.

## 8. Language handling

The user's content is mixed French and English, so this is not an edge case — it's the main path.

- **Default is per-file auto-detection.** Whisper detects language itself and the result goes in the transcript header and in `state.json`.
- **Known limitation:** detection samples only the opening ~30 seconds. A clip that opens in English and switches to French will be locked to English and the French portion will come out as garbled pseudo-translation. Do not try to solve this with chunked re-detection — document it in the README and move on.
- **`REELSCRIBE_LANG`** turns detection off for batches known to be single-language. This is both faster and more accurate.
- **`REELSCRIBE_TRANSLATE=1`** produces both the original-language transcript and an English rendering in the same file. The original is always kept.

Intended usage patterns to document in the README:

```bash
# Mixed batch — auto-detect each clip
reelscribe run

# All-French batch — skip detection, bigger model
REELSCRIBE_LANG=fr REELSCRIBE_MODEL=medium reelscribe run

# French audio, English translation alongside the original
REELSCRIBE_LANG=fr REELSCRIBE_TRANSLATE=1 reelscribe run
```

## 9. Error handling

Failures are per-item and never abort the run. Catch, record to `state.json`, print, continue.

Handle these distinctly, with actionable messages:

- **`yt-dlp` or `ffmpeg` missing from PATH** — fail fast at startup with the install command for the detected platform. Exit 2. Never let this surface as a traceback.
- **Login required / private post** — detect auth-related stderr, suggest `REELSCRIBE_COOKIES=chrome`.
- **Rate limited (HTTP 429)** — this is the one failure that should *stop* the run rather than continue. Report how many items were left unprocessed. Instagram rate-limits aggressively; hammering it makes things worse.
- **Video removed, private, or geo-blocked** — mark failed with the reason, move on.
- **Audio downloaded but silent or empty** — write the transcript with an empty body and a note in the header rather than crashing.
- **Model download interrupted** — surface the real error. Do not retry in a loop.

Sleep `REELSCRIBE_DELAY` seconds between items.

> **Important:** the failure cases above are what I expect, not what I've verified. yt-dlp's exact stderr strings for auth and rate-limiting change between releases. Write the pattern-matching against output the user has actually seen — ask them to paste real errors rather than guessing at string matches.

## 10. Tests

Unit tests only. **No network calls in the test suite.**

- `state.py` — add/dedupe, URL normalization table (including the `?igsh=` case), atomic write survives a simulated crash, schema round-trip.
- `render.py` — markdown output against fixture metadata; missing caption, missing duration, zero segments, translation present and absent.
- `fetch.py` — mock `subprocess.run`; assert the cookie flag appears only when configured; assert stderr is surfaced on non-zero return; assert existing audio short-circuits the download.
- `transcribe.py` — mock the model; assert `language` and `task` pass through correctly; assert the model is instantiated only once across multiple calls.
- `cli.py` — exit code for each scenario in section 5.

`scripts/smoke_test.sh` hits one real public URL end to end. Manual only.

## 11. Acceptance criteria

- `pip install -e .` then `reelscribe add <url> && reelscribe run` produces a transcript.
- A French clip transcribes with correct accents and is tagged `fr` in the header.
- `REELSCRIBE_TRANSLATE=1` yields both sections in one file.
- Killing the process mid-run and re-running resumes without re-downloading completed audio or losing queue state.
- One dead link among five does not prevent the other four; `list --failed` explains why.
- Missing `ffmpeg` produces a clear install message, not a traceback.
- A full run works with networking disabled once the model is cached.
- `reelscribe add` on the same link twice creates one item, not two.

## 12. Non-goals

Do not build: a web UI, a database, a background daemon, speaker diarization, subtitle burn-in, video (as opposed to audio) retention, or any hosted-API fallback. Scheduling is the user's own cron or launchd — just document it:

```
0 * * * * cd ~/reelscribe && /usr/bin/python3 -m reelscribe run >> run.log 2>&1
```

## 13. Reference implementation

This is a working single-file prototype. It runs and produces correct output. Treat it as the specification of *intended behaviour*, not as finished code — it has no tests, no dependency checks, no rate-limit handling, and no resumability on the download step.

```python
#!/usr/bin/env python3
"""
reelscribe - register video links, then download + transcribe them automatically.

Usage:
    python3 reelscribe.py add "https://www.instagram.com/reel/XXXX/"
    python3 reelscribe.py run          # process everything pending
    python3 reelscribe.py list         # show status of all registered links
    python3 reelscribe.py retry        # reset failed items back to pending

Setup:
    pip install yt-dlp faster-whisper
    (ffmpeg also required: `brew install ffmpeg` / `apt install ffmpeg`)

Everything lives in ~/reelscribe/ by default. Override with REELSCRIBE_HOME.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("REELSCRIBE_HOME", Path.home() / "reelscribe"))
STATE_FILE = HOME / "state.json"
AUDIO_DIR = HOME / "audio"
TEXT_DIR = HOME / "transcripts"

# Whisper model: tiny / base / small / medium / large-v3
MODEL_SIZE = os.environ.get("REELSCRIBE_MODEL", "small")

# Pass browser cookies to yt-dlp when a site demands a login.
COOKIE_BROWSER = os.environ.get("REELSCRIBE_COOKIES", "")

# Force a language instead of auto-detecting: "fr", "en", or "" for auto.
LANGUAGE = os.environ.get("REELSCRIBE_LANG", "") or None

# Set REELSCRIBE_TRANSLATE=1 to render non-English speech as English text.
TRANSLATE = os.environ.get("REELSCRIBE_TRANSLATE", "") == "1"


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"items": {}}


def save_state(state):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def slugify(text, limit=60):
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:limit] or "untitled"


# ---------------------------------------------------------------- steps

def ytdlp_base():
    cmd = ["yt-dlp"]
    if COOKIE_BROWSER:
        cmd += ["--cookies-from-browser", COOKIE_BROWSER]
    return cmd


def fetch_metadata(url):
    """Grab title, caption/description, uploader without downloading media."""
    proc = subprocess.run(
        ytdlp_base() + ["--dump-single-json", "--no-warnings", url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:] or "yt-dlp metadata failed")
    return json.loads(proc.stdout)


def download_audio(url, out_stem):
    """Extract audio only - much faster than pulling full video."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    target = AUDIO_DIR / f"{out_stem}.%(ext)s"
    proc = subprocess.run(
        ytdlp_base() + [
            "-x", "--audio-format", "mp3", "--audio-quality", "5",
            "--no-warnings", "-o", str(target), url,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:] or "yt-dlp download failed")
    mp3 = AUDIO_DIR / f"{out_stem}.mp3"
    if not mp3.exists():
        raise RuntimeError("audio file not produced")
    return mp3


_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_SIZE, device="auto", compute_type="int8")
    return _model


def transcribe(audio_path, task="transcribe"):
    """Returns (plain_text, [(start, end, text), ...], detected_language)."""
    segments, info = get_model().transcribe(
        str(audio_path), vad_filter=True, language=LANGUAGE, task=task
    )
    rows, parts = [], []
    for seg in segments:
        rows.append((seg.start, seg.end, seg.text.strip()))
        parts.append(seg.text.strip())
    return " ".join(parts), rows, info.language


def translate_to_english(audio_path):
    """Second pass rendering the audio as English. Returns text or None."""
    text, _rows, _lang = transcribe(audio_path, task="translate")
    return text or None


def timestamp(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def write_transcript(stem, meta, text, rows, lang, english=None):
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEXT_DIR / f"{stem}.md"
    caption = (meta.get("description") or "").strip()
    lines = [
        f"# {meta.get('title') or stem}",
        "",
        f"- Source: {meta.get('webpage_url')}",
        f"- Uploader: {meta.get('uploader') or 'unknown'}",
        f"- Duration: {timestamp(meta.get('duration') or 0)}",
        f"- Language: {lang}",
        f"- Transcribed: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    if caption:
        lines += ["## Caption", "", caption, ""]
    lines += ["## Transcript", "", text, ""]
    if english:
        lines += ["## English translation", "", english, ""]
    lines += ["## Timestamped", ""]
    for start, _end, seg_text in rows:
        lines.append(f"[{timestamp(start)}] {seg_text}")
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- commands

def cmd_add(urls):
    state = load_state()
    added = 0
    for url in urls:
        url = url.split("?")[0].rstrip("/") + "/"
        if url in state["items"]:
            print(f"already registered: {url}")
            continue
        state["items"][url] = {"status": "pending", "added": datetime.now().isoformat()}
        added += 1
        print(f"queued: {url}")
    save_state(state)
    print(f"\n{added} new link(s). Run `reelscribe run` to process.")


def cmd_run():
    state = load_state()
    pending = [u for u, v in state["items"].items() if v["status"] in ("pending", "failed")]
    if not pending:
        print("Nothing pending.")
        return
    print(f"Processing {len(pending)} item(s) with model '{MODEL_SIZE}'...\n")
    for url in pending:
        entry = state["items"][url]
        try:
            print(f"-> {url}")
            meta = fetch_metadata(url)
            stem = f"{meta.get('id', 'clip')}-{slugify(meta.get('title') or '')}"
            audio = download_audio(url, stem)
            text, rows, lang = transcribe(audio)
            english = None
            if TRANSLATE and lang != "en":
                english = translate_to_english(audio)
            path = write_transcript(stem, meta, text, rows, lang, english)
            entry.update(status="done", transcript=str(path), title=meta.get("title"),
                         completed=datetime.now().isoformat())
            entry.pop("error", None)
            print(f"   done -> {path}\n")
        except Exception as exc:  # keep going through the rest of the queue
            entry.update(status="failed", error=str(exc))
            print(f"   FAILED: {exc}\n")
        save_state(state)
    done = sum(1 for v in state["items"].values() if v["status"] == "done")
    print(f"Finished. {done} transcript(s) total in {TEXT_DIR}")


def cmd_list():
    state = load_state()
    if not state["items"]:
        print("Nothing registered yet.")
        return
    for url, v in state["items"].items():
        mark = {"done": "[x]", "pending": "[ ]", "failed": "[!]"}[v["status"]]
        print(f"{mark} {url}")
        if v.get("title"):
            print(f"      {v['title']}")
        if v.get("error"):
            print(f"      error: {v['error'][:120]}")


def cmd_retry():
    state = load_state()
    n = 0
    for v in state["items"].values():
        if v["status"] == "failed":
            v["status"] = "pending"
            n += 1
    save_state(state)
    print(f"{n} item(s) reset to pending.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "add" and args:
        cmd_add(args)
    elif cmd == "run":
        cmd_run()
    elif cmd == "list":
        cmd_list()
    elif cmd == "retry":
        cmd_retry()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
```

### What the prototype is missing

Everything in section 9, plus: no `--only` or `clean` commands, no `attempts` counter, no `version` in the state schema, no delay between items, no `REELSCRIBE_DEVICE`, no dependency check at startup, model size defaults to `small` instead of `medium`, and `cmd_run` returns nothing so there are no meaningful exit codes.

## 14. Work order

1. Scaffold the package. `config.py` (including the `ffmpeg`/`yt-dlp` presence check) and `state.py`, with their tests.
2. `fetch.py` with mocked tests.

   **→ CHECKPOINT. Stop here.** Ask the user to run a real link end to end and paste the output. `fetch.py` is the riskiest module because Instagram's actual response shapes and error strings can't be verified without a live call. Do not build section 9's error handling against guessed stderr strings — build it against what the user actually sees.

3. `transcribe.py` and `render.py`, with tests.
4. `cli.py`, wiring, exit codes, the remaining commands.
5. Error handling pass — work section 9 case by case, using real observed errors.
6. README: install, the three language usage patterns from section 8, the cron line from section 12, and a troubleshooting table.

## 15. Troubleshooting content for the README

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg not found` | System binary missing | `brew install ffmpeg` or `apt install ffmpeg` |
| yt-dlp reports login required | Private account, or Instagram demanding a session | `REELSCRIBE_COOKIES=chrome` |
| Transcript is garbled | Model too small for the accent, or music over speech | `REELSCRIBE_MODEL=medium` or `large-v3` |
| French comes out as bad English | Language locked to `en` during detection | `REELSCRIBE_LANG=fr` |
| First run hangs for minutes | Model weights downloading | Expected once; ~1.5GB for `medium` |
| Several links fail at once | Rate limited | Wait, then `reelscribe retry`. Keep batches small. |

## 16. Scope and use

This is a personal tool for reading material the user already has access to. Downloaded content belongs to its creators — it's for private reading, not redistribution or republication. Private accounts and paywalled content are out of scope; don't build cookie handling into anything beyond the user's own logged-in session for content they can already see.
