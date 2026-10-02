"""A resumed job that decodes retained local media is still confined input.

`transcribe` derives the decode restriction from the *source* (a local file
under a configured input root), not from which branch acquired the media. On the
resume path (`elif diarize and audio is None`) a checkpoint can supply a
`media_path` that still exists, so the re-staging block is skipped and the media
handled is the checkpoint's. If the restriction were derived only inside that
block, this path would decode a retained local file with no decoder boundary at
all - under an input root, which is exactly when the boundary is supposed to
apply.

These tests drive the real `transcribe` entry point with a fake engine and a
fake decode step that records the `confined` flag it was given, so they assert
the *decision*, not ffmpeg's behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textflowkit.core import pipeline
from textflowkit.core.checkpoint import CHECKPOINT_VERSION, local_source_identity
from textflowkit.core.model import Segment, Transcript


class _CountingEngine:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio, *, language=None) -> Transcript:
        self.calls += 1
        return Transcript(
            source=str(audio),
            language=language or "en",
            segments=[Segment(0.0, 1.0, "hello")],
        )


def _resumed_checkpoint(*, source, media_path, input_root=None, audio_path=None):
    """A checkpoint that has a finished transcript but no usable audio.

    The transcript is what makes `can_resume` true; the absent audio is what
    sends the run into the diarization re-acquisition branch, with `media`
    already resolved from the checkpoint. The fingerprint is real, because a
    local resume fails closed without one.
    """
    transcript = Transcript(
        source=source, language="en", segments=[Segment(0.0, 1.0, "hello")]
    )
    identity = local_source_identity(source, input_root=input_root)
    return {
        "version": CHECKPOINT_VERSION,
        "source": source,
        "model": "small",
        "engine": "whisper",
        "options": {"formats": ["json"], "diarize": True,
                    "diarizer_backend": "pyannote", "translate_to": None,
                    "translator_backend": "ollama"},
        "finished_stages": ["fetch", "extract", "transcribe"],
        "transcript": transcript.to_dict(),
        "media_path": str(media_path) if media_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "local_identity": identity,
    }


@pytest.fixture
def recorded_decode(tmp_path, monkeypatch):
    """A local source under an input root, and a decode step that records flags."""
    root = tmp_path / "allowed"
    root.mkdir()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    engine = _CountingEngine()
    seen: list[bool] = []

    def extract(media, *, work_dir, check_cancel=None, confined=False):
        seen.append(confined)
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(b"audio")
        return audio

    class _Diarizer:
        name = "test"

        def diarize(self, audio):
            return []

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)
    monkeypatch.setattr(pipeline, "get_diarizer", lambda *a, **k: _Diarizer())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(root))
    return root, output_dir, seen


def test_resumed_media_under_an_input_root_decodes_confined(tmp_path, recorded_decode):
    """RED before the fix: a retained local checkpoint media was decoded unconfined.

    The media path is supplied by the checkpoint and exists, so the re-staging
    block is skipped. The decode must still be restricted, because the source is
    a local file and an input root is configured.
    """
    root, output_dir, seen = recorded_decode
    media = root / "retained.wav"
    media.write_bytes(b"RIFF....WAVEfmt ")

    pipeline.transcribe(
        str(media),
        formats=["json"],
        output_dir=output_dir,
        work_dir=tmp_path / "work",
        input_root=root,
        diarize=True,
        resume_checkpoint=_resumed_checkpoint(
            source=str(media), media_path=media, input_root=root, audio_path=None
        ),
    )

    assert seen == [True], f"decode ran with confined={seen}; expected [True]"


def test_resumed_media_without_an_input_root_decodes_unconfined(tmp_path, recorded_decode):
    """The restriction follows the input root, not the resume path.

    Same retained media, no configured root: there is no boundary, and the
    documented unconfined workflow must keep decoding as before.
    """
    _root, output_dir, seen = recorded_decode
    outside = tmp_path / "outside"
    outside.mkdir()
    media = outside / "free.wav"
    media.write_bytes(b"RIFF....WAVEfmt ")

    import os

    os.environ.pop("TEXTFLOWKIT_INPUT_ROOT", None)
    pipeline.transcribe(
        str(media),
        formats=["json"],
        output_dir=output_dir,
        work_dir=tmp_path / "work",
        input_root=None,
        diarize=True,
        resume_checkpoint=_resumed_checkpoint(
            source=str(media), media_path=media, audio_path=None
        ),
    )

    assert seen == [False], f"decode ran with confined={seen}; expected [False]"
