"""Translation completeness (G4A / ENG007) - RED.

The defect this file pins, in the coordinator's words: `_translate_batch`
verifies numbering and cardinality and then accepts *empty numbered text*;
`translate_segments` checks the result count and then, for a nonblank source,
ignores a blank returned text and still marks the remainder "translated" with
incomplete segment results; and the pipeline stores its translation metadata and
its finished-stage markers even though items are missing.

The engine decision already encoded in this module is that a bad batch must not
be *trimmed* - it is refused, and the caller's per-item fallback recovers the
text (`_translate_batch`'s unnumbered-line refusal, the duplicate and
out-of-range refusals, and the fallback loop in `translate`). A blank *answer*
for a nonblank item is the same class of failure: content the model failed to
produce, not a line to silently drop. So it must take the same recovery.

What is asserted, and why each is legitimate:

- A batch that numbers every line but returns a blank answer for one of them is
  refused, and the *existing* fallback recovers the item through
  `_translate_one`; the delivered item is complete and the blank is never
  cached. This is the same recovery the malformed-batch tests already rely on.
- A fallback that itself returns blank for a nonblank source fails loudly rather
  than delivering a blank as a translation. It must not be swallowed into an
  "empty translation is fine" success.
- The backend may still return a list of the *right length* whose entries are
  incomplete; `translate_segments` must fail loudly *before* it mutates any
  segment, so no segment is left half-translated and - in the pipeline - no
  translation metadata is written and no `translate` / `postprocess`
  finished-stage marker is recorded.

What is deliberately *not* asserted, because it would be a false error:

- An empty source segment may legitimately come back blank. Blank segments are
  left alone and are not evidence of a missing translation.
- `source == translation` is not, by itself, an error: proper nouns, names, and
  same-language spans legitimately translate to themselves.

No network and no real LLM is consulted. Every model answer is a deterministic
fake supplied through monkeypatch; the batching, fallback, caching, and
numbering/cardinality logic run for real.
"""

from __future__ import annotations

import pytest

from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.core.translate import (
    OllamaTranslator,
    TranslationError,
    translate_segments,
)
from test_translate import FakeTranslator  # noqa: E402  (sibling test module)


# --- _translate_batch: a blank answer for a nonblank item is not content ----

def test_batch_rejects_blank_answer_for_nonblank_item(monkeypatch):
    """Every line is numbered, but one item's answer is empty.

    Numbering and cardinality both pass today, so the current parser accepts
    ``["uno", "", "tres"]``. An empty answer for a nonblank source is a missing
    translation, not a valid one, and the batch must be refused so the caller
    falls back - exactly as an unnumbered, duplicate, or out-of-range row is.
    """
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1. uno\n2.\n3. tres")
    with pytest.raises(TranslationError):
        t._translate_batch(["one", "two", "three"], "es")


def test_batch_blank_item_refusal_does_not_reject_blank_source(monkeypatch):
    """A blank *source* item may legitimately have a blank answer.

    Refusing it would force the fallback to demand a translation for text that
    has none, which is the false-error direction. Only a blank answer for a
    source that carries text is a missing translation.
    """
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1. uno\n2.\n3. tres")
    assert t._translate_batch(["one", "   ", "three"], "es") == ["uno", "", "tres"]


def test_batch_numbered_empty_row_is_not_a_valid_translation(monkeypatch):
    """A numbered row whose text is only whitespace is the empty answer again."""
    t = OllamaTranslator()
    monkeypatch.setattr(t, "_generate", lambda prompt: "1. uno\n2.   \n3. tres")
    with pytest.raises(TranslationError):
        t._translate_batch(["one", "two", "three"], "es")


# --- translate(): the refusal must reuse the existing per-item fallback -----

def test_blank_item_in_batch_triggers_per_item_fallback(monkeypatch):
    """The blank answer is recovered by the *existing* per-item path.

    The batch is refused (its one blank item), and the fallback asks each source
    on its own. The delivered results are complete and no item is blank.
    """
    t = OllamaTranslator()
    singles: list[str] = []

    def fake_generate(prompt):
        if "numbered line" in prompt:
            return "1. uno\n2.\n3. tres"        # item 2 answered blank
        source = prompt.rsplit("\n\n", 1)[1]
        singles.append(source)
        return {"one": "uno", "two": "dos", "three": "tres"}[source]

    monkeypatch.setattr(t, "_generate", fake_generate)
    out = t.translate(["one", "two", "three"], "es")

    assert out == ["uno", "dos", "tres"]          # complete, nothing blank
    assert singles == ["one", "two", "three"]     # the fallback actually ran


