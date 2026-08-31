import json
import subprocess
from pathlib import Path

import pytest

from reelscribe import fetch

FIXTURE = Path(__file__).parent / "fixtures" / "ytdlp_reel.json"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["yt-dlp"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run(cmd, *args, **kwargs):
        recorded.append(cmd)
        return fake_run.result

    fake_run.result = _completed()
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    return recorded, fake_run


def test_fetch_metadata_parses_json(calls):
    recorded, fake_run = calls
    fake_run.result = _completed(stdout=FIXTURE.read_text())
    meta = fetch.fetch_metadata("https://insta/reel/ABC")
    assert meta["id"] == "C9xYzAbCdEf"
    assert "--dump-single-json" in recorded[0]


def test_cookie_flag_absent_by_default(calls):
    recorded, fake_run = calls
    fake_run.result = _completed(stdout="{}")
    fetch.fetch_metadata("https://insta/reel/ABC")
    assert "--cookies-from-browser" not in recorded[0]


def test_cookie_flag_present_when_configured(calls):
    recorded, fake_run = calls
    fake_run.result = _completed(stdout="{}")
    fetch.fetch_metadata("https://insta/reel/ABC", cookies="chrome")
    assert recorded[0].count("--cookies-from-browser") == 1
    idx = recorded[0].index("--cookies-from-browser")
    assert recorded[0][idx + 1] == "chrome"


def test_metadata_surfaces_stderr_on_failure(calls):
    _, fake_run = calls
    fake_run.result = _completed(returncode=1, stderr="ERROR: login required to view this post")
    with pytest.raises(fetch.FetchError) as exc:
        fetch.fetch_metadata("https://insta/reel/ABC")
    assert "login required" in str(exc.value)


def test_download_surfaces_stderr_on_failure(calls, tmp_path):
    _, fake_run = calls
    fake_run.result = _completed(returncode=1, stderr="ERROR: HTTP Error 429: Too Many Requests")
    with pytest.raises(fetch.FetchError) as exc:
        fetch.download_audio("https://insta/reel/ABC", "abc-clip", tmp_path)
    assert "429" in str(exc.value)


def test_download_short_circuits_when_audio_exists(calls, tmp_path):
    recorded, _ = calls
    mp3 = tmp_path / "abc-clip.mp3"
    mp3.write_bytes(b"fake audio")
    result = fetch.download_audio("https://insta/reel/ABC", "abc-clip", tmp_path)
    assert result == mp3
    assert recorded == []  # yt-dlp never invoked


def test_download_errors_if_mp3_missing_after_success(calls, tmp_path):
    _, fake_run = calls
    fake_run.result = _completed(returncode=0)
    with pytest.raises(fetch.FetchError):
        fetch.download_audio("https://insta/reel/ABC", "abc-clip", tmp_path)


def test_download_passes_audio_extraction_flags(monkeypatch, tmp_path):
    recorded = []

    def fake_run(cmd, *a, **k):
        recorded.append(cmd)
        (tmp_path / "abc-clip.mp3").write_bytes(b"x")
        return _completed()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.download_audio("https://insta/reel/ABC", "abc-clip", tmp_path)

    cmd = recorded[0]
    assert "-x" in cmd and "--audio-format" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "mp3"
    assert "https://insta/reel/ABC" in cmd
