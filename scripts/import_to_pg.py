#!/usr/bin/env python3
"""One-shot import of the file-based reelscribe data into PostgreSQL.

    REELSCRIBE_DATABASE_URL=... REELSCRIBE_HOME=~/Homelab/Instagram-recap/data \\
        scripts/import_to_pg.py [--state data/state.json] \\
        [--markdown-dir Traités --markdown-dir data/transcripts] [--replace]

- ``state.json`` items become ``reel`` rows. Naive timestamps are read as
  Europe/Paris. ``audio_path`` is stored relative to ``REELSCRIBE_HOME``.
- Every ``*.md`` export (reelscribe's own format, see ``render.py``) is parsed
  into ``reel`` fields, ``transcript`` and ``transcript_segment`` (mm:ss
  precision, so ``end_s`` is NULL). The first directory listed wins when the
  same reel appears twice; a file with no ``state.json`` item creates a ``done`` reel.
- Refuses to run when ``reel`` is not empty unless ``--replace``.
- The source files are only read, never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from reelscribe.render import NO_SPEECH
from reelscribe.state import State, normalize_url, shortcode

PARIS = ZoneInfo("Europe/Paris")
_FIELD = re.compile(r"^- ([A-Za-z ]+): (.*)$")
_SEGMENT = re.compile(r"^\[(\d+):(\d{2})\] (.*)$")
_HEADINGS = {"Caption", "Transcript", "English translation", "Timestamped"}


def parse_time(raw: str | None) -> datetime | None:
    """ISO8601; a naive value is Europe/Paris local time."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return moment.replace(tzinfo=PARIS) if moment.tzinfo is None else moment