def test_blank_item_is_never_cached(monkeypatch):
    """The refused batch must not seed the cache with a blank answer.

    Caching ``("two", "es") -> ""`` from the partial batch would make the *next*
    call for "two" a cache hit and return the blank forever. The recovered text
    must be what is cached, and a later call must serve it without a new round
    trip.
    """
    t = OllamaTranslator()
    calls = {"n": 0}

    def fake_generate(prompt):
        calls["n"] += 1
        if "numbered line" in prompt:
            return "1. uno\n2.\n3. tres"
        source = prompt.rsplit("\n\n", 1)[1]
        return {"one": "uno", "two": "dos", "three": "tres"}[source]

    monkeypatch.setattr(t, "_generate", fake_generate)
    assert t.translate(["one", "two", "three"], "es") == ["uno", "dos", "tres"]
    assert t._cache.get(("two", "es")) == "dos"
    assert "" not in t._cache.values()

    before = calls["n"]
    assert t.translate(["two"], "es") == ["dos"]  # cache hit, no new round trip
    assert calls["n"] == before


def test_fallback_blank_for_nonblank_source_fails_loudly(monkeypatch):
    """If the per-item recovery is also blank, the failure is loud.

    `_translate_one` already refuses an empty model answer; this pins that the
    refusal is not swallowed by the batch path and that no blank is delivered as
    a translation of nonblank text.
    """
    t = OllamaTranslator()

    def fake_generate(prompt):
        if "numbered line" in prompt:
            return "1. uno\n2.\n3. tres"      # refused -> fallback
        source = prompt.rsplit("\n\n", 1)[1]
        if source == "two":
            return ""                          # the fallback is blank too
        return {"one": "uno", "three": "tres"}[source]

    monkeypatch.setattr(t, "_generate", fake_generate)
    with pytest.raises(TranslationError):
        t.translate(["one", "two", "three"], "es")


# --- translate_segments: incomplete backend result must not half-mutate -----

class _RightLengthBlankEntryTranslator:
    """Right length, one whitespace answer, for a nonblank source.

    The `translator` contract is duck-typed; a misbehaving backend is exactly
    the thing `translate_segments` must defend against.
    """

    name = "right-length-blank"

    def __init__(self, blank_index: int = 1, *, blank: str = "   ") -> None:
        self.blank_index = blank_index
        self.blank = blank
        self.calls: list[list[str]] = []

    def translate(self, texts, target):
        self.calls.append(list(texts))
        out = [f"[{target}]{text}" for text in texts]
        out[self.blank_index] = self.blank
        return out


def test_translate_segments_rejects_blank_result_for_nonblank_source():
    """Count matches, but the middle entry is blank for a nonblank source.

    Today `len(translated) == len(texts)` passes and the loop skips the blank
    entry, returning ``count == 1`` - "translated", with the middle item missing.
    A backend result that answers a nonblank source with blank is incomplete and
    must raise rather than be reported as a finished translation.
    """
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "world"), Segment(2, 3, "again")]
    with pytest.raises(TranslationError):
        translate_segments(segs, "es", translator=_RightLengthBlankEntryTranslator(blank_index=1))


def test_translate_segments_incomplete_result_mutates_no_segment():
    """No partial mutation: a refused result must leave every segment untouched.

    The defect lets the first segment acquire `translated_text` before the blank
    is skipped, delivering a half-translated transcript. When the result is
    rejected, *no* segment may carry a translation and none may be changed.
    """
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "world"), Segment(2, 3, "again")]
    with pytest.raises(TranslationError):
        translate_segments(segs, "es", translator=_RightLengthBlankEntryTranslator(blank_index=1))
    assert [s.translated_text for s in segs] == [None, None, None]


