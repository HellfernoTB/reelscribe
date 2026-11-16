# reelscribe

Register video links (Instagram reels, or anything yt-dlp supports), then
download the audio and transcribe it locally with Whisper. One markdown
transcript per clip, including the post caption.

Fully local and free: no API keys, no accounts, no telemetry. Audio never leaves
the machine. After the Whisper model downloads once it works offline.

## Install

```bash
pip install -e .
```

System requirements:

- Python 3.10+
- `ffmpeg` on PATH — `brew install ffmpeg` (macOS) / `sudo apt install ffmpeg` (Linux)
- `yt-dlp` on PATH — installed automatically as a dependency; `pip install -U yt-dlp` to update

The first `run` downloads the Whisper model to `~/.cache/huggingface/`
(`small` ~500 MB, `medium` ~1.5 GB, `large-v3` ~3 GB). Expect that first run to
take a few minutes. Everything after is offline.

## Usage

```bash
reelscribe add "https://www.instagram.com/reel/XXXX/"   # register a link
reelscribe run                                          # process everything pending
reelscribe run --only "https://.../reel/XXXX/"          # process one link
reelscribe list                                         # status of every link
reelscribe list --failed                                # failures only, with the error
reelscribe retry                                        # reset failed items to pending
reelscribe clean                                        # delete audio for done items (keeps transcripts)
reelscribe serve                                        # HTTP endpoint for phone-share ingestion (see below)
```

Runs are resumable: interrupt `run` and start it again — finished transcripts
and already-downloaded audio are not redone, and queue state is never lost.
One dead link never stops the others.

Exit codes: `0` all succeeded · `1` some items failed · `2` bad invocation or a
missing system dependency.

## Data layout

Everything lives under `~/reelscribe/` by default, overridable with
`REELSCRIBE_HOME`:

```
~/reelscribe/
  audio/          # extracted mp3s
  transcripts/    # Markdown export (REELSCRIBE_WRITE_MARKDOWN=1, default)
  run.log         # only when you redirect cron output here
```

