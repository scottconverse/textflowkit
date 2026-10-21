"""Sixdesk ENG01/02: no external models or transport."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from textflowkit.core.checkpoint import CheckpointRecord
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import PipelineError, transcribe
from textflowkit.core.translate import OllamaTranslator, TranslationError, translate_segments


def checkpoint(**changes):
    args = {
        "source": "https://example.com/a.mp4", "model": "whistle", "engine": "whistle",
        "finished_stages": ["source", "acquire", "extract", "transcribe"],
        "transcript": Transcript(source="https://example.com/a.mp4",
            segments=[Segment(0, 1, "original")], engine="whistle").to_dict(),
    }
    args.update(changes)
    return CheckpointRecord(**args).to_dict()


@pytest.mark.parametrize("changes,completed", [
    ({"source": "https://example.com/b.mp4"}, True),
    ({"model": "other-model"}, True), ({"language": "es"}, True),
    ({"engine": "whisper"}, True),
    ({"source": "https://example.com/b.mp4"}, False),
    ({"model": "other-model"}, False), ({"language": "es"}, False),
])
def test_direct_pipeline_refuses_wrong_checkpoint_before_writes(monkeypatch, tmp_path, changes, completed):
    cp = checkpoint(**changes)
    if not completed:
        cp["transcript"] = None
        cp["finished_stages"] = ["source"]
        # Partial progress is a Whistle-only checkpoint form. Keep it valid so
        # the test reaches pipeline identity matching rather than parse rejection.
        cp["engine_progress"] = {
            "schema": "textflowkit.whistle.progress/1", "policy": "p",
            "wav_identity": "a" * 64, "model_sha256": "b" * 64,
            "binary_sha256": "c" * 64, "duration": 2.0,
            "language": None, "completed_core_index": 0, "segments": [],
        }
    monkeypatch.setattr("textflowkit.core.pipeline.resolve_source",
                        lambda _: SimpleNamespace(kind="url", platform="generic"))
    fetch = Mock(side_effect=AssertionError("must not acquire mismatched media"))
    monkeypatch.setattr("textflowkit.core.pipeline.fetch_media", fetch)
    sink = Mock()
    with pytest.raises(PipelineError, match="match.*checkpoint"):
        transcribe("https://example.com/a.mp4", resume_checkpoint=cp,
                   on_checkpoint=sink, work_dir=tmp_path)
    sink.assert_not_called()
    fetch.assert_not_called()


def test_direct_pipeline_matching_alias_hydrates_and_allows_optional_changes(monkeypatch, tmp_path):
    monkeypatch.setattr("textflowkit.core.pipeline.resolve_source",
                        lambda _: SimpleNamespace(kind="url", platform="generic"))
    monkeypatch.setattr("textflowkit.core.pipeline.fetch_media",
                        Mock(side_effect=AssertionError("should reuse transcript")))
    class Translator:
        name = "fake"
        route = "test"
        model = "test"
        def translate(self, texts, target):
            return ["translated" for _ in texts]
    monkeypatch.setattr("textflowkit.core.pipeline.get_translator", lambda _: Translator())
    snapshots = []
    result = transcribe("https://example.com/a.mp4", engine="default", formats=["json"],
                       resume_checkpoint=checkpoint(), translate_to="es",
                       on_checkpoint=snapshots.append, work_dir=tmp_path)
    assert result.transcript.segments[0].translated_text == "translated"
    assert snapshots[0]["transcript"]["segments"][0]["text"] == "original"
    assert "translate" in snapshots[-1]["finished_stages"]


@pytest.mark.parametrize("envelope", [
    {"done": True, "done_reason": "length"},
    {"done": False, "done_reason": "stop"},
    {"done": True}, {}, {"done": 1, "done_reason": "stop"},
    {"done": True, "done_reason": "error"},
])
@pytest.mark.parametrize("single", [True, False])
def test_incomplete_generation_never_accepted_cached_or_fallback(monkeypatch, envelope, single):
    body = dict(envelope, response="cort" if single else "1. uno\n2. cort")
    transport = Mock(side_effect=lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr("urllib.request.urlopen", transport)
    translator = OllamaTranslator(model="test-local")
    segments = [Segment(0, 1, "one"), Segment(1, 2, "two")]
    with pytest.raises(TranslationError):
        if single:
            translator._translate_one("one", "es")
        else:
            translate_segments(segments, "es", translator=translator)
    assert transport.call_count == 1, "terminal failure must not be hidden by fallback"
    assert not translator._cache
    assert all(s.translated_text is None for s in segments)


@pytest.mark.parametrize("body", [[], None, {"done": True, "done_reason": "stop", "response": None}])
def test_invalid_completion_envelope_is_translation_error(monkeypatch, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    with pytest.raises(TranslationError):
        OllamaTranslator(model="test-local").translate(["one"], "es")


def test_successful_completion_cached(monkeypatch):
    body = {"done": True, "done_reason": "stop", "response": "1. uno\n2. dos"}
    transport = Mock(side_effect=lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr("urllib.request.urlopen", transport)
    translator = OllamaTranslator(model="test-local")
    assert translator.translate(["one", "two"], "es") == ["uno", "dos"]
    assert translator.translate(["one", "two"], "es") == ["uno", "dos"]
    assert transport.call_count == 1


def test_pipeline_incomplete_translation_never_marks_checkpoint_complete(monkeypatch, tmp_path):
    monkeypatch.setattr("textflowkit.core.pipeline.resolve_source",
                        lambda _: SimpleNamespace(kind="url", platform="generic"))
    body = {"done": True, "done_reason": "length", "response": "1. cort"}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    translator = OllamaTranslator(model="test-local")
    monkeypatch.setattr("textflowkit.core.pipeline.get_translator", lambda _: translator)
    snapshots = []
    with pytest.raises(PipelineError, match="translation"):
        transcribe("https://example.com/a.mp4", resume_checkpoint=checkpoint(),
                   translate_to="es", on_checkpoint=snapshots.append, work_dir=tmp_path)
    assert snapshots
    assert all("translate" not in cp["finished_stages"] for cp in snapshots)
    assert all("postprocess" not in cp["finished_stages"] for cp in snapshots)
    assert all(cp["transcript"]["segments"][0]["translated_text"] is None for cp in snapshots)
    assert not translator._cache
