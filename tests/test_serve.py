import json
import urllib.request

import pytest

from reelscribe import serve
from reelscribe.config import Config


@pytest.fixture
def cfg():
    return Config(environ={"REELSCRIBE_TOKEN": "good-token"})


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


def test_post_body_params_accepted(cfg, enqueue_spy):
    status, body = serve.handle(
        "POST", "/add", {"url": "https://x/reel/Z", "token": "good-token"}, cfg, enqueue_spy
    )
    assert status == 202


# ---- Ingest: enqueue + worker ---------------------------------------

def test_ingest_enqueue_dedupes(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "t"})
    ingest = serve.Ingest(cfg, run_once=lambda: None)
    norm1, new1 = ingest.enqueue("https://www.instagram.com/reel/ABC/?igsh=1")
    norm2, new2 = ingest.enqueue("instagram.com/reel/ABC")
    assert norm1 == norm2 == "https://instagram.com/reel/ABC"
    assert new1 is True and new2 is False


def test_ingest_worker_drains_on_wake(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "t"})
    drained = []
    ingest = serve.Ingest(cfg, run_once=lambda: drained.append(True))

    ingest.start()
    ingest.enqueue("https://x/reel/A")
    ingest.wait_idle(timeout=2)
    ingest.stop()

    assert drained  # run_once was invoked after the enqueue woke the worker


def test_ingest_worker_survives_run_error(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "t"})
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

def test_server_end_to_end(tmp_path):
    cfg = Config(
        environ={
            "REELSCRIBE_HOME": str(tmp_path),
            "REELSCRIBE_TOKEN": "tok",
            "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0",
        }
    )
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

    from reelscribe.state import State

    assert "https://x/reel/A" in State.load(tmp_path / "state.json").items


def test_server_accepts_bearer_header(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "tok",
                          "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0"})
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


def test_server_reads_form_body_on_get(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "tok",
                          "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0"})
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


def test_server_rejects_bad_token_over_http(tmp_path):
    cfg = Config(environ={"REELSCRIBE_HOME": str(tmp_path), "REELSCRIBE_TOKEN": "tok",
                          "REELSCRIBE_SERVE_ADDR": "127.0.0.1:0"})
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
