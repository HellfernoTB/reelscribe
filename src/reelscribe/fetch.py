"""yt-dlp wrappers: metadata dump and audio-only download.

Error classification (auth / rate-limit / unavailable) is deliberately *not*
implemented here yet. Per BUILD.md section 9, the stderr patterns must be written
against errors the user has actually seen, not guessed. For now every non-zero
exit surfaces the raw yt-dlp stderr via ``FetchError``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

STDERR_TAIL = 600


class FetchError(Exception):
    """yt-dlp failed. The message carries the tail of its stderr."""


def _ytdlp_base(cookies: str | None) -> list[str]:
    cmd = ["yt-dlp", "--no-warnings"]
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    return cmd


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _fail(proc: subprocess.CompletedProcess, fallback: str) -> FetchError:
    stderr = (proc.stderr or "").strip()
    return FetchError(stderr[-STDERR_TAIL:] or fallback)


def fetch_metadata(url: str, cookies: str | None = None) -> dict:
    """Return the yt-dlp JSON blob for ``url`` without downloading media."""
    proc = _run(_ytdlp_base(cookies) + ["--dump-single-json", url])
    if proc.returncode != 0:
        raise _fail(proc, "yt-dlp metadata failed")
    return json.loads(proc.stdout)


def download_audio(
    url: str,
    out_stem: str,
    audio_dir,
    cookies: str | None = None,
) -> Path:
    """Extract audio to ``<audio_dir>/<out_stem>.mp3``.

    If that file already exists the download is skipped entirely — this is what
    makes ``reelscribe run`` resumable.
    """
    audio_dir = Path(audio_dir)
    mp3 = audio_dir / f"{out_stem}.mp3"
    if mp3.exists():
        return mp3

    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / f"{out_stem}.%(ext)s"
    proc = _run(
        _ytdlp_base(cookies)
        + ["-x", "--audio-format", "mp3", "--audio-quality", "5", "-o", str(target), url]
    )
    if proc.returncode != 0:
        raise _fail(proc, "yt-dlp download failed")
    if not mp3.exists():
        raise FetchError("yt-dlp reported success but no mp3 was produced")
    return mp3
