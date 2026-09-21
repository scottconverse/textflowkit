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
    t = OllamaTranslator(timeout=300.0)
    out = t.translate(["Hello, good morning."], "Spanish")
    assert out and out[0].strip()
    assert out[0].strip().lower() != "hello, good morning."
    print("live translation:", out[0])
