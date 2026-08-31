"""Queue persistence and URL normalization.

state.json schema (version 1):

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
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEME_RE = re.compile(r"^(?P<scheme>https?://)?(?P<rest>.*)$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Canonical key for a link.

    - force an https:// scheme
    - lowercase the host, drop a leading ``www.``
    - drop query string and fragment
    - drop the trailing slash

    So ``instagram.com/reel/ABC/?igsh=xyz`` and
    ``https://www.instagram.com/reel/ABC`` map to the same key.
    Path case is preserved (reel IDs are case-sensitive).
    """
    match = _SCHEME_RE.match(url.strip())
    rest = match.group("rest")

    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host, path = rest, ""

    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")

    return f"https://{host}{path}"


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"version": SCHEMA_VERSION, "items": {}}

    @classmethod
    def load(cls, path) -> "State":
        state = cls(path)
        if state.path.exists():
            raw = json.loads(state.path.read_text())
            raw.setdefault("version", SCHEMA_VERSION)
            raw.setdefault("items", {})
            state.data = raw
        return state

    def save(self) -> None:
        """Atomic write: temp file then ``os.replace``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

    # --- queue operations -------------------------------------------------
    @property
    def items(self) -> dict:
        return self.data["items"]

    def add(self, url: str) -> str | None:
        """Register a link. Returns the normalized key, or None if already present."""
        key = normalize_url(url)
        if key in self.items:
            return None
        self.items[key] = {"status": "pending", "added": now_iso(), "attempts": 0}
        return key

    def to_process(self) -> list[str]:
        return [k for k, v in self.items.items() if v["status"] in ("pending", "failed")]

    def mark_done(self, key: str, **fields) -> None:
        entry = self.items[key]
        entry.update(status="done", completed=now_iso(), **fields)
        entry.pop("error", None)

    def mark_failed(self, key: str, error: str) -> None:
        entry = self.items[key]
        entry["status"] = "failed"
        entry["error"] = str(error)
        entry["attempts"] = entry.get("attempts", 0) + 1

    def retry_failed(self) -> int:
        n = 0
        for entry in self.items.values():
            if entry["status"] == "failed":
                entry["status"] = "pending"
                n += 1
        return n
