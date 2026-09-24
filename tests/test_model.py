"""Canonical model round-trip and rendering."""

import json
from dataclasses import dataclass

from textflowkit.core.model import Segment, Transcript, WordTiming


def sample() -> Transcript:
    return Transcript(
        source="https://example.com/x",
        language="en",
        platform="youtube",
        duration=12.5,
        engine="openai-whisper",
        segments=[
            Segment(0.0, 2.5, " Hello there. ", speaker="A"),
            Segment(2.5, 6.0, "General Kenobi.", speaker="B"),
            Segment(6.0, 12.5, "you are a bold one", translated_text="eres audaz"),
        ],
    )


def test_roundtrip_json():
    tr = sample()
    tr.segments[0].words = [WordTiming(0.0, 0.4, "Hello"), WordTiming(0.5, 1.0, "there.")]
    restored = Transcript.from_json(tr.to_json())
    assert restored.source == tr.source
    assert restored.language == "en"
    assert restored.platform == "youtube"
    assert len(restored.segments) == 3
    assert restored.segments[2].translated_text == "eres audaz"
    assert restored.segments[0].words == tr.segments[0].words


def test_legacy_transcript_without_words_still_loads():
    data = sample().to_dict()
    for segment in data["segments"]:
        segment.pop("words")
    assert all(not s.words for s in Transcript.from_dict(data).segments)


def test_text_uses_translation_when_present():
    tr = sample()
    assert "eres audaz" in tr.text
    assert "you are a bold one" not in tr.text


def test_hidden_segments_excluded():
    tr = sample()
    tr.segments[0].hidden = True
    assert "Hello there" not in tr.text


def test_save_and_load(tmp_path):
    tr = sample()
    p = tr.save_json(tmp_path / "t.json")
    assert p.exists()
    again = Transcript.load_json(p)
    assert len(again.segments) == 3


@dataclass(slots=True)
class FutureSegment(Segment):
    """Stand-in for the next field added to Segment (issue #16 / finding A15).

    Nothing else in this repository knows about `quality`. The compact response
    is only supposed to drop `words`, so a field added after the compact path
    was written has to survive it. This subclass is that field addition made
    executable, rather than an imagined one.
    """

    quality: float = 0.9


def test_compact_segment_dict_drops_only_words():
    segment = FutureSegment(0.0, 1.0, "hi", words=[WordTiming(0.0, 0.5, "hi")])

    full = segment.to_dict()
    compact = segment.to_dict(include_words=False)

    # Control: the surrogate really does carry a field this module never names,
    # so the assertions below cannot pass by the field simply not existing.
    assert full["quality"] == 0.9
    assert compact["quality"] == 0.9
    assert "words" not in compact
    assert compact == {key: value for key, value in full.items() if key != "words"}


def test_compact_transcript_dict_drops_only_words():
    tr = Transcript(source="s", segments=[FutureSegment(0.0, 1.0, "hi")])

    compact = tr.to_dict(include_words=False)
    assert compact["segments"][0]["quality"] == 0.9
    assert "words" not in compact["segments"][0]

    # to_json is what the MCP adapter renders; it must agree with to_dict.
    rendered = json.loads(tr.to_json(include_words=False))
    assert rendered["segments"][0]["quality"] == 0.9
    assert "words" not in rendered["segments"][0]