def _mmss(raw: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d{2})", raw.strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def parse_markdown(text: str) -> dict:
    """Parse one reelscribe export. Raises ``ValueError`` when it is not one."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("no title line")
    fields: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines[1:]:
        if line.startswith("## ") and line[3:].strip() in _HEADINGS:
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif m := _FIELD.match(line):
            fields[m.group(1).lower()] = m.group(2).strip()
    body = {k: "\n".join(v).strip() for k, v in sections.items()}
    if "Transcript" not in body or "source" not in fields:
        raise ValueError("no Source line or Transcript section")

    def known(key):
        return None if fields.get(key) in (None, "", "unknown") else fields[key]

    segments = []
    for line in body.get("Timestamped", "").splitlines():
        if m := _SEGMENT.match(line):
            segments.append(
                {"start": int(m.group(1)) * 60 + int(m.group(2)), "end": None, "text": m.group(3)}
            )
    return {
        "url": normalize_url(fields["source"]),
        "title": lines[0][2:].strip() or None,
        "uploader": known("uploader"),
        "duration_s": _mmss(fields.get("duration", "")),
        "language": known("language"),
        "transcribed_at": parse_time(fields.get("transcribed")),
        "note": known("note"),
        "caption": body.get("Caption") or None,
        "text": "" if body["Transcript"] == NO_SPEECH else body["Transcript"],
        "english_text": body.get("English translation") or None,
        "segments": segments,
    }


def relative_audio(raw: str | None, home: Path) -> str | None:
    if not raw:
        return None
    path = Path(raw)
    try:
        return str(path.relative_to(home))
    except ValueError:
        return str(Path("audio") / path.name)  # reelscribe always stores home/audio/<stem>.mp3


def import_all(conn: psycopg.Connection, *, state_file: Path | None, markdown_dirs: list[Path],
               home: Path, replace: bool = False) -> dict:
    """Run the import inside one transaction. Returns the counts."""
    with conn.transaction():
        if conn.execute("SELECT count(*) FROM reel").fetchone()[0]:
            if not replace:
                raise SystemExit("reel table is not empty; use --replace to wipe and reimport")
            conn.execute("DELETE FROM reel")  # cascades to transcript + segments

        reels: dict[str, int] = {}
        if state_file and state_file.exists():
            items = json.loads(state_file.read_text())["items"]
            for raw_url, item in items.items():
                url = normalize_url(raw_url)
                status = item.get("status")
                status = status if status in ("done", "failed") else "pending"
                transcript = item.get("transcript")
                reels[url] = conn.execute(
                    "INSERT INTO reel (url, shortcode, status, added_at, completed_at, attempts, "
                    "last_error, title, language, audio_path, transcript_file) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (url, shortcode(url), status,
                     parse_time(item.get("added")) or datetime.now(PARIS),
                     parse_time(item.get("completed")), item.get("attempts") or 0,
                     item.get("error"), item.get("title"), item.get("language"),
                     relative_audio(item.get("audio"), home),
                     Path(transcript).name if transcript else None),
                ).fetchone()[0]

        seen: set[str] = set()
        for folder in markdown_dirs:
            for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
                try:
                    rec = parse_markdown(path.read_text(encoding="utf-8", errors="replace"))
                except ValueError as exc:
                    print(f"skip {path.name}: {exc}", file=sys.stderr)
                    continue
                url = rec["url"]
                if url in seen:
                    continue
                seen.add(url)
                when = rec["transcribed_at"] or datetime.now(PARIS)
                if url in reels:
                    rid = reels[url]
                    conn.execute(
                        "UPDATE reel SET status = 'done', uploader = %s, duration_s = %s, "
                        "caption = %s, title = COALESCE(%s, title), "
                        "language = COALESCE(%s, language), "
                        "completed_at = COALESCE(completed_at, %s), "
                        "transcript_file = COALESCE(transcript_file, %s) WHERE id = %s",
                        (rec["uploader"], rec["duration_s"], rec["caption"], rec["title"],
                         rec["language"], when, path.name, rid),
                    )
                else:
                    rid = reels[url] = conn.execute(
                        "INSERT INTO reel (url, shortcode, status, added_at, completed_at, title, "
                        "uploader, duration_s, caption, language, transcript_file) "
                        "VALUES (%s,%s,'done',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (url, shortcode(url), when, when, rec["title"], rec["uploader"],
                         rec["duration_s"], rec["caption"], rec["language"], path.name),
                    ).fetchone()[0]
                conn.execute(
                    "INSERT INTO transcript (reel_id, text, english_text, note, model, transcribed_at) "
                    "VALUES (%s,%s,%s,%s,NULL,%s)",
                    (rid, rec["text"], rec["english_text"], rec["note"], when),
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO transcript_segment (reel_id, idx, start_s, end_s, text) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        [(rid, i, s["start"], s["end"], s["text"]) for i, s in enumerate(rec["segments"])],
                    )

        counts = {
            "reels": conn.execute("SELECT count(*) FROM reel").fetchone()[0],
            "done": conn.execute("SELECT count(*) FROM reel WHERE status='done'").fetchone()[0],
            "failed": conn.execute("SELECT count(*) FROM reel WHERE status='failed'").fetchone()[0],
            "transcripts": conn.execute("SELECT count(*) FROM transcript").fetchone()[0],
            "segments": conn.execute("SELECT count(*) FROM transcript_segment").fetchone()[0],
        }
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.environ.get("REELSCRIBE_HOME") or "~/reelscribe").expanduser()
    p.add_argument("--state", type=Path, default=home / "state.json")
    p.add_argument("--markdown-dir", type=Path, action="append", dest="markdown_dirs",
                   help="folder of exported .md files (repeatable; default: REELSCRIBE_HOME/transcripts)")
    p.add_argument("--replace", action="store_true", help="wipe existing reel data first")
    args = p.parse_args(argv)

    url = os.environ.get("REELSCRIBE_DATABASE_URL")
    if not url:
        print("REELSCRIBE_DATABASE_URL must be set", file=sys.stderr)
        return 2
    dirs = args.markdown_dirs or [home / "transcripts"]
    State.open(url)  # applies migrations
    with psycopg.connect(url) as conn:
        counts = import_all(conn, state_file=args.state, markdown_dirs=dirs, home=home,
                            replace=args.replace)
    print("imported: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
