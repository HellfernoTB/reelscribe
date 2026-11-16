"""Queue persistence (PostgreSQL) and URL normalization.

The queue and every transcript live in the ``reel`` / ``transcript`` /
``transcript_segment`` tables of the connection's current schema
(``instagram_recap`` in production; the role's ``search_path`` picks it). Nothing
is held in memory between calls: each operation is one short transaction on its
own connection, so the HTTP server and the CLI can share the queue safely.

Status values: ``pending``, ``running`` (claimed by a worker), ``done``, ``failed``.
"""

from __future__ import annotations

import re
from datetime import datetime
from importlib import resources
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

MAX_ATTEMPTS = 5

_SCHEME_RE = re.compile(r"^(?P<scheme>https?://)?(?P<rest>.*)$", re.IGNORECASE)
_MIGRATION_LOCK = 7203_1701  # arbitrary pg_advisory_xact_lock key


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


def shortcode(normalized_url: str) -> str:
    """Last path segment of a normalized URL (``.../reel/ABC`` -> ``ABC``)."""
    segments = [s for s in urlparse(normalized_url).path.split("/") if s]
    return segments[-1] if segments else normalized_url


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply any ``migrations/*.sql`` not yet recorded in ``schema_migrations``.

    Idempotent and safe to race: a transaction-level advisory lock serializes
    concurrent starters. Returns the versions applied by this call.
    """
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.cursor(row_factory=tuple_row).execute("SELECT version FROM schema_migrations")}
        files = sorted(
            (p for p in resources.files("reelscribe").joinpath("migrations").iterdir()
             if p.name.endswith(".sql")),
            key=lambda p: p.name,
        )
        for path in files:
            version = path.name[: -len(".sql")]
            if version in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


class State:
    """Repository over the reel tables. ``dsn`` is a libpq connection string/URL."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    @classmethod
    def open(cls, dsn: str) -> "State":
        """Build a State and make sure the schema is migrated."""
        state = cls(dsn)
        with state._connect() as conn:
            apply_migrations(conn)
        return state

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)

    # --- queue operations -------------------------------------------------
    def add(self, url: str) -> str | None:
        """Register a link. Returns the normalized key, or None if already present."""
        key = normalize_url(url)
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO reel (url, shortcode, status, added_at) "
                "VALUES (%s, %s, 'pending', now()) ON CONFLICT (url) DO NOTHING RETURNING id",
                (key, shortcode(key)),
            ).fetchone()
        return key if row else None

    def save(self) -> None:
        """No-op kept for API compatibility: every operation commits on its own."""

    def to_process(self) -> list[str]:
        """Claim and return pending rows plus failed rows under the attempt cap.

        Claimed rows are set to ``running`` (``FOR UPDATE SKIP LOCKED``), so two
        workers never take the same reel. Oldest first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "UPDATE reel SET status = 'running' WHERE id IN ("
                "  SELECT id FROM reel WHERE status = 'pending' "
                "     OR (status = 'failed' AND attempts < %s) "
                "  FOR UPDATE SKIP LOCKED) RETURNING url, added_at, id",
                (MAX_ATTEMPTS,),
            ).fetchall()
        rows.sort(key=lambda r: (r["added_at"], r["id"]))
        return [r["url"] for r in rows]

    def claim(self, url: str) -> None:
        """Mark one specific row running (``run --only``), whatever its status."""
        with self._connect() as conn:
            conn.execute("UPDATE reel SET status = 'running' WHERE url = %s", (normalize_url(url),))

    def release(self, urls: list[str]) -> None:
        """Give claimed-but-unprocessed rows back to the queue."""
        if urls:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE reel SET status = 'pending' WHERE status = 'running' AND url = ANY(%s)",
                    (list(urls),),
                )

    def reset_stale_running(self) -> int:
        """Startup recovery: a crashed worker leaves rows ``running``."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE reel SET status = 'pending' WHERE status = 'running'"
            ).rowcount

    def mark_done(
        self,
        url: str,
        *,
        text: str,
        transcribed_at: datetime,
        segments=(),
        english_text: str | None = None,
        note: str | None = None,
        model: str | None = None,
        title: str | None = None,
        uploader: str | None = None,
        duration_s: float | None = None,
        caption: str | None = None,
        language: str | None = None,
        audio_path: str | None = None,
        transcript_file: str | None = None,
        ytdlp_meta: dict | None = None,
    ) -> None:
        """Record a finished item: reel fields, transcript and segments, one transaction."""
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE reel SET status = 'done', completed_at = now(), last_error = NULL, "
                "title = %s, uploader = %s, duration_s = %s, caption = %s, language = %s, "
                "audio_path = %s, transcript_file = %s, ytdlp_meta = %s WHERE url = %s RETURNING id",
                (title, uploader, duration_s, caption, language, audio_path, transcript_file,
                 Jsonb(ytdlp_meta) if ytdlp_meta is not None else None, url),
            ).fetchone()
            if row is None:
                raise KeyError(url)
            rid = row["id"]
            conn.execute(
                "INSERT INTO transcript (reel_id, text, english_text, note, model, transcribed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (reel_id) DO UPDATE SET "
                "text = EXCLUDED.text, english_text = EXCLUDED.english_text, note = EXCLUDED.note, "
                "model = EXCLUDED.model, transcribed_at = EXCLUDED.transcribed_at",
                (rid, text, english_text, note, model, transcribed_at),
            )
            conn.execute("DELETE FROM transcript_segment WHERE reel_id = %s", (rid,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO transcript_segment (reel_id, idx, start_s, end_s, text) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(rid, i, s["start"], s.get("end"), s["text"]) for i, s in enumerate(segments)],
                )

    def mark_failed(self, url: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reel SET status = 'failed', last_error = %s, attempts = attempts + 1 "
                "WHERE url = %s",
                (str(error), url),
            )

    def retry_failed(self) -> int:
        """Reset every failed item to pending (``attempts`` is kept as history)."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE reel SET status = 'pending' WHERE status = 'failed'"
            ).rowcount

    # --- reads ------------------------------------------------------------
    def list_items(self, failed_only: bool = False) -> list[dict]:
        """All reels (oldest first) as dicts with the ``reel`` columns."""
        sql = "SELECT * FROM reel"
        if failed_only:
            sql += " WHERE status = 'failed'"
        with self._connect() as conn:
            return conn.execute(sql + " ORDER BY added_at, id").fetchall()

    def get(self, url: str) -> dict | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM reel WHERE url = %s", (normalize_url(url),)
            ).fetchone()

    def done_audio(self) -> list[dict]:
        """Done reels that still reference an audio file: ``[{url, audio_path}]``."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT url, audio_path FROM reel WHERE status = 'done' AND audio_path IS NOT NULL"
            ).fetchall()

    def clear_audio_path(self, url: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE reel SET audio_path = NULL WHERE url = %s", (url,))
