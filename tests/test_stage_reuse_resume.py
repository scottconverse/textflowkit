"""Stage reuse across a failed publication, then a resume (G3B).

The pipeline records checkpoints per stage. On the way in, a resumed run reads
the checkpoint's transcript and skips acquisition and Whisper - but the optional
postprocessing stages are governed only by the arguments the *caller* passes
now, not by what the checkpoint already finished. The current source:

    transcript = _resume_transcript(resumed, ...)
    can_resume = transcript is not None and _checkpoint_paths_usable(resumed)
    ...
    elif diarize and audio is None:        # line 343: reacquire audio
        ...
    if diarize:                            # line 376: rerun the diarizer
        ...
    if translate_to:                       # line 397: rerun the translator
        ...
    _checkpoint("postprocess")             # line 415: the only postprocess mark

so a job that finished diarization and translation, published one format, and
then failed a later format is forced to re-run the diarizer *and* the translator
on resume - and, when the audio was scratch-deleted, to re-fetch and re-decode
the media to do it. The expensive work is already durable in the checkpoint; the
resume must reuse it.

These tests drive the **real** ``submit_request`` -> ``run_job`` -> ``transcribe``
path over a temp tree with counting engine / diarizer / translator stand-ins and
a fake decode step, so no model, network, ffmpeg, or inference is involved. The
fault is injected once, at the real publish boundary, which is where the defect
is observed. Nothing here sleeps and nothing here runs a sleep loop.

The finalize contract (runner.py) is: a run that ends **DONE** stores its
transcript in the job's own ``transcript`` field and the checkpoint keeps only
metadata; a run that ends **ERROR** keeps the full transcript in the checkpoint
and the job's ``transcript`` field stays None. Every test that inspects a
checkpoint after a failure therefore reads the ERROR checkpoint - never
``job.transcript``, which by construction is empty on a failed job.

These tests are written to fail RED against the current source (the "resume
reuses the finished stage" cases) while the "fails closed / is preserved" cases
pin behaviour that must survive the fix. See ``reports/G3B-red.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import textflowkit.render as render_mod
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import local_source_identity
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.submission import (
    SubmissionRequest,
    resume_job,
    submit_request,
)

# Distinctive bodies so "was this text retained" is a byte question, not a guess.
SOURCE_TEXT = "the original spoken words"
TRANSLATED_TEXT = "les mots parles originaux"
SPEAKER = "SPEAKER_07"


class _CountingEngine:
    """Deterministic engine that records how many times it was asked to work."""

    def __init__(self, segments=None) -> None:
        self.calls = 0
        self._segments = segments

    def transcribe(self, audio, *, language=None) -> Transcript:
        self.calls += 1
        segments = self._segments or [
            Segment(0.0, 1.0, SOURCE_TEXT),
            Segment(1.0, 2.0, "and a second line"),
        ]
        return Transcript(source=str(audio), language=language or "en", segments=segments)


class _CountingDiarizer:
    """A diarizer stand-in that counts runs and returns a fixed turn list."""

    name = "test-diarizer"

    def __init__(self, *, turns=None, fail: bool = False) -> None:
        self.calls = 0
        self._turns = turns
        self._fail = fail

    def diarize(self, audio):
        self.calls += 1
        if self._fail:
            from textflowkit.core.diarize import DiarizationError

            raise DiarizationError("injected diarizer failure")
        from textflowkit.core.diarize import SpeakerTurn

        if self._turns is not None:
            return self._turns
        return [SpeakerTurn(0.0, 2.0, SPEAKER)]


class _CountingTranslator:
    """A translator stand-in that counts runs and returns fixed target text."""

    name = "test-translator"
    route = "local test"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self._fail = fail

    def translate(self, texts, target: str):
        self.calls += 1
        if self._fail:
            from textflowkit.core.translate import TranslationError

            raise TranslationError("injected translator failure")
        return [TRANSLATED_TEXT for _ in texts]


@pytest.fixture
def stage_counters(tmp_path, monkeypatch):
    """A temp tree, counting stand-ins, and a fake decode step: no real work.

    Returns an object holding the counters and recording which acquisition
    functions ran, so a test can assert "no second acquire" as a fact rather
    than inferring it.
    """
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    class Counters:
        def __init__(self) -> None:
            self.engine = _CountingEngine()
            self.diarizer = _CountingDiarizer()
            self.translator = _CountingTranslator()
            self.fetches = 0
            self.extracts = 0
            self.stages: list[str] = []

    counters = Counters()

    def fetch(ref, *, work_dir, **kwargs):
        counters.fetches += 1
        media = Path(work_dir) / "media.wav"
        media.write_bytes(Path(ref.location).read_bytes())
        return media

    def extract(media, *, work_dir, check_cancel=None, confined=False):
        counters.extracts += 1
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(media.read_bytes())
        return audio

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: counters.engine)
    monkeypatch.setattr(pipeline, "get_diarizer", lambda *a, **k: counters.diarizer)
    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: counters.translator)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return output_dir, counters


@pytest.fixture(autouse=True)
def _isolate_diarizer_cache(monkeypatch):
    """Never hand out a cached real diarizer/translator through a fake lookup.

    The pipeline's fakes replace ``get_diarizer``/``get_translator`` wholesale,
    but a stray direct call must not reach the process-wide lru_cache and load a
    real backend. Clearing it keeps the module hermetic and inference-free.
    """
    from textflowkit.core.diarize import _cached_diarizer

    _cached_diarizer.cache_clear()
    yield
    _cached_diarizer.cache_clear()


def _fail_publish_once(monkeypatch, suffix: str) -> None:
    """Fail the first publish attempt for ``suffix``, then publish normally.

    The wrapper delegates to the real writer, so the resumed attempt still runs
    the real publication path - only the first attempt is faulted, exactly where
    the defect is observed.
    """
    real = render_mod.atomic_write_bytes
    armed = {"pending": True}

    def flaky(path, data, **kwargs):
        if armed["pending"] and Path(path).suffix == suffix:
            armed["pending"] = False
            raise OSError(f"injected publish failure for {suffix}")
        return real(path, data, **kwargs)

    monkeypatch.setattr(render_mod, "atomic_write_bytes", flaky)


def _request(
    source: Path, output_dir: Path, *, diarize: bool, translate_to: str | None
) -> SubmissionRequest:
    return SubmissionRequest(
        source=str(source),
        formats=["txt", "srt"],
        output_dir=str(output_dir),
        model="tiny",
        device="cpu",
        diarize=diarize,
        translate_to=translate_to,
    )


def _error_checkpoint(store: MemoryJobStore, job_id: str) -> dict:
    """The durable snapshot of a failed job, read from its ERROR checkpoint.

    Deliberately not ``job.transcript``: a failed run never writes the job's
    transcript field, so the checkpoint is the only copy of the work.
    """
    job = store.get(job_id)
    assert job.state is JobState.ERROR, job.error
    assert job.transcript is None, "a failed job must not carry a job transcript"
    checkpoint = job.checkpoint
    assert isinstance(checkpoint, dict), "the ERROR checkpoint is the resume material"
    return checkpoint


# --- the observed defect: a failed publication then a resume -----------------
#
# A job that ran transcribe + diarize + translate, published its first format,
# and failed the second is forced to re-run the optional stages on resume, and -
# because scratch media is deleted after every run - to re-acquire the audio
# first. Each ``*_reuses_*`` case below turns RED against the current source.


def test_resume_after_dead_provider_run_reuses_diarization_and_translation(
    tmp_path, monkeypatch, stage_counters
):
    """No second engine/provider/acquire; the finished stages are reused.

    This is the headline case. It fails the publish of the second format, then
    resumes the same job and asserts that the engine, the diarizer and the
    translator each ran exactly once and nothing was re-fetched or re-decoded.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to="fr"),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR

    # The first attempt did each expensive optional stage exactly once.
    assert counters.engine.calls == 1
    assert counters.diarizer.calls == 1
    assert counters.translator.calls == 1
    assert counters.fetches == 1
    assert counters.extracts == 1

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert resumed.id == job.id
    assert counters.engine.calls == 1, "a resume must not transcribe again"
    assert counters.diarizer.calls == 1, "a resume must reuse finished diarization"
    assert counters.translator.calls == 1, "a resume must reuse finished translation"
    assert counters.fetches == 1, "a resume must not re-acquire the media"
    assert counters.extracts == 1, "a resume must not re-decode the audio"

    # Both formats are published, and the reused text survived the resume.
    txt_path = output_dir / f"clip-{job.id}.txt"
    srt_path = output_dir / f"clip-{job.id}.srt"
    assert txt_path.is_file() and srt_path.is_file()
    assert TRANSLATED_TEXT in txt_path.read_text(encoding="utf-8")
    assert SPEAKER in srt_path.read_text(encoding="utf-8")


