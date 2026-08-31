import pytest

from reelscribe.config import Config, ConfigError, check_dependencies


def test_defaults():
    cfg = Config(environ={})
    assert cfg.model == "medium"
    assert cfg.device == "auto"
    assert cfg.delay == 3.0
    assert cfg.language is None
    assert cfg.translate is False
    assert cfg.cookies is None
    assert cfg.state_file.name == "state.json"
    assert cfg.audio_dir.name == "audio"


def test_env_overrides():
    cfg = Config(
        environ={
            "REELSCRIBE_HOME": "/data/rs",
            "REELSCRIBE_MODEL": "large-v3",
            "REELSCRIBE_LANG": "fr",
            "REELSCRIBE_TRANSLATE": "1",
            "REELSCRIBE_COOKIES": "chrome",
            "REELSCRIBE_DEVICE": "cuda",
            "REELSCRIBE_DELAY": "0.5",
        }
    )
    assert str(cfg.home) == "/data/rs"
    assert cfg.model == "large-v3"
    assert cfg.language == "fr"
    assert cfg.translate is True
    assert cfg.cookies == "chrome"
    assert cfg.device == "cuda"
    assert cfg.delay == 0.5


def test_serve_defaults():
    cfg = Config(environ={})
    assert cfg.token is None
    assert cfg.serve_host == "0.0.0.0"
    assert cfg.serve_port == 8765


def test_serve_addr_override():
    cfg = Config(environ={"REELSCRIBE_TOKEN": "s3cret", "REELSCRIBE_SERVE_ADDR": "127.0.0.1:9000"})
    assert cfg.token == "s3cret"
    assert cfg.serve_host == "127.0.0.1"
    assert cfg.serve_port == 9000


def test_serve_addr_port_only():
    cfg = Config(environ={"REELSCRIBE_SERVE_ADDR": "9999"})
    assert cfg.serve_host == "0.0.0.0"
    assert cfg.serve_port == 9999


def test_bad_serve_addr_rejected():
    with pytest.raises(ConfigError):
        Config(environ={"REELSCRIBE_SERVE_ADDR": "not:a:port"})


def test_bad_delay_falls_back_to_default():
    assert Config(environ={"REELSCRIBE_DELAY": "soon"}).delay == 3.0


def test_invalid_model_rejected():
    with pytest.raises(ConfigError):
        Config(environ={"REELSCRIBE_MODEL": "huge"})


def test_invalid_device_rejected():
    with pytest.raises(ConfigError):
        Config(environ={"REELSCRIBE_DEVICE": "tpu"})


def test_check_dependencies_ok():
    check_dependencies(which=lambda b: f"/usr/bin/{b}")


def test_check_dependencies_reports_all_missing():
    with pytest.raises(ConfigError) as exc:
        check_dependencies(which=lambda b: None)
    msg = str(exc.value)
    assert "ffmpeg" in msg and "yt-dlp" in msg


def test_check_dependencies_reports_only_missing_one():
    with pytest.raises(ConfigError) as exc:
        check_dependencies(which=lambda b: None if b == "ffmpeg" else "/usr/bin/yt-dlp")
    assert "ffmpeg" in str(exc.value)
