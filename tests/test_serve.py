import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import psycopg
import pytest

from reelscribe import serve
from reelscribe.config import Config
from reelscribe.state import State


@pytest.fixture
def cfg():
    return Config(environ={"REELSCRIBE_TOKEN": "good-token"})


@pytest.fixture
def db_env(pg_url, tmp_path):
    """Config environ for a throwaway schema."""
    return {
        "REELSCRIBE_HOME": str(tmp_path),
        "REELSCRIBE_TOKEN": "tok",
        "REELSCRIBE_DATABASE_URL": pg_url,
        "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0",
    }


@pytest.fixture
def enqueue_spy():
    calls = []
    known = set()

    def enqueue(url):
        calls.append(url)
        norm = url.split("?")[0].rstrip("/")
        is_new = norm not in known
        known.add(norm)
        return norm, is_new

    enqueue.calls = calls
    return enqueue


# ---- handle(): routing ------------------------------------------------

def test_health_needs_no_token(cfg, enqueue_spy):
    status, body = serve.handle("GET", "/health", {}, cfg, enqueue_spy)
    assert status == 200
    assert body["status"] == "ok"
    assert enqueue_spy.calls == []


def test_unknown_path_404(cfg, enqueue_spy):
    status, _ = serve.handle("GET", "/nope", {"token": "good-token"}, cfg, enqueue_spy)
    assert status == 404


def test_add_missing_token_401(cfg, enqueue_spy):
    status, _ = serve.handle("GET", "/add", {"url": "https://x/reel/A"}, cfg, enqueue_spy)
    assert status == 401
    assert enqueue_spy.calls == []


def test_add_wrong_token_401(cfg, enqueue_spy):
    status, _ = serve.handle(
        "GET", "/add", {"url": "https://x/reel/A", "token": "bad"}, cfg, enqueue_spy
    )
    assert status == 401


def test_add_missing_url_400(cfg, enqueue_spy):
    status, _ = serve.handle("GET", "/add", {"token": "good-token"}, cfg, enqueue_spy)
    assert status == 400
    assert enqueue_spy.calls == []


def test_add_wrong_method_405(cfg, enqueue_spy):
    status, _ = serve.handle(
        "PUT", "/add", {"url": "https://x/reel/A", "token": "good-token"}, cfg, enqueue_spy
    )
    assert status == 405


def test_add_new_url_202_and_enqueues(cfg, enqueue_spy):
    status, body = serve.handle(
        "GET", "/add",
        {"url": "https://www.instagram.com/reel/ABC/?igsh=1", "token": "good-token"},
        cfg, enqueue_spy,
    )
    assert status == 202
    assert body["queued"] == "https://www.instagram.com/reel/ABC"
    assert enqueue_spy.calls == ["https://www.instagram.com/reel/ABC/?igsh=1"]


def test_add_duplicate_url_200_skipped(cfg, enqueue_spy):
    args = ("GET", "/add", {"url": "https://x/reel/A", "token": "good-token"}, cfg, enqueue_spy)
    serve.handle(*args)
    status, body = serve.handle(*args)
    assert status == 200
    assert body["skipped"] == "https://x/reel/A"


def test_add_returns_503_when_database_unreachable(cfg):
    def enqueue(url):
        raise psycopg.OperationalError("connection refused")

    status, body = serve.handle(
        "GET", "/add", {"url": "https://x/reel/A", "token": "good-token"}, cfg, enqueue
    )
    assert status == 503
    assert "database" in body["error"]


def test_post_body_params_accepted(cfg, enqueue_spy):
    status, body = serve.handle(
        "POST", "/add", {"url": "https://x/reel/Z", "token": "good-token"}, cfg, enqueue_spy
    )
    assert status == 202


# ---- Ingest: enqueue + worker ---------------------------------------

def test_ingest_enqueue_dedupes(db_env):
    cfg = Config(environ=db_env)
    ingest = serve.Ingest(cfg, run_once=lambda: None)
    norm1, new1 = ingest.enqueue("https://www.instagram.com/reel/ABC/?igsh=1")
    norm2, new2 = ingest.enqueue("instagram.com/reel/ABC")
    assert norm1 == norm2 == "https://instagram.com/reel/ABC"
    assert new1 is True and new2 is False


def test_ingest_worker_drains_on_wake(db_env):
    cfg = Config(environ=db_env)
    drained = []
    ingest = serve.Ingest(cfg, run_once=lambda: drained.append(True))

    ingest.start()
    ingest.enqueue("https://x/reel/A")
    ingest.wait_idle(timeout=2)
    ingest.stop()

    assert drained  # run_once was invoked after the enqueue woke the worker


def test_ingest_worker_survives_run_error(db_env):
    cfg = Config(environ=db_env)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    ingest = serve.Ingest(cfg, run_once=flaky)
    ingest.start()
    ingest.enqueue("https://x/reel/A")
    ingest.wait_idle(timeout=2)
    ingest.enqueue("https://x/reel/B")
    ingest.wait_idle(timeout=2)
    ingest.stop()

    assert len(calls) == 2  # thread kept running after the first call raised