def test_resume_retains_exact_translated_text_speakers_and_timestamps(
    tmp_path, monkeypatch, stage_counters
):
    """The reused transcript keeps exact text, speaker labels, and timings.

    A resume that re-ran the diarizer or translator could silently relabel or
    retranslate. This pins the delivered bytes of the reused segments: the
    translated text, the speaker assignment, and both timestamps are exactly
    those the first attempt produced.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to="fr"),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR

    # Capture the durable snapshot the resume will read.
    checkpoint = _error_checkpoint(store, job.id)
    first_segments = checkpoint["transcript"]["segments"]
    assert first_segments[0]["translated_text"] == TRANSLATED_TEXT
    assert first_segments[0]["speaker"] == SPEAKER
    assert (first_segments[0]["start"], first_segments[0]["end"]) == (0.0, 1.0)
    assert first_segments[0]["text"] == SOURCE_TEXT

    resumed = resume_job(store, job.id, background=False)
    assert resumed.state is JobState.DONE, resumed.error

    # Read the rendered txt/srt, which is what a user receives.
    txt_path = output_dir / f"clip-{job.id}.txt"
    srt_path = output_dir / f"clip-{job.id}.srt"
    txt_body = txt_path.read_text(encoding="utf-8")
    srt_body = srt_path.read_text(encoding="utf-8")

    assert SOURCE_TEXT in txt_body
    assert TRANSLATED_TEXT in txt_body
    assert SPEAKER in srt_body
    # Timestamps survive: the first cue in the SRT is still 00:00:00.
    assert "00:00:00,000 --> 00:00:01,000" in srt_body

    # And every stage ran once.
    assert counters.diarizer.calls == 1
    assert counters.translator.calls == 1
    assert counters.fetches == 1
    assert counters.extracts == 1


def test_resume_reuses_diarization_when_media_was_removed_after_failure(
    tmp_path, monkeypatch, stage_counters
):
    """Even with the retained media gone, a finished diarization is not redone.

    The checkpoint records only paths outside scratch; the scratch tree (and the
    media in it) is deleted after every run. On resume the pipeline re-acquires
    the audio solely to re-run the diarizer - work the checkpoint already holds.
    A resume that reuses the finished stage needs neither the media nor the
    provider.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to=None),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR
    assert counters.diarizer.calls == 1

    # The provider is unavailable on resume: reuse must not need it.
    monkeypatch.setattr(
        pipeline,
        "get_diarizer",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the diarizer provider was consulted on resume")
        ),
    )

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert counters.diarizer.calls == 1, "the finished diarization was rerun"
    assert counters.engine.calls == 1
    assert counters.fetches == 1, "the audio was re-acquired for no reason"
    assert counters.extracts == 1
    srt_path = output_dir / f"clip-{job.id}.srt"
    assert SPEAKER in srt_path.read_text(encoding="utf-8")


