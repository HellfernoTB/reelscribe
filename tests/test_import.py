import importlib.util
import json
from datetime import timezone
from pathlib import Path

import psycopg
import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "import_to_pg.py"
spec = importlib.util.spec_from_file_location("import_to_pg", SCRIPT)
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)

MD = """# Video by alice

- Source: https://instagram.com/reel/AAA
- Uploader: Alice
- Duration: 01:05
- Language: fr
- Transcribed: 2026-08-31T14:16:00

## Caption

Une legende
sur deux lignes

## Transcript

Bonjour a tous. Ceci est un test.

## Timestamped

[00:00] Bonjour a tous.
[01:04] Ceci est un test.
"""

MD_SILENT = """# https://instagram.com/reel/CCC

- Source: https://instagram.com/reel/CCC
- Uploader: unknown
- Duration: unknown
- Language: unknown
- Transcribed: 2026-09-01T08:00:00
- Note: no speech detected in audio

## Transcript

_(no speech detected)_

## Timestamped

_(no speech detected)_
"""


def test_parse_markdown_fields():
    rec = imp.parse_markdown(MD)
    assert rec["url"] == "https://instagram.com/reel/AAA"
    assert (rec["title"], rec["uploader"], rec["duration_s"], rec["language"]) == (
        "Video by alice", "Alice", 65, "fr")
    assert rec["caption"] == "Une legende\nsur deux lignes"
    assert rec["text"] == "Bonjour a tous. Ceci est un test."
    assert rec["segments"] == [
        {"start": 0, "end": None, "text": "Bonjour a tous."},
        {"start": 64, "end": None, "text": "Ceci est un test."},
    ]
    assert rec["transcribed_at"].utcoffset().total_seconds() == 2 * 3600  # Paris, summer


def test_parse_markdown_silent_clip():
    rec = imp.parse_markdown(MD_SILENT)
    assert rec["text"] == "" and rec["segments"] == []
    assert rec["uploader"] is None and rec["duration_s"] is None and rec["language"] is None
    assert rec["note"] == "no speech detected in audio" and rec["caption"] is None


def test_parse_markdown_rejects_other_files():
    with pytest.raises(ValueError):
        imp.parse_markdown("just notes\n")


def test_naive_time_is_europe_paris():
    assert imp.parse_time("2026-01-15T10:00:00").astimezone(timezone.utc).hour == 9  # winter, UTC+1
    assert imp.parse_time("2026-07-15T10:00:00").astimezone(timezone.utc).hour == 8  # summer, UTC+2


def test_relative_audio(tmp_path):
    assert imp.relative_audio(str(tmp_path / "audio" / "a.mp3"), tmp_path) == "audio/a.mp3"
    assert imp.relative_audio("/elsewhere/old/audio/b.mp3", tmp_path) == "audio/b.mp3"
    assert imp.relative_audio(None, tmp_path) is None


@pytest.fixture
def source(tmp_path):
    home = tmp_path / "home"
    (home / "audio").mkdir(parents=True)
    treated = tmp_path / "treated"
    treated.mkdir()
    (treated / "AAA-video.md").write_text(MD)
    transcripts = home / "transcripts"
    transcripts.mkdir()
    (transcripts / "CCC-silent.md").write_text(MD_SILENT)
    (transcripts / "AAA-dup.md").write_text(MD.replace("Alice", "Duplicate"))  # same reel, later dir
    (transcripts / "README.md").write_text("not a transcript")
    state = home / "state.json"
    state.write_text(json.dumps({"version": 1, "items": {
        "https://instagram.com/reel/AAA": {
            "status": "done", "added": "2026-08-31T14:10:00", "completed": "2026-08-31T14:16:00",
            "attempts": 0, "transcript": "/old/place/AAA-video.md",
            "audio": str(home / "audio" / "AAA-video.mp3"), "title": "Video by alice",
            "language": "fr"},
        "https://instagram.com/reel/BBB": {
            "status": "failed", "added": "2026-08-31T14:11:00", "attempts": 3, "error": "boom"},
    }}))
    return home, state, [treated, transcripts]


def test_import_all(pg_url, source):
    home, state, dirs = source
    with psycopg.connect(pg_url) as conn:
        imp.State.open(pg_url)
        counts = imp.import_all(conn, state_file=state, markdown_dirs=dirs, home=home)
        assert counts == {"reels": 3, "done": 2, "failed": 1, "transcripts": 2, "segments": 2}
        a = conn.execute(
            "SELECT status, uploader, duration_s, caption, audio_path, transcript_file, "
            "completed_at, shortcode FROM reel WHERE url = 'https://instagram.com/reel/AAA'"
        ).fetchone()
        assert a[:6] == ("done", "Alice", 65.0, "Une legende\nsur deux lignes", "audio/AAA-video.mp3",
                         "AAA-video.md")
        assert a[6].astimezone(timezone.utc).isoformat() == "2026-08-31T12:16:00+00:00"
        assert a[7] == "AAA"
        b = conn.execute("SELECT status, attempts, last_error FROM reel WHERE shortcode='BBB'").fetchone()
        assert b == ("failed", 3, "boom")
        c = conn.execute("SELECT status, uploader FROM reel WHERE shortcode='CCC'").fetchone()
        assert c == ("done", None)  # md-only reel created as done
        assert conn.execute(
            "SELECT count(*) FROM transcript_segment s JOIN reel r ON r.id = s.reel_id "
            "WHERE r.shortcode='AAA' AND s.end_s IS NULL").fetchone()[0] == 2


def test_import_refuses_non_empty_unless_replace(pg_url, source):
    home, state, dirs = source
    imp.State.open(pg_url)
    with psycopg.connect(pg_url) as conn:
        imp.import_all(conn, state_file=state, markdown_dirs=dirs, home=home)
    with psycopg.connect(pg_url) as conn:
        with pytest.raises(SystemExit, match="not empty"):
            imp.import_all(conn, state_file=state, markdown_dirs=dirs, home=home)
    with psycopg.connect(pg_url) as conn:
        counts = imp.import_all(conn, state_file=state, markdown_dirs=dirs, home=home, replace=True)
        assert counts["reels"] == 3 and counts["transcripts"] == 2


def test_main_prints_counts(pg_url, source, monkeypatch, capsys):
    home, state, dirs = source
    monkeypatch.setenv("REELSCRIBE_DATABASE_URL", pg_url)
    monkeypatch.setenv("REELSCRIBE_HOME", str(home))
    args = ["--state", str(state)] + [a for d in dirs for a in ("--markdown-dir", str(d))]
    assert imp.main(args) == 0
    assert "reels=3, done=2, failed=1, transcripts=2, segments=2" in capsys.readouterr().out
