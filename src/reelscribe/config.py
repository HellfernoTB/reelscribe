"""Environment-variable resolution, defaults, and startup dependency checks."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

DEFAULT_HOME = "~/reelscribe"
DEFAULT_MODEL = "medium"
DEFAULT_DEVICE = "auto"
DEFAULT_DELAY = 3.0
DEFAULT_SERVE_ADDR = "0.0.0.0:8765"

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
VALID_DEVICES = {"cpu", "cuda", "auto"}


class ConfigError(Exception):
    """Raised for bad configuration or a missing system dependency (exit code 2)."""


def _install_hint(binary: str) -> str:
    mac = sys.platform == "darwin"
    if binary == "ffmpeg":
        return "brew install ffmpeg" if mac else "sudo apt install ffmpeg"
    if binary == "yt-dlp":
        return "brew install yt-dlp" if mac else "pip install -U yt-dlp"
    return f"install {binary}"


class Config:
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ

        self.home = Path(env.get("REELSCRIBE_HOME") or DEFAULT_HOME).expanduser()
        self.model = env.get("REELSCRIBE_MODEL") or DEFAULT_MODEL
        self.language = env.get("REELSCRIBE_LANG") or None
        self.translate = env.get("REELSCRIBE_TRANSLATE") == "1"
        self.cookies = env.get("REELSCRIBE_COOKIES") or None
        self.device = env.get("REELSCRIBE_DEVICE") or DEFAULT_DEVICE

        raw_delay = env.get("REELSCRIBE_DELAY")
        try:
            self.delay = float(raw_delay) if raw_delay not in (None, "") else DEFAULT_DELAY
        except ValueError:
            self.delay = DEFAULT_DELAY

        self.token = env.get("REELSCRIBE_TOKEN") or None
        # Required for queue state (see ``require_database_url``); secrets live in serve.env.
        self.database_url = env.get("REELSCRIBE_DATABASE_URL") or None
        self.write_markdown = (env.get("REELSCRIBE_WRITE_MARKDOWN") or "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        self.serve_host, self.serve_port = self._parse_addr(
            env.get("REELSCRIBE_SERVE_ADDR") or DEFAULT_SERVE_ADDR
        )

        if self.model not in VALID_MODELS:
            raise ConfigError(
                f"REELSCRIBE_MODEL={self.model!r} is not one of {sorted(VALID_MODELS)}"
            )
        if self.device not in VALID_DEVICES:
            raise ConfigError(
                f"REELSCRIBE_DEVICE={self.device!r} is not one of {sorted(VALID_DEVICES)}"
            )

    @staticmethod
    def _parse_addr(raw: str) -> tuple[str, int]:
        host, _, port = raw.rpartition(":")
        if not _:  # no colon -> port only
            host, port = "0.0.0.0", raw
        try:
            return (host or "0.0.0.0"), int(port)
        except ValueError:
            raise ConfigError(f"REELSCRIBE_SERVE_ADDR={raw!r} is not HOST:PORT or PORT")

    # --- derived paths -------------------------------------------------
    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError("REELSCRIBE_DATABASE_URL must be set (PostgreSQL connection URL)")
        return self.database_url

    @property
    def audio_dir(self) -> Path:
        return self.home / "audio"

    @property
    def transcript_dir(self) -> Path:
        return self.home / "transcripts"

    @property
    def log_file(self) -> Path:
        return self.home / "run.log"


def check_dependencies(which=shutil.which) -> None:
    """Verify ffmpeg and yt-dlp are on PATH. Raise ConfigError with install hints."""
    missing = [b for b in ("ffmpeg", "yt-dlp") if which(b) is None]
    if missing:
        lines = [f"  {b}: {_install_hint(b)}" for b in missing]
        raise ConfigError(
            "Missing required system dependency:\n" + "\n".join(lines)
        )