# ---- run_server: real socket smoke --------------------------------

def test_server_end_to_end(db_env, pg_url):
    cfg = Config(environ=db_env)
    seen = []
    server = serve.build_server(cfg, run_once=lambda: seen.append(True))
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
            assert r.status == 200
        url = "http://127.0.0.1:%d/add?token=tok&url=https://x/reel/A" % port
        with urllib.request.urlopen(url) as r:
            assert r.status == 202
            assert json.load(r)["queued"] == "https://x/reel/A"
    finally:
        server.shutdown()
        t.join(timeout=2)

    assert State(pg_url).get("https://x/reel/A")["status"] == "pending"


def test_server_accepts_bearer_header(db_env):
    cfg = Config(environ=db_env)
    server = serve.build_server(cfg, run_once=lambda: None)
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/add?url=https://x/reel/H",
            headers={"Authorization": "Bearer tok"},
        )
        with urllib.request.urlopen(req) as r:
            assert r.status == 202
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_server_reads_form_body_on_get(db_env):
    cfg = Config(environ=db_env)
    server = serve.build_server(cfg, run_once=lambda: None)
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/add",
            data=b"token=tok&url=https://x/reel/B",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="GET",
        )
        with urllib.request.urlopen(req) as r:
            assert r.status == 202
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_server_rejects_bad_token_over_http(db_env):
    cfg = Config(environ=db_env)
    server = serve.build_server(cfg, run_once=lambda: None)
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/add?token=nope&url=https://x/reel/A")
        assert exc.value.code == 401
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_server_returns_503_over_http_when_db_down(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "tok",
                          "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0",
                          "REELSCRIBE_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/none"})
    server = serve.build_server(cfg, run_once=lambda: None)  # starts even with the DB down
    import threading

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/add?token=tok&url=https://x/reel/A")
        assert exc.value.code == 503
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
            assert r.status == 200
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_drain_resets_stale_running_rows_under_lock(db_env, pg_url, monkeypatch):
    state = State.open(pg_url)
    state.add("https://x/reel/A")
    assert state.to_process() == ["https://x/reel/A"]  # a crashed worker left it running
    seen = []
    monkeypatch.setattr(serve, "process_item", lambda cfg, t, url: seen.append(url) or {
        "text": "t", "transcribed_at": datetime.now(timezone.utc), "transcript_file": None})
    monkeypatch.setattr(serve, "Transcriber", lambda *a, **k: object())
    ingest = serve.Ingest(Config(environ=db_env))
    ingest._default_drain()
    ingest.stop()
    assert seen == ["https://x/reel/A"]
    assert state.get("https://x/reel/A")["status"] == "done"


def test_drain_waits_while_another_worker_holds_lock(db_env, pg_url, monkeypatch):
    monkeypatch.setattr(serve, "LOCK_RETRY_SECONDS", 0.05)
    state = State.open(pg_url)
    state.add("https://x/reel/A")
    other = state.worker_lock()
    assert other.acquire()
    seen = []
    monkeypatch.setattr(serve, "process_item", lambda cfg, t, url: seen.append(url) or {
        "text": "t", "transcribed_at": datetime.now(timezone.utc), "transcript_file": None})
    monkeypatch.setattr(serve, "Transcriber", lambda *a, **k: object())
    ingest = serve.Ingest(Config(environ=db_env))
    ingest.start()
    ingest.enqueue("https://x/reel/B")  # /add still works while another worker holds the lock
    import time
    time.sleep(0.3)
    assert seen == [] and state.get("https://x/reel/A")["status"] == "pending"
    other.release()
    ingest.wait_idle(timeout=5)
    ingest.stop()
    assert sorted(seen) == ["https://x/reel/A", "https://x/reel/B"]


def test_default_drain_processes_and_releases(db_env, pg_url, monkeypatch):
    seen = []

    def fake_process(cfg, transcriber, url):
        seen.append(url)
        if url.endswith("/bad"):
            raise RuntimeError("nope")
        return {"text": "t", "transcribed_at": datetime.now(timezone.utc), "segments": [],
                "transcript_file": None}

    monkeypatch.setattr(serve, "process_item", fake_process)
    monkeypatch.setattr(serve, "Transcriber", lambda *a, **k: object())
    ingest = serve.Ingest(Config(environ=db_env))
    ingest.enqueue("https://x/reel/ok")
    ingest.enqueue("https://x/reel/bad")
    ingest._default_drain()
    ingest.stop()
    state = State(pg_url)
    assert state.get("https://x/reel/ok")["status"] == "done"
    bad = state.get("https://x/reel/bad")
    assert bad["status"] == "failed" and bad["attempts"] == 1 and "nope" in bad["last_error"]
