"""Markdown transcript output.

Section order (BUILD.md section 7): H1 title, metadata bullets, ``## Caption``,
``## Transcript``, ``## English translation`` (conditional), ``## Timestamped``.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

NO_SPEECH = "_(no speech detected)_"


def slugify(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:limit].strip("-") or "untitled"


def make_stem(meta: dict) -> str:
    return f"{meta.get('id') or 'clip'}-{slugify(meta.get('title') or '')}"


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds or 0), 60)
    return f"{m:02d}:{s:02d}"


def _duration_str(duration) -> str:
    return _fmt_ts(duration) if duration else "unknown"


def build_markdown(
    *,
    source: str,
    meta: dict,
    result,
    english: str | None = None,
    transcribed_at: str | None = None,
    note: str | None = None,
) -> str:
    transcribed_at = transcribed_at or datetime.now().isoformat(timespec="seconds")
    title = (meta.get("title") or "").strip() or source
    caption = (meta.get("description") or "").strip()

    lines = [f"# {title}", ""]
    lines += [
        f"- Source: {source}",
        f"- Uploader: {meta.get('uploader') or 'unknown'}",
        f"- Duration: {_duration_str(meta.get('duration'))}",
        f"- Language: {result.language or 'unknown'}",
        f"- Transcribed: {transcribed_at}",
    ]
    if note:
        lines.append(f"- Note: {note}")
    lines.append("")

    if caption:
        lines += ["## Caption", "", caption, ""]

    lines += ["## Transcript", "", result.text or NO_SPEECH, ""]

    if english:
        lines += ["## English translation", "", english.strip(), ""]

    lines += ["## Timestamped", ""]
    if result.rows:
        lines += [f"[{_fmt_ts(start)}] {text}" for start, _end, text in result.rows]
    else:
        lines.append(NO_SPEECH)

    return "\n".join(lines) + "\n"


def build_record(
    *,
    meta: dict,
    result,
    english: str | None = None,
    note: str | None = None,
    model: str | None = None,
    transcribed_at: datetime,
) -> dict:
    """Structured counterpart of ``build_markdown``: the fields stored in PostgreSQL.

    Keys match ``State.mark_done`` (minus the file/audio paths added by the pipeline).
    ``segments`` carry float start/end seconds.
    """
    duration = meta.get("duration")
    return {
        "title": (meta.get("title") or "").strip() or None,
        "uploader": meta.get("uploader") or None,
        "duration_s": float(duration) if duration else None,
        "caption": (meta.get("description") or "").strip() or None,
        "language": result.language,
        "text": result.text or "",
        "english_text": english.strip() if english else None,
        "note": note,
        "model": model,
        "transcribed_at": transcribed_at,
        "segments": [
            {"start": float(start), "end": float(end), "text": text}
            for start, end, text in result.rows
        ],
        "ytdlp_meta": meta,
    }


def write_transcript(transcript_dir, stem: str, markdown: str) -> Path:
    path = Path(transcript_dir) / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return path
