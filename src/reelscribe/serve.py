"""Opt-in HTTP ingestion endpoint.

``reelscribe serve`` runs a small stdlib HTTP listener (no extra dependencies).
A phone Share-sheet shortcut hits ``GET /add?url=...&token=...`` over Tailscale;
the URL is queued and a background worker runs the normal pipeline, keeping the
Whisper model loaded for the life of the process.

This is the one part of reelscribe that is a daemon. The core CLI stays
daemon-free; nothing here runs unless you start it.
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from .config import Config
from .pipeline import process_item
from .state import State, normalize_url
from .transcribe import Transcriber


# --------------------------------------------------------------------- routing


def handle(method: str, path: str, params: dict, cfg: Config, enqueue) -> tuple[int, dict]:
    """Pure request router. ``enqueue(url) -> (normalized, is_new)``."""
    if path == "/health":
        return 200, {"status": "ok"}
    if path != "/add":
        return 404, {"error": "not found"}
    if method not in ("GET", "POST"):
        return 405, {"error": "method not allowed"}

    token = params.get("token") or ""
    if not cfg.token or not hmac.compare_digest(token, cfg.token):
        return 401, {"error": "unauthorized"}

    url = (params.get("url") or "").strip()
    if not url:
        return 400, {"error": "missing url"}

    normalized, is_new = enqueue(url)
    if is_new:
        return 202, {"queued": normalized}
    return 200, {"skipped": normalized}


# --------------------------------------------------------------------- ingest


class Ingest:
    """Queue writes + a single background worker that drains pending items."""

    def __init__(self, cfg: Config, run_once=None):
        self._cfg = cfg
        self._run_once = run_once or self._default_drain
        self._state = State.load(cfg.state_file)
        self._state_lock = threading.Lock()
        self._transcriber: Transcriber | None = None

        self._cv = threading.Condition()
        self._wakes = 0
        self._processed = 0
        self._stop = False
        self._thread: threading.Thread | None = None

    # -- public API --------------------------------------------------
    def enqueue(self, url: str) -> tuple[str, bool]:
        with self._state_lock:
            key = self._state.add(url)
            self._state.save()
        is_new = key is not None
        if is_new:
            with self._cv:
                self._wakes += 1
                self._cv.notify_all()
        return normalize_url(url), is_new

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="reelscribe-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the worker has processed every enqueue so far."""
        with self._cv:
            return self._cv.wait_for(lambda: self._processed >= self._wakes, timeout=timeout)

    # -- worker ----------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._stop or self._processed < self._wakes)
                if self._stop:
                    return
                target = self._wakes
            try:
                self._run_once()
            except Exception as exc:  # never let the worker thread die
                print(f"reelscribe worker error: {exc}", file=sys.stderr, flush=True)
            with self._cv:
                self._processed = target
                self._cv.notify_all()

    def _default_drain(self) -> None:
        with self._state_lock:
            pending = self._state.to_process()
        if not pending:
            return
        if self._transcriber is None:
            self._transcriber = Transcriber(
                self._cfg.model, device=self._cfg.device, language=self._cfg.language
            )
        for url in pending:
            try:
                fields = process_item(self._cfg, self._transcriber, url)
                with self._state_lock:
                    self._state.mark_done(url, **fields)
                    self._state.save()
                print(f"transcribed {url} -> {fields['transcript']}", flush=True)
            except Exception as exc:
                with self._state_lock:
                    self._state.mark_failed(url, str(exc))
                    self._state.save()
                print(f"FAILED {url}: {exc}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------- http


class _Handler(BaseHTTPRequestHandler):
    server_version = "reelscribe"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        params = dict(parse_qsl(parsed.query))

        # Read a request body on any method - some HTTP clients put GET
        # parameters in the body rather than the query string.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length).decode("utf-8", "replace")
            if "json" in (self.headers.get("Content-Type") or ""):
                try:
                    params.update(json.loads(raw))
                except ValueError:
                    pass
            else:
                params.update(parse_qsl(raw))

        # Accept the token as an Authorization: Bearer header too.
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            params.setdefault("token", auth[7:].strip())

        status, body = handle(
            method, parsed.path, params, self.server.cfg, self.server.ingest.enqueue
        )
        if parsed.path == "/add":
            detail = body.get("queued") or body.get("skipped") or body.get("error", "")
            print(f"[serve] {method} /add -> {status} {detail}", file=sys.stderr, flush=True)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # keep the token out of the journal - drop any query string
        line = (fmt % args).replace(self.path, urlparse(self.path).path, 1)
        print("[serve] " + line, file=sys.stderr, flush=True)


def build_server(cfg: Config, run_once=None) -> ThreadingHTTPServer:
    ingest = Ingest(cfg, run_once=run_once)
    ingest.start()
    server = ThreadingHTTPServer((cfg.serve_host, cfg.serve_port), _Handler)
    server.cfg = cfg
    server.ingest = ingest
    return server


def run_server(cfg: Config) -> int:
    if not cfg.token:
        print(
            "REELSCRIBE_TOKEN must be set before running `reelscribe serve`.",
            file=sys.stderr,
        )
        return 2
    server = build_server(cfg)
    print(f"reelscribe serve listening on {cfg.serve_host}:{cfg.serve_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.ingest.stop()
        server.server_close()
    return 0