def test_resume_reuses_translation_when_the_translator_is_unavailable(
    tmp_path, monkeypatch, stage_counters
):
    """A completed translation is reused without consulting the provider.

    The same shape for the translation stage: with the translator made to explode
    on resume, the resume must still finish DONE from the durable snapshot.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=False, translate_to="fr"),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR
    assert counters.translator.calls == 1

    monkeypatch.setattr(
        pipeline,
        "get_translator",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the translator provider was consulted on resume")
        ),
    )

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert counters.translator.calls == 1
    assert counters.engine.calls == 1
    txt_path = output_dir / f"clip-{job.id}.txt"
    assert TRANSLATED_TEXT in txt_path.read_text(encoding="utf-8")


# --- partial diarization success, then translation failure -------------------


def test_partial_diarization_then_translation_failure_resumes_only_translation(
    tmp_path, monkeypatch, stage_counters
):
    """Diarization succeeds, translation fails: resume runs only translation.

    The translation fails on the first attempt (provider error, not a publish
    fault). The durable snapshot must record the finished diarization so the
    resume reuses it and runs *only* the translation - no second diarizer, no
    acquisition, no Whisper.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()

    failing_translator = _CountingTranslator(fail=True)
    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: failing_translator)

    request = _request(source, output_dir, diarize=True, translate_to="fr")
    job = submit_request(store, request, background=False)

    assert store.get(job.id).state is JobState.ERROR
    assert counters.diarizer.calls == 1
    assert failing_translator.calls == 1

    # The durable snapshot after the diarization succeeded must say so, so a
    # resume knows diarization does not need to run again.
    checkpoint = _error_checkpoint(store, job.id)
    assert "diarize" in checkpoint["finished_stages"], (
        "a completed diarization stage was not recorded as durable"
    )
    # The diarization result is retained in the snapshot.
    first_segments = checkpoint["transcript"]["segments"]
    assert first_segments[0]["speaker"] == SPEAKER, (
        "the durable snapshot did not retain the diarization labels"
    )

    # The provider is now healthy, and the failed first attempt left the
    # diarizer counter at one. The resume must run only the translation.
    healthy_translator = _CountingTranslator()
    monkeypatch.setattr(pipeline, "get_translator", lambda *a, **k: healthy_translator)

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert healthy_translator.calls == 1, "translation must run on the resume"
    assert counters.diarizer.calls == 1, "diarization must not run again"
    assert counters.engine.calls == 1, "Whisper must not run again"
    assert counters.fetches == 1, "the audio must not be re-acquired"
    assert counters.extracts == 1
    txt_path = output_dir / f"clip-{job.id}.txt"
    assert TRANSLATED_TEXT in txt_path.read_text(encoding="utf-8")
    assert SPEAKER in (output_dir / f"clip-{job.id}.srt").read_text(encoding="utf-8")


