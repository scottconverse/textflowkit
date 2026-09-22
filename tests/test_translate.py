"""Transcript translation.

Guarantees that matter:

- the result length always matches the input, so a misbehaving backend cannot
  shift text onto the wrong segment
- an unreachable backend raises; it never returns source text as a translation
- a malformed batch falls back to per-segment rather than being trusted

The live Ollama path is exercised only when a server is reachable; CI has none,
so that test skips rather than failing.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from textflowkit.core.model import Segment
from textflowkit.core.translate import (
    BATCH_SIZE,
    ENV_OLLAMA_HOST,
    OllamaTranslator,
    TranslationError,
    get_translator,
    translate_segments,
)


@pytest.fixture(autouse=True)
def configured_local_translation_model(monkeypatch, request):
    if request.node.name != "test_live_ollama_translates":
        monkeypatch.setenv("TEXTFLOWKIT_TRANSLATE_MODEL", "test-local-model")


def test_translation_refuses_to_choose_a_cloud_model(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_TRANSLATE_MODEL", raising=False)
    with pytest.raises(TranslationError, match="explicit model"):
        OllamaTranslator()


def test_translation_reports_cloud_and_remote_routes():
    assert OllamaTranslator(model="chosen:cloud").route == "cloud model"
    assert OllamaTranslator(model="local-model").route == "local Ollama"
    assert OllamaTranslator(model="model", host="https://other.example").route == "remote Ollama host"


class FakeTranslator:
    name = "fake"

    def __init__(self, mapping=None, fail: bool = False, wrong_length: bool = False):
        self.mapping = mapping or {}
        self.fail = fail
        self.wrong_length = wrong_length
        self.calls: list[list[str]] = []

    def translate(self, texts, target):
        self.calls.append(list(texts))
        if self.fail:
            raise TranslationError("backend down")
        if self.wrong_length:
            return ["only one"]
        return [self.mapping.get(t, f"[{target}]{t}") for t in texts]


# --- translate_segments ---------------------------------------------------

def test_translate_segments_fills_translated_text():
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "world")]
    n = translate_segments(segs, "es", translator=FakeTranslator({"hello": "hola", "world": "mundo"}))
    assert n == 2
    assert [s.translated_text for s in segs] == ["hola", "mundo"]


def test_translate_segments_keeps_source_text():
    segs = [Segment(0, 1, "hello")]
    translate_segments(segs, "es", translator=FakeTranslator())
    assert segs[0].text == "hello"          # source preserved
    assert segs[0].translated_text is not None


def test_translate_segments_leaves_blank_alone():
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "   ")]
    n = translate_segments(segs, "es", translator=FakeTranslator())
    assert n == 1
    assert segs[1].translated_text is None


def test_translate_segments_rejects_wrong_length():
    """A length mismatch must raise, not silently misalign text."""
    segs = [Segment(0, 1, "a"), Segment(1, 2, "b")]
    with pytest.raises(TranslationError):
        translate_segments(segs, "es", translator=FakeTranslator(wrong_length=True))


def test_translate_segments_propagates_backend_failure():
    segs = [Segment(0, 1, "a")]
    with pytest.raises(TranslationError):
        translate_segments(segs, "es", translator=FakeTranslator(fail=True))


def test_display_text_prefers_translation():
    from textflowkit.core.model import Transcript

    tr = Transcript(source="s", segments=[Segment(0, 1, "hello", translated_text="hola")])
    assert "hola" in tr.text
    assert "hello" not in tr.text


# --- OllamaTranslator internals -------------------------------------------

def test_batch_parses_numbered_response(monkeypatch):
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1. uno\n2. dos\n3. tres")
    assert t._translate_batch(["one", "two", "three"], "es") == ["uno", "dos", "tres"]


def test_batch_rejects_missing_items(monkeypatch):
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1. uno")
    with pytest.raises(TranslationError):
        t._translate_batch(["one", "two"], "es")


def test_batch_rejects_empty_response(monkeypatch):
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "")
    with pytest.raises(TranslationError):
        t._translate_batch(["one"], "es")


def test_batch_accepts_various_number_formats(monkeypatch):
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1) uno\n2: dos\n3- tres")
    assert t._translate_batch(["a", "b", "c"], "es") == ["uno", "dos", "tres"]


def test_falls_back_to_per_segment_on_bad_batch(monkeypatch):
    """A malformed batch must not be trusted - redo it one at a time."""
    t = OllamaTranslator()
    calls = {"batches": 0, "singles": 0}

    def fake_generate(prompt):
        if "\n" in prompt and prompt.count("\n") > 2:
            calls["batches"] += 1
            return "garbage that will not parse"
        calls["singles"] += 1
        return "translated"

    monkeypatch.setattr(t, "_generate", fake_generate)
    out = t.translate(["a", "b", "c"], "es")
    assert out == ["translated", "translated", "translated"]
    assert calls["batches"] == 1
    assert calls["singles"] == 3


def test_repeated_text_is_translated_once_per_batch(monkeypatch):
    """Identical text in one batch costs one round trip, not three."""
    t = OllamaTranslator()
    calls = {"n": 0}

    def fake_generate(prompt):
        calls["n"] += 1
        # one numbered line per requested item, so the batch parses
        return "\n".join(f"{i}. translated" for i in range(1, 4))

    monkeypatch.setattr(t, "_generate", fake_generate)
    out = t.translate(["same", "same", "same"], "es")
    assert out == ["translated"] * 3
    assert calls["n"] == 1


def test_translation_is_cached_across_calls(monkeypatch):
    """A phrase seen again in a later call is not retranslated."""
    t = OllamaTranslator()
    calls = {"n": 0}

    def fake_generate(prompt):
        calls["n"] += 1
        return "1. translated"

    monkeypatch.setattr(t, "_generate", fake_generate)
    assert t.translate(["same"], "es") == ["translated"]
    assert calls["n"] == 1

    # second call: cache hit, no further round trips
    assert t.translate(["same"], "es") == ["translated"]
    assert calls["n"] == 1


def test_blank_target_rejected():
    with pytest.raises(ValueError):
        OllamaTranslator().translate(["a"], "   ")


def test_unreachable_host_raises_actionable(monkeypatch):
    monkeypatch.setenv(ENV_OLLAMA_HOST, "http://127.0.0.1:1")  # nothing listening
    t = OllamaTranslator(timeout=2.0)
    with pytest.raises(TranslationError) as exc:
        t.translate(["hello"], "es")
    msg = str(exc.value)
    assert "could not reach Ollama" in msg
    assert ENV_OLLAMA_HOST in msg


def test_unknown_backend_rejected():
    with pytest.raises(TranslationError):
        get_translator("nope")


def test_batch_size_is_sane():
    assert 1 <= BATCH_SIZE <= 100


# --- pipeline integration -------------------------------------------------

def test_pipeline_translate_failure_is_loud(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.engine import WhisperEngine
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import PipelineError, transcribe

    media = tmp_path / "clip.wav"
    media.write_bytes(b"fake")
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: media)
    monkeypatch.setattr(
        WhisperEngine,
        "transcribe",
        lambda self, audio, *, language=None, **kw: Transcript(
            source=str(audio), language="en", segments=[Segment(0, 1, "hello")]
        ),
    )

    class Boom:
        name = "boom"

        def translate(self, texts, target):
            raise TranslationError("no backend")

    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: Boom())

    with pytest.raises(PipelineError) as exc:
        transcribe(str(media), input_root=tmp_path, translate_to="es")
    assert "translation" in str(exc.value).lower()
    assert "no backend" in str(exc.value)


def test_pipeline_records_translation_metadata(monkeypatch, tmp_path):
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
            source=str(audio),
            language="en",
            segments=[Segment(0, 1, "hello"), Segment(1, 2, "world")],
        ),
    )
    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: FakeTranslator())

    result = transcribe(str(media), input_root=tmp_path, translate_to="es")
    meta = result.transcript.metadata["translation"]
    assert meta["target"] == "es"
    assert meta["segments_translated"] == 2
    assert all(s.translated_text for s in result.transcript.segments)


# --- live Ollama (skipped when unavailable) -------------------------------

def _ollama_reachable() -> bool:
    host = os.environ.get(ENV_OLLAMA_HOST, "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="no local Ollama server")
def test_live_ollama_translates():
    """Real backend smoke test. CI has no Ollama, so this skips there."""
    if not os.environ.get("TEXTFLOWKIT_TRANSLATE_MODEL"):
        pytest.skip("no explicit translation model configured")
    t = OllamaTranslator(timeout=300.0)
    out = t.translate(["Hello, good morning."], "Spanish")
    assert out and out[0].strip()
    assert out[0].strip().lower() != "hello, good morning."
    print("live translation:", out[0])


# --- over real HTTP, against the stub (runs in CI) ------------------------

from ollama_stub import OllamaStub


def test_translates_over_real_http():
    """The transport is exercised for real, not mocked at the function level."""
    with OllamaStub() as stub:
        t = OllamaTranslator(host=stub.host, timeout=10)
        out = t.translate(["hello", "world"], "es")
    assert out == ["x-hello", "x-world"]


def test_batching_is_one_round_trip_over_the_wire():
    with OllamaStub() as stub:
        t = OllamaTranslator(host=stub.host, timeout=10)
        t.translate([f"line{i}" for i in range(5)], "es")
        assert len(stub.calls) == 1, "five short lines should be one batch"


def test_garbage_response_falls_back_over_the_wire():
    """A stub that ignores the format must trigger per-segment retries."""
    with OllamaStub(mode="garbage") as stub:
        t = OllamaTranslator(host=stub.host, timeout=10)
        out = t.translate(["a", "b", "c"], "es")
        # 1 failed batch + 1 per item
        assert len(stub.calls) == 4
    assert len(out) == 3


def test_http_error_is_actionable():
    with OllamaStub() as stub:
        t = OllamaTranslator(host=stub.host, timeout=10)
        with pytest.raises(TranslationError) as exc:
            t.translate(["please reject this"], "es")
    assert "404" in str(exc.value) or "rejected" in str(exc.value)


def test_model_name_is_forwarded():
    with OllamaStub() as stub:
        t = OllamaTranslator(model="my-model:tag", host=stub.host, timeout=10)
        t.translate(["hi"], "es")
    assert stub.calls[0]["model"] == "my-model:tag"


def test_pipeline_translates_end_to_end_over_http(monkeypatch, tmp_path):
    """Full pipeline: stub transcribe -> real HTTP translation -> transcript."""
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
            source=str(audio),
            language="en",
            segments=[Segment(0, 1, "hello"), Segment(1, 2, "world")],
        ),
    )

    with OllamaStub() as stub:
        monkeypatch.setenv(ENV_OLLAMA_HOST, stub.host)
        result = transcribe(str(media), input_root=tmp_path, translate_to="Spanish")

    assert [s.translated_text for s in result.transcript.segments] == ["x-hello", "x-world"]
    assert result.transcript.metadata["translation"]["segments_translated"] == 2
    assert len(stub.calls) == 1   # the whole pipeline batched it
