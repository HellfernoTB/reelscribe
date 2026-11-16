from datetime import datetime, timezone

import psycopg
import pytest

from reelscribe.state import MAX_ATTEMPTS, State, apply_migrations, normalize_url, shortcode

NORMALIZATION_CASES = [
    (
        "https://www.instagram.com/reel/ABC/?igsh=xyz123",
        "https://instagram.com/reel/ABC",
    ),
    ("www.instagram.com/reel/ABC", "https://instagram.com/reel/ABC"),
    ("https://instagram.com/reel/ABC/", "https://instagram.com/reel/ABC"),
    ("http://Instagram.com/reel/ABC", "https://instagram.com/reel/ABC"),
    ("  https://instagram.com/reel/ABC#frag  ", "https://instagram.com/reel/ABC"),
    (
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=1s",
        "https://youtube.com/watch",
    ),
]


@pytest.mark.parametrize("raw,expected", NORMALIZATION_CASES)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_shortcode_is_last_path_segment():
    assert shortcode("https://instagram.com/reel/Da0H8Qzh5Mk") == "Da0H8Qzh5Mk"
    assert shortcode("https://instagram.com/p/XyZ") == "XyZ"


# ---- PostgreSQL-backed (skip without HOMELAB_TEST_DATABASE_URL) ----------

@pytest.fixture
def state(pg_url):
    return State.open(pg_url)


def _row(state, url):
    return state.get(url)


def test_migrations_are_idempotent_and_tracked(pg_url):
    with psycopg.connect(pg_url) as conn:
        assert apply_migrations(conn) == ["001_init"]
        assert apply_migrations(conn) == []
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == ["001_init"]
    State.open(pg_url)  # a third apply via open() must not fail


def test_status_check_constraint(state, pg_url):
    with psycopg.connect(pg_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO reel (url, shortcode, status, added_at) VALUES ('u','s','bogus',now())"
        )


def test_igsh_and_www_variants_collapse_to_one_item(state):
    assert state.add("https://www.instagram.com/reel/ABC/?igsh=xyz") is not None
    assert state.add("instagram.com/reel/ABC") is None
    assert len(state.list_items()) == 1


def test_add_returns_normalized_key_and_derives_shortcode(state):
    key = state.add("www.instagram.com/reel/ABC/?igsh=1")
    assert key == "https://instagram.com/reel/ABC"
    row = _row(state, key)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["shortcode"] == "ABC"
    assert row["added_at"].tzinfo is not None


def test_to_process_claims_pending_and_failed_only(state):
    for n in (1, 2, 3):
        state.add(f"https://x/reel/{n}")
    state.mark_done("https://x/reel/2", text="t", transcribed_at=datetime.now(timezone.utc))
    state.mark_failed("https://x/reel/3", "boom")
    claimed = state.to_process()
    assert claimed == ["https://x/reel/1", "https://x/reel/3"]  # oldest first
    assert _row(state, "https://x/reel/1")["status"] == "running"
    assert state.to_process() == []  # already claimed


def test_to_process_skips_failed_at_attempt_cap(state):
    state.add("https://x/reel/1")
    for _ in range(MAX_ATTEMPTS):
        state.mark_failed("https://x/reel/1", "boom")
    assert state.to_process() == []
    assert state.retry_failed() == 1  # explicit retry makes it pending again
    assert state.to_process() == ["https://x/reel/1"]


def test_concurrent_claims_do_not_overlap(state, pg_url):
    for n in range(10):
        state.add(f"https://x/reel/{n}")
    other = State(pg_url)
    a, b = state.to_process(), other.to_process()
    assert not set(a) & set(b) and len(a) + len(b) == 10


def test_release_and_reset_stale_running(state):
    state.add("https://x/reel/1")
    state.add("https://x/reel/2")
    assert len(state.to_process()) == 2
    state.release(["https://x/reel/1"])
    assert _row(state, "https://x/reel/1")["status"] == "pending"
    assert state.reset_stale_running() == 1
    assert _row(state, "https://x/reel/2")["status"] == "pending"


def test_mark_failed_increments_attempts_and_records_error(state):
    state.add("https://x/1")
    state.mark_failed("https://x/1", "network down")
    state.mark_failed("https://x/1", "network down again")
    row = _row(state, "https://x/1")
    assert row["status"] == "failed"
    assert row["attempts"] == 2
    assert row["last_error"] == "network down again"


