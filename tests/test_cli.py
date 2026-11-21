from pathlib import Path

import psycopg
import pytest

from reelscribe import cli, pipeline, serve
from reelscribe.config import ConfigError
from reelscribe.fetch import FetchError
from reelscribe.state import State
from reelscribe.transcribe import TranscriptResult


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REELSCRIBE_HOME", str(tmp_path))
    monkeypatch.setenv("REELSCRIBE_MODEL", "small")
    monkeypatch.delenv("REELSCRIBE_TRANSLATE", raising=False)
    monkeypatch.delenv("REELSCRIBE_WRITE_MARKDOWN", raising=False)
    monkeypatch.delenv("REELSCRIBE_DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "check_dependencies", lambda *a, **k: None)
    return tmp_path


@pytest.fixture
def db(pg_url, monkeypatch):
    """Point the CLI at a throwaway schema and return a State on it."""
    monkeypatch.setenv("REELSCRIBE_DATABASE_URL", pg_url)
    return State(pg_url)


class FakeTranscriber:
    def __init__(self, *a, **k):
        pass

    def run(self, audio_path, task="transcribe"):
        return TranscriptResult(text="hello world", rows=[(0.0, 1.0, "hello world")], language="en")

    def translate_to_english(self, audio_path):
        return "hello world"


def _wire(monkeypatch, *, meta=None, fail_urls=()):
    meta = meta or {"id": "vid1", "title": "Clip One", "uploader": "bob", "duration": 12,
                    "description": "a caption"}

    def fake_meta(url, cookies=None):
        if url in fail_urls:
            raise FetchError("ERROR: video unavailable")
        return {**meta, "webpage_url": url}

    def fake_download(url, stem, audio_dir, cookies=None):
        p = Path(audio_dir) / f"{stem}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio")
        return p

    monkeypatch.setattr(pipeline, "fetch_metadata", fake_meta)
    monkeypatch.setattr(pipeline, "download_audio", fake_download)
    monkeypatch.setattr(cli, "Transcriber", FakeTranscriber)


def test_add_dedupes_and_exits_zero(capsys, db):
    assert cli.main(["add", "https://www.instagram.com/reel/ABC/?igsh=1"]) == 0
    assert cli.main(["add", "instagram.com/reel/ABC"]) == 0
    out = capsys.readouterr().out
    assert "already registered" in out


def test_run_all_success_exits_zero(monkeypatch, home, db):
    _wire(monkeypatch)
    cli.main(["add", "https://insta/reel/A", "https://insta/reel/B"])
    assert cli.main(["run"]) == 0
    assert (home / "transcripts" / "vid1-clip-one.md").exists()
    row = db.get("https://insta/reel/A")
    assert row["status"] == "done"
    assert (row["uploader"], row["duration_s"], row["caption"]) == ("bob", 12.0, "a caption")
    assert row["audio_path"] == "audio/vid1-clip-one.mp3"  # relative to REELSCRIBE_HOME
    assert row["transcript_file"] == "vid1-clip-one.md"
    assert row["ytdlp_meta"]["id"] == "vid1"
    with psycopg.connect(db.dsn) as conn:
        text, model = conn.execute("SELECT text, model FROM transcript LIMIT 1").fetchone()
        segs = conn.execute("SELECT start_s, end_s, text FROM transcript_segment").fetchall()
    assert (text, model) == ("hello world", "small")
    assert segs == [(0.0, 1.0, "hello world")] * 2


def test_markdown_export_can_be_disabled(monkeypatch, home, db):
    monkeypatch.setenv("REELSCRIBE_WRITE_MARKDOWN", "0")
    _wire(monkeypatch)
    cli.main(["add", "https://insta/reel/A"])
    assert cli.main(["run"]) == 0
    assert not (home / "transcripts").exists()
    row = db.get("https://insta/reel/A")
    assert row["status"] == "done" and row["transcript_file"] is None


def test_database_url_is_required(capsys):
    assert cli.main(["list"]) == 2
    assert "REELSCRIBE_DATABASE_URL" in capsys.readouterr().err