The queue and the transcripts live in PostgreSQL (`REELSCRIBE_DATABASE_URL`,
required; schema `instagram_recap` via the role's `search_path`). Tables:
`reel` (status, attempts, caption, uploader, yt-dlp metadata, relative
`audio_path`), `transcript` and `transcript_segment`. They are created and
migrated automatically at startup (`schema_migrations`). The Markdown files are
a human-readable export, not the source of truth. Set
`REELSCRIBE_WRITE_MARKDOWN=0` to stop writing them.

Failed items are retried automatically at most 5 times; `reelscribe retry`
resets them explicitly. Rows claimed by a crashed run (`running`) go back to
`pending` when the next worker (`run` or `serve`) takes the lock.

To migrate an old file-based install, run `scripts/import_to_pg.py` once
(`--replace` to redo it).

## Configuration

All environment variables are optional except `REELSCRIBE_DATABASE_URL`:

| Var | Default | Purpose |
|---|---|---|
| `REELSCRIBE_DATABASE_URL` | unset (**required**) | PostgreSQL URL for the queue and transcripts; keep it in a mode-600 env file |
| `REELSCRIBE_WRITE_MARKDOWN` | `1` | `0` disables the Markdown export |
| `REELSCRIBE_HOME` | `~/reelscribe` | Data root (audio, Markdown export) |
| `REELSCRIBE_MODEL` | `medium` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `REELSCRIBE_LANG` | auto | Force `fr` or `en`, skipping detection |
| `REELSCRIBE_TRANSLATE` | unset | `1` adds an English rendering of non-English audio |
| `REELSCRIBE_COOKIES` | unset | Browser name for `yt-dlp --cookies-from-browser` |
| `REELSCRIBE_DEVICE` | `auto` | `cpu`, `cuda`, or `auto` (auto falls back to CPU if CUDA is unavailable) |
| `REELSCRIBE_DELAY` | `3` | Seconds to wait between items |
| `REELSCRIBE_TOKEN` | unset | Shared secret for `reelscribe serve`; the endpoint refuses to start without it |
| `REELSCRIBE_SERVE_ADDR` | `0.0.0.0:8765` | `HOST:PORT` (or just `PORT`) for `reelscribe serve` |

`medium` is the default because French accuracy drops noticeably below it —
dropped accents, mangled liaisons.

## Language handling

The content is a mix of French and English, so language handling is the main
path, not an edge case.

```bash
# Mixed batch — auto-detect each clip
reelscribe run

# All-French batch — skip detection, use a bigger model
REELSCRIBE_LANG=fr REELSCRIBE_MODEL=medium reelscribe run

# French audio with an English translation alongside the original
REELSCRIBE_LANG=fr REELSCRIBE_TRANSLATE=1 reelscribe run
```

**Known limitation:** Whisper detects language from only the opening ~30 seconds.
A clip that starts in English and switches to French stays locked to English, and
the French part comes out as garbled pseudo-translation. Use `REELSCRIBE_LANG`
for such clips.

## Scheduling

By default reelscribe has no daemon. Use cron or launchd:

```
0 * * * * cd ~/reelscribe && /usr/bin/python3 -m reelscribe run >> run.log 2>&1
```

## Automatic ingestion — share a reel from your phone

`reelscribe serve` is an **opt-in** HTTP endpoint: share a reel from Instagram to
a phone shortcut, and the homelab box downloads and transcribes it within a
minute. This is the one mode where reelscribe runs as a long-lived service — the
core CLI stays daemon-free.

The worker keeps the Whisper model loaded between reels, so each share is only as
slow as the transcription itself (no per-item model reload like cron).

### 1. Put the box on Tailscale (free)

The [Personal plan](https://tailscale.com/pricing) is free forever, unlimited
devices, personal use.

```bash
# on the box
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status          # note the box's name, e.g. "homelab"
```

Install the Tailscale app on your phone and sign in with the same account.
MagicDNS (on by default) makes the box reachable at `http://homelab:8765`.

### 2. Start the endpoint

```bash
DATA=~/reelscribe
mkdir -p "$DATA" ~/.config/systemd/user

printf 'REELSCRIBE_TOKEN=%s\n' "$(openssl rand -hex 16)" > "$DATA/serve.env"
printf 'REELSCRIBE_HOME=%s\n' "$DATA"                    >> "$DATA/serve.env"
echo   'REELSCRIBE_MODEL=medium'                         >> "$DATA/serve.env"
chmod 600 "$DATA/serve.env"

cp scripts/reelscribe-serve.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now reelscribe-serve

curl http://localhost:8765/health        # -> {"status": "ok"}
cat "$DATA/serve.env"                     # copy the token for step 3
```

Logs: `journalctl --user -u reelscribe-serve -f`

For interactive use in the same shell, point the CLI at the same data root:

```bash
echo 'export REELSCRIBE_HOME=~/reelscribe' >> ~/.bashrc
```

### 3. Phone shortcut

**iOS (Shortcuts app):**

1. New shortcut → rename it "Transcribe reel".
2. Shortcut settings → **Show in Share Sheet**; accepted input **URLs only**.
3. Add one action: **Get Contents of URL**
   - URL: `http://homelab:8765/add`
   - Method: **GET**
   - Query parameters:
     - `url` → Shortcut Input
     - `token` → *(paste the token)*
4. Share a reel → scroll to **Transcribe reel**.

**Android:** the *HTTP Shortcuts* app — add a GET request to the same URL with
`url` and `token` parameters, and register it as a share target.

Test from anywhere on the tailnet:

```bash
curl "http://homelab:8765/add?token=YOURTOKEN&url=https://www.instagram.com/reel/XXXX/"
# -> {"queued": "https://instagram.com/reel/XXXX"}
```

### Endpoint reference

| Route | |
|---|---|
| `GET /health` | `{"status":"ok"}`, no token |
| `GET/POST /add?url=…&token=…` | `202 {"queued":…}` new · `200 {"skipped":…}` already known · `400` missing url · `401` bad token · `503` database unreachable (retry later) |

### Caveats

- **The box must be powered on** when you share — an offline box means that share
  is lost (nothing is queued on the phone).
- The token is the only thing protecting the endpoint. Keep Tailscale ACLs tight
  and don't expose port 8765 to the public internet.
- Pick **one** ingestion method. Running `serve` and a `reelscribe run` cron at
  the same time is refused: each worker holds a PostgreSQL advisory lock for its lifetime, so
  `reelscribe run` exits with "another reelscribe worker is running" while `serve`
  is up (and `serve` waits and retries while a `run` is active; `/add` still queues).
  Only the lock holder claims rows or resets stale `running` ones, so a reel is
  never transcribed twice.
- Private / login-walled reels still need `REELSCRIBE_COOKIES` in `serve.env`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg not found` | System binary missing | `brew install ffmpeg` / `sudo apt install ffmpeg` |
| yt-dlp reports login required | Private account, or Instagram wants a session | `REELSCRIBE_COOKIES=chrome` |
| Transcript is garbled | Model too small for the accent, or music over speech | `REELSCRIBE_MODEL=medium` or `large-v3` |
| French comes out as bad English | Language locked to `en` during detection | `REELSCRIBE_LANG=fr` |
| First run hangs for minutes | Model weights downloading | Expected once; ~1.5 GB for `medium` |
| Several links fail at once | Rate limited | Wait, then `reelscribe retry`. Keep batches small. |

## Scope

A personal tool for reading material you already have access to. Downloaded
content belongs to its creators — for private reading, not redistribution.
Private and paywalled content is out of scope.

## Development

```bash
pip install -e '.[dev]'
pytest                 # unit tests only, no network
scripts/smoke_test.sh  # one real public URL end to end — manual, not CI
```