def test_diarization_only_resume_does_not_run_the_translator(
    tmp_path, monkeypatch, stage_counters
):
    """A resume must not run an optional stage that was never requested.

    Control for the partial case: with only diarization requested, the translator
    must never be consulted, on the first attempt or the resume.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    def explode(*a, **k):
        raise AssertionError("the translator ran for a diarization-only job")

    monkeypatch.setattr(pipeline, "get_translator", explode)

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to=None),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR
    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert counters.diarizer.calls == 1


# --- legacy markers and the "cannot count as completed" rule ----------------


def _legacy_error_job(
    store: MemoryJobStore,
    source: Path,
    output_dir: Path,
    *,
    finished_stages: list[str],
    input_root: Path | None = None,
) -> str:
    """Seed a failed job whose checkpoint carries finished diarization work.

    The transcript already holds the speaker labels, imitating the snapshot a
    real diarizing attempt would leave behind. ``finished_stages`` is varied to
    probe which marker a reader is willing to trust.
    """
    transcript = Transcript(
        source=str(source),
        language="en",
        segments=[Segment(0.0, 1.0, SOURCE_TEXT, speaker=SPEAKER)],
    )
    request = _request(source, output_dir, diarize=True, translate_to=None)
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id,
        state=JobState.ERROR,
        error="interrupted",
        progress="failed",
        checkpoint={
            "version": 2,
            "source": str(source),
            "model": request.model,
            "language": request.language,
            "engine": request.engine,
            "device": request.device,
            "options": request.options(),
            "finished_stages": finished_stages,
            "transcript": transcript.to_dict(),
            "media_path": None,
            "audio_path": None,
            "local_identity": local_source_identity(
                str(source), input_root=input_root
            ),
        },
    )
    return job.id


def test_legacy_postprocess_marker_reuses_optional_finished_work(
    tmp_path, monkeypatch, stage_counters
):
    """A checkpoint marked ``postprocess`` reuses its diarization without rerunning.

    Records written before a per-stage marker existed carry only the coarse
    ``postprocess`` mark. That is the marker a finished optional stage leaves, so
    it must be enough to reuse the work it names - not a reason to rerun.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job_id = _legacy_error_job(
        store,
        source,
        output_dir,
        finished_stages=["source", "fetch", "extract", "transcribe", "postprocess"],
    )

    monkeypatch.setattr(
        pipeline,
        "get_diarizer",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the diarizer ran despite a legacy postprocess mark")
        ),
    )

    resumed = resume_job(store, job_id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert counters.diarizer.calls == 0
    assert counters.engine.calls == 0
    assert counters.fetches == 0
    srt_path = output_dir / f"clip-{job_id}.srt"
    assert SPEAKER in srt_path.read_text(encoding="utf-8")


def test_metadata_without_a_stage_marker_is_not_treated_as_completed(
    tmp_path, monkeypatch, stage_counters
):
    """Metadata alone must not pass as a finished stage.

    A checkpoint whose speaker metadata happens to be present but whose
    ``finished_stages`` never recorded the optional stage must be read as "not
    done", so the stage runs. Trusting the mere presence of metadata would let a
    half-finished snapshot be served as complete.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    # Transcription finished, but nothing records a postprocess stage.
    job_id = _legacy_error_job(
        store,
        source,
        output_dir,
        finished_stages=["source", "fetch", "extract", "transcribe"],
    )

    resumed = resume_job(store, job_id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert counters.diarizer.calls == 1, (
        "an unmarked stage must run, not be assumed complete from metadata"
    )
    assert counters.engine.calls == 0, "Whisper must still be reused"
    srt_path = output_dir / f"clip-{job_id}.srt"
    assert SPEAKER in srt_path.read_text(encoding="utf-8")


# --- preserved behaviour: fail-closed and confinement ------------------------


def test_resume_preserves_the_source_fingerprint_check(
    tmp_path, monkeypatch, stage_counters
):
    """A source changed since the checkpoint still fails closed on resume.

    Reuse must not weaken the fingerprint guard: a local source whose bytes moved
    after the failed attempt must refuse the resume, not serve stale work.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to="fr"),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR

    source.write_bytes(b"different bytes entirely")

    with pytest.raises(ValueError, match="changed since checkpoint"):
        resume_job(store, job.id, background=False)

    current = store.get(job.id)
    assert current.state is JobState.ERROR, "a refusal must not reopen the row"
    assert counters.engine.calls == 1
    assert counters.diarizer.calls == 1