def test_mark_done_stores_transcript_segments_and_clears_error(state, pg_url):
    state.add("https://x/reel/1")
    state.mark_failed("https://x/reel/1", "boom")
    when = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    state.mark_done(
        "https://x/reel/1", title="Hi", uploader="bob", duration_s=12.5, caption="cap",
        language="fr", audio_path="audio/a.mp3", transcript_file="a.md",
        ytdlp_meta={"id": "1", "tags": ["a"]}, text="bonjour", english_text="hello",
        note=None, model="small", transcribed_at=when,
        segments=[{"start": 0.0, "end": 1.5, "text": "bon"}, {"start": 1.5, "end": 3.0, "text": "jour"}],
    )
    row = _row(state, "https://x/reel/1")
    assert row["status"] == "done" and row["last_error"] is None and row["completed_at"]
    assert (row["title"], row["uploader"], row["duration_s"]) == ("Hi", "bob", 12.5)
    assert row["ytdlp_meta"] == {"id": "1", "tags": ["a"]}
    with psycopg.connect(pg_url) as conn:
        t = conn.execute("SELECT text, english_text, model, transcribed_at FROM transcript").fetchone()
        segs = conn.execute(
            "SELECT idx, start_s, end_s, text FROM transcript_segment ORDER BY idx"
        ).fetchall()
    assert t == ("bonjour", "hello", "small", when)
    assert segs == [(0, 0.0, 1.5, "bon"), (1, 1.5, 3.0, "jour")]


def test_mark_done_twice_replaces_segments(state, pg_url):
    state.add("https://x/reel/1")
    when = datetime.now(timezone.utc)
    seg = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 1.0, "end": 2.0, "text": "b"}]
    state.mark_done("https://x/reel/1", text="ab", transcribed_at=when, segments=seg)
    state.mark_done("https://x/reel/1", text="a", transcribed_at=when, segments=seg[:1])
    with psycopg.connect(pg_url) as conn:
        assert conn.execute("SELECT count(*) FROM transcript_segment").fetchone()[0] == 1
        assert conn.execute("SELECT text FROM transcript").fetchone()[0] == "a"


def test_mark_done_is_atomic(state, pg_url):
    state.add("https://x/reel/1")
    bad = [{"start": 0.0, "end": 1.0, "text": None}]  # violates NOT NULL on text
    with pytest.raises(psycopg.errors.NotNullViolation):
        state.mark_done("https://x/reel/1", text="x", transcribed_at=datetime.now(timezone.utc),
                        segments=bad)
    assert _row(state, "https://x/reel/1")["status"] == "pending"
    with psycopg.connect(pg_url) as conn:
        assert conn.execute("SELECT count(*) FROM transcript").fetchone()[0] == 0


def test_retry_failed_resets_to_pending(state):
    state.add("https://x/1")
    state.add("https://x/2")
    state.mark_failed("https://x/1", "boom")
    assert state.retry_failed() == 1
    assert _row(state, "https://x/1")["status"] == "pending"


def test_list_items_failed_only(state):
    state.add("https://x/1")
    state.add("https://x/2")
    state.mark_failed("https://x/2", "e")
    assert [r["url"] for r in state.list_items(failed_only=True)] == ["https://x/2"]
    assert len(state.list_items()) == 2


def test_worker_lock_excludes_second_worker(state, pg_url):
    state.add("https://x/reel/1")
    first, second = state.worker_lock(), State(pg_url).worker_lock()
    assert first.acquire()
    assert first.acquire()  # re-acquire while held is a no-op
    assert not second.acquire()  # second worker can neither reset nor claim
    assert state.to_process() == ["https://x/reel/1"]  # first worker's claim ...
    assert not second.acquire()  # ... stays safe: it cannot reach reset_stale_running
    first.release()
    assert second.acquire()
    second.release()


def test_worker_lock_is_per_schema(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA other_lock_schema")
        try:
            other = pg_url.rsplit("search_path", 1)[0] + "search_path%3Dother_lock_schema"
            a, b = State(pg_url).worker_lock(), State(other).worker_lock()
            assert a.acquire() and b.acquire()
            a.release()
            b.release()
        finally:
            conn.execute("DROP SCHEMA other_lock_schema CASCADE")
