import json

import pytest

from reelscribe.state import State, SCHEMA_VERSION, normalize_url

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


def test_igsh_and_www_variants_collapse_to_one_item(tmp_path):
    s = State(tmp_path / "state.json")
    assert s.add("https://www.instagram.com/reel/ABC/?igsh=xyz") is not None
    assert s.add("instagram.com/reel/ABC") is None
    assert len(s.items) == 1


def test_add_returns_normalized_key(tmp_path):
    s = State(tmp_path / "state.json")
    key = s.add("www.instagram.com/reel/ABC/?igsh=1")
    assert key == "https://instagram.com/reel/ABC"
    assert s.items[key]["status"] == "pending"
    assert s.items[key]["attempts"] == 0


def test_schema_round_trip(tmp_path):
    path = tmp_path / "state.json"
    s = State(path)
    s.add("https://instagram.com/reel/ABC")
    s.save()

    reloaded = State.load(path)
    assert reloaded.data["version"] == SCHEMA_VERSION
    assert "https://instagram.com/reel/ABC" in reloaded.items


def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "state.json"
    s = State(path)
    s.add("https://instagram.com/reel/ABC")
    s.save()
    assert not (tmp_path / "state.json.tmp").exists()


def test_crash_mid_write_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    s = State(path)
    s.add("https://instagram.com/reel/ONE")
    s.save()
    good = path.read_text()

    s.add("https://instagram.com/reel/TWO")

    def boom(*a, **k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr("reelscribe.state.os.replace", boom)
    with pytest.raises(OSError):
        s.save()

    # original file untouched, still valid JSON
    assert path.read_text() == good
    assert json.loads(path.read_text())["items"] == {
        "https://instagram.com/reel/ONE": {
            "status": "pending",
            "added": s.items["https://instagram.com/reel/ONE"]["added"],
            "attempts": 0,
        }
    }


def test_to_process_includes_pending_and_failed_only(tmp_path):
    s = State(tmp_path / "state.json")
    s.add("https://x/1")
    s.add("https://x/2")
    s.add("https://x/3")
    s.mark_done("https://x/2", transcript="/t/2.md")
    s.mark_failed("https://x/3", "boom")
    assert set(s.to_process()) == {"https://x/1", "https://x/3"}


def test_mark_failed_increments_attempts_and_records_error(tmp_path):
    s = State(tmp_path / "state.json")
    s.add("https://x/1")
    s.mark_failed("https://x/1", "network down")
    s.mark_failed("https://x/1", "network down again")
    assert s.items["https://x/1"]["attempts"] == 2
    assert s.items["https://x/1"]["error"] == "network down again"


def test_mark_done_clears_prior_error(tmp_path):
    s = State(tmp_path / "state.json")
    s.add("https://x/1")
    s.mark_failed("https://x/1", "boom")
    s.mark_done("https://x/1", transcript="/t/1.md", title="Hi")
    entry = s.items["https://x/1"]
    assert entry["status"] == "done"
    assert "error" not in entry
    assert entry["transcript"] == "/t/1.md"


def test_retry_failed_resets_to_pending(tmp_path):
    s = State(tmp_path / "state.json")
    s.add("https://x/1")
    s.add("https://x/2")
    s.mark_failed("https://x/1", "boom")
    assert s.retry_failed() == 1
    assert s.items["https://x/1"]["status"] == "pending"