def test_resume_keeps_the_confined_decoder_semantics(
    tmp_path, monkeypatch, stage_counters
):
    """A re-acquired local source under an input root still decodes confined.

    If the fix keeps the re-acquisition branch, the decoder boundary must follow
    the source exactly as it does today. This records the ``confined`` flag the
    decode step is handed, so the decision is asserted rather than ffmpeg's
    behaviour.
    """
    root = tmp_path / "allowed"
    root.mkdir()
    output_dir, counters = stage_counters
    source = root / "clip.wav"
    source.write_bytes(b"fake media")

    confined_flags: list[bool] = []
    real_extract = pipeline.extract_audio

    def recording_extract(media, *, work_dir, check_cancel=None, confined=False):
        confined_flags.append(confined)
        return real_extract(
            media, work_dir=work_dir, check_cancel=check_cancel, confined=confined
        )

    monkeypatch.setattr(pipeline, "extract_audio", recording_extract)
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(root))

    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")
    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to=None),
        background=False,
    )
    assert store.get(job.id).state is JobState.ERROR
    assert confined_flags == [True], f"first decode flags: {confined_flags}"

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    # Whether the resume re-acquires or reuses the audio, any decode it performs
    # must be confined, because the source is a local file under an input root.
    assert confined_flags and all(confined_flags), (
        f"a decode ran unconfined under an input root: {confined_flags}"
    )


def test_error_checkpoint_carries_the_transcript_not_the_job_field(
    tmp_path, monkeypatch, stage_counters
):
    """The failed-attempt contract the reuse must build on.

    Pins the storage shape the other tests rely on: an ERROR job keeps its full
    transcript in the checkpoint and leaves ``job.transcript`` empty. A fix that
    read the job's transcript field on resume would find nothing here.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to="fr"),
        background=False,
    )
    failed = store.get(job.id)
    assert failed.state is JobState.ERROR
    assert failed.transcript is None, "a failed job must not carry a job transcript"
    assert failed.checkpoint is not None
    body = failed.checkpoint["transcript"]
    assert body is not None, "the ERROR checkpoint is the only copy of the work"
    assert body["segments"][0]["translated_text"] == TRANSLATED_TEXT
    assert body["segments"][0]["speaker"] == SPEAKER
    # The metadata the resume matches on is present too.
    assert failed.checkpoint["source"] == str(source)
    assert "transcribe" in failed.checkpoint["finished_stages"]


def test_finalize_moves_the_transcript_to_the_job_on_success(
    tmp_path, monkeypatch, stage_counters
):
    """Control: a run that finishes stores its transcript once, in the job field.

    The counterpart to the ERROR shape above - a DONE job's checkpoint is
    metadata-only. Together the two cases show why a resume cannot read the job's
    transcript field after a failure.
    """
    output_dir, counters = stage_counters
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()

    job = submit_request(
        store,
        _request(source, output_dir, diarize=True, translate_to="fr"),
        background=False,
    )
    done = store.get(job.id)

    assert done.state is JobState.DONE, done.error
    assert done.transcript is not None
    body = json.dumps(done.transcript)
    assert TRANSLATED_TEXT in body and SPEAKER in body
    assert done.checkpoint is not None
    assert done.checkpoint.get("transcript") is None, (
        "a DONE checkpoint keeps only metadata"
    )
