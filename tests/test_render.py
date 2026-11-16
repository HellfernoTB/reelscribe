import json
from pathlib import Path

from reelscribe.render import build_markdown, make_stem, slugify
from reelscribe.transcribe import TranscriptResult

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ytdlp_reel.json").read_text())

ROWS = [(0.0, 2.0, "Bonjour"), (65.0, 70.0, "et voila")]


def _md(meta=None, result=None, **kw):
    return build_markdown(
        source="https://instagram.com/reel/ABC",
        meta=FIXTURE if meta is None else meta,
        result=result or TranscriptResult(text="Bonjour et voila", rows=ROWS, language="fr"),
        transcribed_at="2026-08-31T12:00:00",
        **kw,
    )


def test_section_order():
    md = _md()
    assert md.index("# ") < md.index("- Source:")
    assert md.index("## Caption") < md.index("## Transcript") < md.index("## Timestamped")


def test_source_is_normalized_url_not_webpage_url():
    md = _md()
    assert "- Source: https://instagram.com/reel/ABC" in md
    assert "igsi" not in md


def test_timestamps_use_mmss_and_wrap_past_a_minute():
    md = _md()
    assert "[00:00] Bonjour" in md
    assert "[01:05] et voila" in md


def test_missing_caption_omits_section():
    meta = {**FIXTURE, "description": ""}
    md = _md(meta=meta)
    assert "## Caption" not in md
    assert "## Transcript" in md


def test_missing_duration_shows_unknown():
    meta = {**FIXTURE, "duration": None}
    md = _md(meta=meta)
    assert "- Duration: unknown" in md


def test_zero_segments_writes_placeholder_not_crash():
    md = _md(result=TranscriptResult(text="", rows=[], language="fr"))
    assert "_(no speech detected)_" in md
    assert md.count("_(no speech detected)_") == 2  # transcript body + timestamped


def test_translation_present():
    md = _md(english="Hello everyone")
    assert "## English translation\n\nHello everyone" in md


def test_translation_absent_by_default():
    assert "## English translation" not in _md()


def test_note_rendered_in_header():
    md = _md(note="no speech detected in audio")
    assert "- Note: no speech detected in audio" in md


def test_title_falls_back_to_source_when_missing():
    md = _md(meta={**FIXTURE, "title": ""})
    assert md.startswith("# https://instagram.com/reel/ABC")


def test_slugify_handles_unicode_and_symbols():
    assert slugify("Café: 3 Projects!!!") == "café-3-projects"
    assert slugify("") == "untitled"


def test_make_stem():
    assert make_stem(FIXTURE) == "C9xYzAbCdEf-how-to-cook-the-perfect-omelette"


def test_build_record_structured_fields():
    from datetime import datetime, timezone

    from reelscribe.render import build_record

    when = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    rec = build_record(
        meta=FIXTURE, result=TranscriptResult(text="Bonjour et voila", rows=ROWS, language="fr"),
        english=" Hello ", note="n", model="small", transcribed_at=when,
    )
    assert rec["segments"] == [
        {"start": 0.0, "end": 2.0, "text": "Bonjour"},
        {"start": 65.0, "end": 70.0, "text": "et voila"},
    ]
    assert rec["english_text"] == "Hello" and rec["model"] == "small" and rec["note"] == "n"
    assert rec["text"] == "Bonjour et voila" and rec["language"] == "fr"
    assert rec["ytdlp_meta"] is FIXTURE and rec["transcribed_at"] == when
    assert rec["caption"] == FIXTURE["description"].strip()
