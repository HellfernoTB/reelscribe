"""The per-item pipeline: metadata -> audio -> transcribe -> translate -> render.

Shared by ``reelscribe run`` (cli) and ``reelscribe serve`` (serve).
``process_item`` returns the structured fields for ``State.mark_done`` and, when
``REELSCRIBE_WRITE_MARKDOWN`` is on, also writes the Markdown export.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config
from .fetch import download_audio, fetch_metadata
from .render import build_markdown, build_record, make_stem, write_transcript
from .transcribe import Transcriber


def _relative_to_home(cfg: Config, path: Path) -> str:
    try:
        return str(Path(path).relative_to(cfg.home))
    except ValueError:
        return str(path)


def process_item(cfg: Config, transcriber: Transcriber, url: str) -> dict:
    meta = fetch_metadata(url, cookies=cfg.cookies)
    stem = make_stem(meta)
    audio = download_audio(url, stem, cfg.audio_dir, cookies=cfg.cookies)

    result = transcriber.run(audio)
    note = "no speech detected in audio" if result.is_empty else None

    english = None
    if cfg.translate and result.language != "en":
        english = transcriber.translate_to_english(audio)

    moment = datetime.now().astimezone()
    record = build_record(
        meta=meta, result=result, english=english, note=note,
        model=cfg.model, transcribed_at=moment,
    )
    record["audio_path"] = _relative_to_home(cfg, audio)
    record["transcript_file"] = None

    if cfg.write_markdown:
        markdown = build_markdown(
            source=url, meta=meta, result=result, english=english,
            transcribed_at=moment.replace(tzinfo=None).isoformat(timespec="seconds"),
            note=note,
        )
        record["transcript_file"] = write_transcript(cfg.transcript_dir, stem, markdown).name
    return record
