from types import SimpleNamespace

from reelscribe.transcribe import Transcriber


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        segments = [
            SimpleNamespace(start=0.0, end=1.5, text=" Bonjour "),
            SimpleNamespace(start=1.5, end=3.0, text=" tout le monde "),
        ]
        info = SimpleNamespace(language="fr")
        return iter(segments), info


def _transcriber(**kw):
    holder = {}

    def factory(model_size, device):
        holder["instances"] = holder.get("instances", 0) + 1
        holder["model"] = FakeModel()
        return holder["model"]

    t = Transcriber(model_factory=factory, **kw)
    return t, holder


def test_run_collects_text_rows_and_language():
    t, _ = _transcriber()
    result = t.run("/tmp/a.mp3")
    assert result.text == "Bonjour tout le monde"
    assert result.language == "fr"
    assert result.rows[0] == (0.0, 1.5, "Bonjour")
    assert not result.is_empty


def test_language_and_task_pass_through():
    t, holder = _transcriber(language="fr")
    t.run("/tmp/a.mp3", task="translate")
    _path, kwargs = holder["model"].calls[0]
    assert kwargs["language"] == "fr"
    assert kwargs["task"] == "translate"
    assert kwargs["vad_filter"] is True


def test_model_instantiated_only_once_across_calls():
    t, holder = _transcriber()
    t.run("/tmp/a.mp3")
    t.run("/tmp/b.mp3")
    t.translate_to_english("/tmp/c.mp3")
    assert holder["instances"] == 1
    assert len(holder["model"].calls) == 3


def test_translate_returns_text():
    t, _ = _transcriber()
    assert t.translate_to_english("/tmp/a.mp3") == "Bonjour tout le monde"


def test_empty_result_flagged():
    from reelscribe.transcribe import TranscriptResult

    assert TranscriptResult(text="", rows=[]).is_empty
