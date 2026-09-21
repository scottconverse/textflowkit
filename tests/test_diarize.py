"""Speaker diarization.

The important guarantees here are about *honesty*, not model quality:

- assignment is deterministic and refuses to guess when there is no overlap
- a missing backend or token fails with an actionable error
- the pipeline never returns a transcript with empty speakers when diarization
  was requested - that silent no-op is the defect this replaced
"""

from __future__ import annotations

import pytest

from textflowkit.core.diarize import (
    ENV_HF_TOKEN,
    DiarizationError,
    PyannoteDiarizer,
    SpeakerTurn,
    assign_speakers,
    get_diarizer,
    overlap,
)
from textflowkit.core.model import Segment

# --- overlap --------------------------------------------------------------

def test_overlap_basic():
    assert overlap(0, 10, 5, 15) == 5


def test_overlap_disjoint_is_zero():
    assert overlap(0, 5, 10, 15) == 0


def test_overlap_contained():
    assert overlap(0, 10, 2, 4) == 2


def test_overlap_touching_edges_is_zero():
    assert overlap(0, 5, 5, 10) == 0


# --- assignment -----------------------------------------------------------

def test_assign_picks_max_overlap():
    segs = [Segment(start=0.0, end=10.0, text="a")]
    turns = [
        SpeakerTurn(start=0, end=2, speaker="A"),
        SpeakerTurn(start=2, end=10, speaker="B"),
    ]
    assert assign_speakers(segs, turns) == 1
    assert segs[0].speaker == "B"


def test_assign_is_deterministic_on_ties():
    """Equal overlap must not depend on iteration order."""
    segs = [Segment(start=0.0, end=10.0, text="a")]
    turns_a = [
        SpeakerTurn(start=0, end=5, speaker="A"),
        SpeakerTurn(start=5, end=10, speaker="B"),
    ]
    turns_b = list(reversed(turns_a))
    assign_speakers(segs, turns_a)
    first = segs[0].speaker
    segs[0].speaker = None
    assign_speakers(segs, turns_b)
    assert segs[0].speaker == first


def test_assign_leaves_uncovered_segment_unlabelled():
    """No overlap means no guess."""
    segs = [Segment(start=100.0, end=110.0, text="far away")]
    turns = [SpeakerTurn(start=0, end=10, speaker="A")]
    assert assign_speakers(segs, turns) == 0
    assert segs[0].speaker is None


def test_assign_multiple_segments():
    segs = [
        Segment(start=0.0, end=4.0, text="one"),
        Segment(start=4.0, end=8.0, text="two"),
        Segment(start=8.0, end=12.0, text="three"),
    ]
    turns = [
        SpeakerTurn(start=0, end=5, speaker="A"),
        SpeakerTurn(start=5, end=12, speaker="B"),
    ]
    assert assign_speakers(segs, turns) == 3
    assert [s.speaker for s in segs] == ["A", "B", "B"]


def test_assign_empty_turns_labels_nothing():
    segs = [Segment(start=0.0, end=1.0, text="x")]
    assert assign_speakers(segs, []) == 0
    assert segs[0].speaker is None


def test_assign_respects_min_overlap():
    segs = [Segment(start=0.0, end=10.0, text="x")]
    turns = [SpeakerTurn(start=0, end=0.1, speaker="A")]
    assert assign_speakers(segs, turns, min_overlap=1.0) == 0
    assert segs[0].speaker is None


# --- backend failure is loud ----------------------------------------------

def test_get_diarizer_unknown_backend():
    with pytest.raises(DiarizationError):
        get_diarizer("nope")


def test_pyannote_missing_dependency_is_actionable(monkeypatch):
    monkeypatch.setenv(ENV_HF_TOKEN, "fake-token")
    d = PyannoteDiarizer()
    try:
        d._load()
    except DiarizationError as exc:
        msg = str(exc)
        # either the extra is missing, or pyannote resolved but the model is
        # gated - both are acceptable, and both must name what to do
        assert "diarize" in msg or "token" in msg or "load" in msg
    else:
        pytest.skip("pyannote is installed and the model loaded")


def test_pyannote_missing_token_is_actionable(monkeypatch):
    monkeypatch.delenv(ENV_HF_TOKEN, raising=False)
    d = PyannoteDiarizer()
    # Force the "dependency present" branch so the token check is what fires.
    monkeypatch.setattr(d, "_pipeline", None)
    import sys
    import types

    fake = types.ModuleType("pyannote.audio")
    fake.Pipeline = object()
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake)

    with pytest.raises(DiarizationError) as exc:
        d._load()
    assert ENV_HF_TOKEN in str(exc.value)


# --- the pipeline refuses to be silent ------------------------------------

def test_pipeline_diarize_failure_is_not_silent(monkeypatch, tmp_path):
    """Requesting diarization with a broken backend must FAIL, not return
    a transcript that quietly has no speakers."""
    from textflowkit.core import pipeline
    from textflowkit.core.pipeline import PipelineError, transcribe

    media = tmp_path / "clip.wav"
    media.write_bytes(b"fake")
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: media)

    from textflowkit.core.engine import WhisperEngine
    from textflowkit.core.model import Transcript

    monkeypatch.setattr(
        WhisperEngine,
        "transcribe",
        lambda self, audio, *, language=None, **kw: Transcript(
            source=str(audio), language="en", segments=[Segment(0.0, 1.0, "hi")]
        ),
    )

    class Boom:
        name = "boom"

        def diarize(self, audio):
            raise DiarizationError("no token configured")

    monkeypatch.setattr(pipeline, "get_diarizer", lambda *a, **k: Boom())

    with pytest.raises(PipelineError) as exc:
        transcribe(str(media), input_root=tmp_path, diarize=True)
    assert "diarization" in str(exc.value).lower()
    assert "no token configured" in str(exc.value)


def test_pipeline_records_diarization_metadata(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.engine import WhisperEngine
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import transcribe

    media = tmp_path / "clip.wav"
    media.write_bytes(b"fake")
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: media)

    def fake_transcribe(self, audio, *, language=None, **kw):
        return Transcript(
            source=str(audio),
            language="en",
            segments=[Segment(0.0, 10.0, "hello"), Segment(10.0, 20.0, "world")],
        )

    monkeypatch.setattr(WhisperEngine, "transcribe", fake_transcribe)

    class FakeDiarizer:
        name = "fake"

        def diarize(self, audio):
            return [
                SpeakerTurn(0, 10, "SPEAKER_00"),
                SpeakerTurn(10, 20, "SPEAKER_01"),
            ]

    monkeypatch.setattr(pipeline, "get_diarizer", lambda *a, **k: FakeDiarizer())

    result = transcribe(str(media), input_root=tmp_path, diarize=True)
    speakers = [s.speaker for s in result.transcript.segments]
    assert speakers == ["SPEAKER_00", "SPEAKER_01"]
    meta = result.transcript.metadata["diarization"]
    assert meta["segments_labelled"] == 2
    assert meta["speakers"] == ["SPEAKER_00", "SPEAKER_01"]


def test_pipeline_without_diarize_leaves_speakers_empty(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.engine import WhisperEngine
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import transcribe

    media = tmp_path / "clip.wav"
    media.write_bytes(b"fake")
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: media)

    monkeypatch.setattr(
        WhisperEngine,
        "transcribe",
        lambda self, audio, *, language=None, **kw: Transcript(
            source=str(audio), language="en", segments=[Segment(0.0, 1.0, "hi")]
        ),
    )
    result = transcribe(str(media), input_root=tmp_path)
    assert result.transcript.segments[0].speaker is None
    assert "diarization" not in result.transcript.metadata