def test_translate_segments_preserves_words_and_source_on_rejection():
    """Rejection must not touch word timings, source text, or a prior translation.

    Word timings and source text are pinned data; a rejected backend result must
    not blank them or shift them. A pre-existing `translated_text` (from an
    earlier successful run) must survive unchanged rather than be overwritten by
    the rejected partial result.
    """
    words = [WordTiming(0.0, 0.5, "hello"), WordTiming(0.5, 1.0, "world")]
    segs = [
        Segment(0, 1, "hello world", translated_text="hola mundo", words=list(words)),
        Segment(1, 2, "again"),
    ]
    before = [
        (s.start, s.end, s.text, s.translated_text, [w.to_dict() for w in s.words])
        for s in segs
    ]

    class _BlankSecond:
        name = "blank-second"

        def translate(self, texts, target):
            return ["[es]hello world", "   "]

    with pytest.raises(TranslationError):
        translate_segments(segs, "es", translator=_BlankSecond())

    after = [
        (s.start, s.end, s.text, s.translated_text, [w.to_dict() for w in s.words])
        for s in segs
    ]
    assert after == before


def test_translate_segments_blank_source_may_return_blank():
    """An empty source segment is allowed to come back blank - no false error.

    Blank segments are skipped by design, so a blank answer for a blank source
    is not a missing translation. The other, nonblank segment still translates.
    """
    segs = [Segment(0, 1, "hello"), Segment(1, 2, "   ")]
    n = translate_segments(segs, "es", translator=FakeTranslator())
    assert n == 1
    assert segs[0].translated_text == "[es]hello"
    assert segs[1].translated_text is None


def test_translate_segments_identical_translation_is_not_an_error():
    """A translation equal to its source is legitimate (proper nouns, names).

    Rejecting `source == translation` would fail on names and same-language
    spans. Both segments are nonblank, so both count as translated.
    """
    segs = [Segment(0, 1, "Scott Converse"), Segment(1, 2, "hello")]

    class _Identity:
        name = "identity"

        def translate(self, texts, target):
            return list(texts)

    n = translate_segments(segs, "es", translator=_Identity())
    assert n == 2
    assert segs[0].translated_text == "Scott Converse"
    assert segs[1].translated_text == "hello"


# --- pipeline: no success metadata or finished marker on an incomplete set --

def test_pipeline_rejects_incomplete_translation_before_metadata(
    monkeypatch, tmp_path
):
    """An incomplete backend result must fail *before* success is recorded.

    The backend returns the right length with one blank entry for a nonblank
    source. The pipeline must raise, and must not have written
    `transcript.metadata["translation"]` nor recorded the `translate` (nor the
    coarse `postprocess`) finished-stage marker - recording either would advertise
    a translation that was never completed.

    The checkpoint snapshots handed to `on_checkpoint` are the durable record: if
    they carry a `translate`/`postprocess` marker or a `translation` metadata
    block after this failure, a resume would treat the half-done work as done.
    """
    from textflowkit.core import pipeline
    from textflowkit.core.pipeline import PipelineError, transcribe

    media = tmp_path / "clip.wav"
    media.write_bytes(b"fake")

    class _BlankLast:
        name = "blank-last"
        route = "test"

        def translate(self, texts, target):
            out = [f"[{target}]{t}" for t in texts]
            out[-1] = "   "
            return out

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: _BlankLast())
    from textflowkit.core.engine import WhisperEngine

    monkeypatch.setattr(
        WhisperEngine,
        "transcribe",
        lambda self, audio, *, language=None, **kw: Transcript(
            source=str(audio),
            language="en",
            segments=[Segment(0, 1, "hello"), Segment(1, 2, "world")],
        ),
    )

    snapshots: list[dict] = []
    with pytest.raises(PipelineError):
        transcribe(
            str(media),
            input_root=tmp_path,
            translate_to="es",
            on_checkpoint=snapshots.append,
        )

    # No finished record may claim the translate (or coarse postprocess) stage.
    for snap in snapshots:
        assert "translate" not in snap.get("finished_stages", []), (
            "an incomplete translation must not record a translate finished marker"
        )
        assert "postprocess" not in snap.get("finished_stages", []), (
            "an incomplete translation must not record the coarse postprocess marker"
        )
        meta = (snap.get("transcript") or {}).get("metadata") or {}
        assert "translation" not in meta, (
            "an incomplete translation must not be reported as translation metadata"
        )
