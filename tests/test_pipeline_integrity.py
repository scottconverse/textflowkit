"""Failure and data-integrity regressions for the shared pipeline."""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import PipelineError, transcribe
from textflowkit.render import write_all


def _fake_pipeline(monkeypatch, *, fail_engine: bool = False):
    from textflowkit.core import pipeline

    scratch_paths: list[Path] = []

    def fetch(ref, *, work_dir, **kwargs):
        scratch_paths.append(Path(work_dir))
        media = Path(work_dir) / "media.wav"
        media.write_bytes(Path(ref.location).read_bytes())
        return media

    def extract(media, *, work_dir, check_cancel=None):
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(media.read_bytes())
        return audio

    class Engine:
        def transcribe(self, audio, *, language=None):
            if fail_engine:
                raise RuntimeError("model failed")
            return Transcript(
                source=str(audio), language=language or "en",
                segments=[Segment(0, 1, audio.read_text(encoding="utf-8"))],
            )

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    return scratch_paths


def test_same_basename_sources_keep_distinct_outputs(tmp_path, monkeypatch):
    scratch_paths = _fake_pipeline(monkeypatch)
    sources = [tmp_path / "one" / "meeting.wav", tmp_path / "two" / "meeting.wav"]
    for source, content in zip(sources, ("alpha", "beta"), strict=True):
        source.parent.mkdir()
        source.write_text(content, encoding="utf-8")

    results = [transcribe(str(source), formats=["txt"], output_dir=tmp_path)
               for source in sources]

    first, second = [result.outputs[0] for result in results]
    assert first != second
    assert first.read_text(encoding="utf-8").strip() == "alpha"
    assert second.read_text(encoding="utf-8").strip() == "beta"
    assert all(not path.exists() for path in scratch_paths)


def test_engine_failure_cleans_scratch(tmp_path, monkeypatch):
    scratch_paths = _fake_pipeline(monkeypatch, fail_engine=True)
    source = tmp_path / "meeting.wav"
    source.write_text("alpha", encoding="utf-8")

    with pytest.raises(PipelineError, match="model failed"):
        transcribe(str(source), formats=["txt"], output_dir=tmp_path)

    assert len(scratch_paths) == 1
    assert not scratch_paths[0].exists()


def test_format_validation_precedes_any_output(tmp_path):
    tr = Transcript(source="x", segments=[Segment(0, 1, "hello")])
    with pytest.raises(ValueError, match="unsupported format"):
        write_all(tr, formats=["txt", "bogus"], output_dir=tmp_path, stem="talk")
    assert not (tmp_path / "talk.txt").exists()


def test_atomic_write_never_replaces_existing_file(tmp_path):
    tr = Transcript(source="x", segments=[Segment(0, 1, "hello")])
    existing = tmp_path / "talk.txt"
    existing.write_text("owner data", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_all(tr, formats=["txt"], output_dir=tmp_path, stem="talk")

    assert existing.read_text(encoding="utf-8") == "owner data"
    assert not list(tmp_path.glob("*.tmp"))


def test_selftest_reports_engine_failure(monkeypatch):
    from textflowkit import cli

    class Matrix:
        shape = (512, 512)

        def __matmul__(self, other):
            return self

    torch = SimpleNamespace(
        __version__="test", version=SimpleNamespace(hip=None),
        cuda=SimpleNamespace(is_available=lambda: False),
        device=lambda name: name, randn=lambda *args, **kwargs: Matrix(),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    observed: list[Path] = []

    class FailingEngine:
        def transcribe(self, wav):
            observed.append(Path(wav))
            raise RuntimeError("test engine failure")

    monkeypatch.setattr(cli, "get_engine", lambda *args, **kwargs: FailingEngine())
    assert cli.main(["selftest", "--model", "tiny"]) == 1
    assert len(observed) == 1
    assert observed[0].exists()  # bundled fixture, not a disposable scratch file


def test_selftest_rejects_empty_transcription(monkeypatch, capsys):
    from textflowkit import cli

    class Matrix:
        shape = (512, 512)

        def __matmul__(self, other):
            return self

    torch = SimpleNamespace(
        __version__="test", version=SimpleNamespace(hip=None),
        cuda=SimpleNamespace(is_available=lambda: False),
        device=lambda name: name, randn=lambda *args, **kwargs: Matrix(),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(cli, "get_engine", lambda *a, **k: SimpleNamespace(
        transcribe=lambda wav: Transcript(source=str(wav), segments=[], metadata={})
    ))
    assert cli.main(["selftest"]) == 1
    assert "no nonempty timed speech segments" in capsys.readouterr().out


@pytest.mark.parametrize("has_speech", [True, False])
def test_duration_covers_trailing_silence_or_empty_speech(tmp_path, monkeypatch, has_speech):
    from textflowkit.core import pipeline

    source = tmp_path / "clip.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000 * 3)

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda ref, **kwargs: source)
    monkeypatch.setattr(pipeline, "extract_audio", lambda media, **kwargs: media)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: SimpleNamespace(
        transcribe=lambda audio, **kwargs: Transcript(
            source=str(audio), segments=[Segment(0, 1, "hi")] if has_speech else []
        )
    ))
    result = transcribe(str(source), formats=["txt"])
    assert result.transcript.duration == 3.0
