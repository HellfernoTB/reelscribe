"""The per-item pipeline: metadata -> audio -> transcribe -> translate -> render.

Shared by ``reelscribe run`` (cli) and ``reelscribe serve`` (serve).
"""

from __future__ import annotations

from .config import Config
from .fetch import download_audio, fetch_metadata
from .render import build_markdown, make_stem, write_transcript
from .state import now_iso
from .transcribe import Transcriber


def process_item(cfg: Config, transcriber: Transcriber, url: str) -> dict:
    meta = fetch_metadata(url, cookies=cfg.cookies)
    stem = make_stem(meta)
    audio = download_audio(url, stem, cfg.audio_dir, cookies=cfg.cookies)

    result = transcriber.run(audio)
    note = "no speech detected in audio" if result.is_empty else None

    english = None
    if cfg.translate and result.language != "en":
        english = transcriber.translate_to_english(audio)

    markdown = build_markdown(
        source=url, meta=meta, result=result, english=english,
        transcribed_at=now_iso(), note=note,
    )
    path = write_transcript(cfg.transcript_dir, stem, markdown)
    return {
        "transcript": str(path),
        "audio": str(audio),
        "title": meta.get("title"),
        "language": result.language,
    }