def test_unreachable_database_exits_two(monkeypatch, capsys):
    monkeypatch.setenv("REELSCRIBE_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    assert cli.main(["list"]) == 2
    assert "database error" in capsys.readouterr().err


def test_run_partial_failure_exits_one(monkeypatch, home, capsys, db):
    _wire(monkeypatch, fail_urls={"https://insta/reel/B"})
    cli.main(["add", "https://insta/reel/A", "https://insta/reel/B"])
    assert cli.main(["run"]) == 1
    capsys.readouterr()
    assert cli.main(["list", "--failed"]) == 0
    out = capsys.readouterr().out
    assert "https://insta/reel/B" in out and "video unavailable" in out
    assert "https://insta/reel/A" not in out


def test_run_nothing_pending_exits_zero(monkeypatch, db):
    _wire(monkeypatch)
    assert cli.main(["run"]) == 0


def test_run_rate_limit_stops_and_exits_one(monkeypatch, home, capsys, db):
    def fake_meta(url, cookies=None):
        raise FetchError("ERROR: HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(pipeline, "fetch_metadata", fake_meta)
    monkeypatch.setattr(cli, "Transcriber", FakeTranscriber)
    cli.main(["add", "https://insta/reel/A", "https://insta/reel/B"])
    assert cli.main(["run"]) == 1
    err = capsys.readouterr().err
    assert "RATE LIMITED" in err and "1 item(s) left" in err
    assert db.get("https://insta/reel/A")["status"] == "failed"
    assert db.get("https://insta/reel/B")["status"] == "pending"  # claim released, not stuck running


def test_missing_dependency_exits_two(monkeypatch, capsys, db):
    def boom(*a, **k):
        raise ConfigError("Missing required system dependency:\n  ffmpeg: apt install ffmpeg")

    monkeypatch.setattr(cli, "check_dependencies", boom)
    cli.main(["add", "https://insta/reel/A"])
    assert cli.main(["run"]) == 2
    assert "ffmpeg" in capsys.readouterr().err


def test_bad_invocation_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2


def test_bad_config_exits_two(monkeypatch, capsys):
    monkeypatch.setenv("REELSCRIBE_MODEL", "gigantic")
    assert cli.main(["list"]) == 2
    assert "REELSCRIBE_MODEL" in capsys.readouterr().err


def test_run_only_processes_single_link(monkeypatch, home, db):
    _wire(monkeypatch)
    cli.main(["add", "https://insta/reel/A", "https://insta/reel/B"])
    assert cli.main(["run", "--only", "https://insta/reel/A"]) == 0
    assert db.get("https://insta/reel/A")["status"] == "done"
    assert db.get("https://insta/reel/B")["status"] == "pending"


def test_retry_resets_failed(monkeypatch, home, db):
    _wire(monkeypatch, fail_urls={"https://insta/reel/A"})
    cli.main(["add", "https://insta/reel/A"])
    cli.main(["run"])
    assert cli.main(["retry"]) == 0
    assert db.get("https://insta/reel/A")["status"] == "pending"


def test_serve_without_token_exits_two(capsys):
    assert cli.main(["serve"]) == 2
    assert "REELSCRIBE_TOKEN" in capsys.readouterr().err


def test_serve_dispatches_to_run_server(monkeypatch):
    monkeypatch.setenv("REELSCRIBE_TOKEN", "x")
    captured = {}

    def fake_run_server(cfg):
        captured["addr"] = (cfg.serve_host, cfg.serve_port)
        return 0

    monkeypatch.setattr(serve, "run_server", fake_run_server)
    assert cli.main(["serve"]) == 0
    assert captured["addr"] == ("0.0.0.0", 8765)


def test_clean_deletes_audio_keeps_transcript(monkeypatch, home, db):
    _wire(monkeypatch)
    cli.main(["add", "https://insta/reel/A"])
    cli.main(["run"])
    audio = home / "audio" / "vid1-clip-one.mp3"
    transcript = home / "transcripts" / "vid1-clip-one.md"
    assert audio.exists() and transcript.exists()
    assert cli.main(["clean"]) == 0
    assert not audio.exists()
    assert transcript.exists()
    assert db.get("https://insta/reel/A")["audio_path"] is None


def test_run_refuses_when_another_worker_holds_lock(monkeypatch, home, db, capsys):
    _wire(monkeypatch)
    cli.main(["add", "https://insta/reel/A"])
    db.reset_stale_running()
    other = db.worker_lock()
    assert other.acquire()
    try:
        assert cli.main(["run"]) == 1
    finally:
        other.release()
    assert "another reelscribe worker is running" in capsys.readouterr().err
    assert db.get("https://insta/reel/A")["status"] == "pending"
    assert cli.main(["run"]) == 0  # lock was free again
