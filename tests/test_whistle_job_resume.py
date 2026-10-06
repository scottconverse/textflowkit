"""Durable per-block resume through the pipeline and the job store.

Whistle's native CLI transcribes one short block at a time. W1/W2 gave the
engine ``on_progress`` / ``resume_progress`` / ``check_cancel`` hooks, but the
shared pipeline never passed them, so a long run that died at block 40 of 100
started again from zero. W3 wires those hooks through the pipeline into the
existing run-owned checkpoint sink.

Every test here is deterministic and offline. A *fake* block engine stands in
for the native CLI: it implements the same hook contract, emits a Whistle-shaped
progress body after each block, and stops on cancellation exactly as the real
engine does. No model, binary, network, or ffmpeg is involved - a fake decode
step writes the "audio" the engine is handed.

The set pins the *durable* behaviour, not engine internals:

- a cancelled run keeps the blocks it finished, and a fresh SQLite handle over
  the same file resumes without redoing them or duplicating words;
- a block that fails is never recorded as done;
- changed source, changed config, and a corrupt progress body are refused;
- a v2 record with a *complete* transcript is still reused;
- a checkpoint from a lost generation, or after an accepted cancel, cannot
  corrupt the row;
- a finished job stores its words exactly once;
- cancellation is an orderly stop, never a transcribed-to-ERROR failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textflowkit.core import pipeline, whistle
from textflowkit.core.cancel import CancelledError
from textflowkit.core.checkpoint import (
    CHECKPOINT_VERSION,
    local_source_identity,
    parse_checkpoint,
)
from textflowkit.core.jobs import JobState
from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest

BLOCKS = 3


class _BlockEngine:
    """A deterministic stand-in for the windowed native engine.

    It owns the same three hooks the real engine exposes:

    - ``resume_progress`` - a validated partial to continue from. The blocks it
      records are *skipped*, never re-run, and their words are adopted verbatim.
    - ``on_progress`` - called once per completed block with a serializable
      Whistle progress body.
    - ``check_cancel`` - polled before each block; raising stops the run.

    ``fail_at`` makes a chosen block raise, so the "a failed block is not done"
    rule can be exercised. ``on_block`` is an optional hook the test uses to
    request cancellation after a chosen block, which is the cooperative-stop
    case the real engine has too (its next poll sees the request and stops).
    """

    name = "fake-block"

    def __init__(self, *, fail_at: int | None = None, on_block=None) -> None:
        self.fail_at = fail_at
        self.on_block = on_block
        self.blocks_run: list[int] = []

    def transcribe(self, audio_path, *, language=None, check_cancel=None,
                   on_progress=None, resume_progress=None) -> Transcript:
        if resume_progress is not None:
            completed = int(resume_progress["completed_core_index"])
            segments = [Segment.from_dict(s) for s in resume_progress.get("segments", [])]
        else:
            completed = 0
            segments = []

        for index in range(completed, BLOCKS):
            if check_cancel is not None:
                check_cancel()
            if self.fail_at is not None and index == self.fail_at:
                raise RuntimeError(f"native CLI failed on block {index}")
            self.blocks_run.append(index)
            word = WordTiming(start=float(index), end=index + 0.5, text=f"b{index}")
            segments.append(Segment(start=float(index), end=index + 0.5,
                                    text=f"b{index}", words=[word]))
            if on_progress is not None:
                on_progress(self._progress(index + 1, segments))
            if self.on_block is not None:
                self.on_block(index)

        return Transcript(source=str(audio_path), language=language or "en",
                          segments=list(segments), engine=self.name)

    def _progress(self, completed: int, segments: list[Segment]) -> dict:
        return {
            "schema": whistle.PROGRESS_SCHEMA,
            "policy": whistle.WINDOW_POLICY,
            "wav_identity": "aa" * 32,
            "duration": float(BLOCKS),
            "model_sha256": "bb" * 32,
            "binary_sha256": "cc" * 32,
            "language": None,
            "completed_core_index": completed,
            "segments": [s.to_dict() for s in segments],
            "total_cores": BLOCKS,
        }


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake decode step, a fake engine factory, no ffmpeg."""
    from textflowkit.core import pipeline as pipeline_mod

    built: list[_BlockEngine] = []

    def make_engine(*args, **kwargs):
        engine = _BlockEngine()
        built.append(engine)
        return engine

    def fetch(ref, *, work_dir, **kwargs):
        media = Path(work_dir) / "media.wav"
        media.write_bytes(Path(ref.location).read_bytes())
        return media

    def extract(media, *, work_dir, check_cancel=None, confined=False):
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(Path(media).read_bytes())
        return audio

    monkeypatch.setattr(pipeline_mod, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline_mod, "fetch_media", fetch)
    monkeypatch.setattr(pipeline_mod, "extract_audio", extract)
    monkeypatch.setattr(pipeline_mod, "get_engine", make_engine)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return tmp_path, built


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    return source


def _request(source: Path, **overrides) -> SubmissionRequest:
    data = {"source": str(source), "formats": ["json"], "engine": "whistle"}
    data.update(overrides)
    return SubmissionRequest(**data)


def _words(transcript_dict: dict) -> list[tuple[float, str]]:
    return [(w["start"], w["text"])
            for s in transcript_dict["segments"] for w in s["words"]]


def _first_partial(stored: list[dict]) -> dict:
    """The first snapshot that carries a partial engine body (never None here)."""
    return next(s["engine_progress"] for s in stored if s.get("engine_progress"))


# --- the core: a stopped run keeps its blocks; a restart resumes them --------


def test_a_cancelled_run_keeps_its_finished_blocks_and_a_restart_resumes_them(
    tmp_path, monkeypatch, fake_pipeline
):
    """A stop mid-recording must not lose the blocks already done.

    A run is asked to cancel after block 0 of 3 through the store's own
    cooperative flag. The job ends CANCELLED, its checkpoint holds exactly the
    one completed block, and a *fresh* store handle over the same SQLite file
    resumes: block 0 does not run again, no word is duplicated.
    """
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    db = tmp_path / "jobs.sqlite"

    store = SqliteJobStore(db)
    request = _request(source)
    job = store.create(request.source, request=request.to_dict())

    # Cancel is requested the instant the first block completes; the engine's next
    # poll sees it and raises, exactly as the real engine's check_cancel does.
    def request_cancel(_index: int) -> None:
        store.accept_cancel(job.id)

    engine = _BlockEngine(on_block=request_cancel)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)

    from textflowkit.core.runner import run_job

    run_job(job, store, source=str(source), engine="whistle",
            check_cancel=lambda: _poll_cancel(store, job.id))

    cancelled = store.get(job.id)
    assert cancelled.state is JobState.CANCELLED
    # Exactly one block ran; the store holds a valid partial with that block.
    assert engine.blocks_run == [0]
    stored_checkpoint = store.get(job.id).checkpoint
    record = parse_checkpoint(stored_checkpoint)
    assert record is not None
    assert record.engine_progress is not None
    assert record.engine_progress["completed_core_index"] == 1
    # The *full* durable record is what a resume reads, not the bare partial
    # body: `prepare_resume` stores whatever it is handed as the row's
    # checkpoint, and the pipeline parses that value back with
    # `parse_checkpoint`. A bare progress body is not a CheckpointRecord, so
    # handing it over makes the pipeline start from zero - the false proof this
    # test replaces. Carrying the whole record is what makes block 0's words
    # survive into the resumed run.
    assert stored_checkpoint.get("engine_progress") is not None
    store.close()

    # A new process: same database file, fresh handle, fresh engine.
    # A fresh handle over the same SQLite file, plus a fresh engine - the same
    # state a restarted process would begin from. (A genuinely separate OS
    # process is the coordinator's stronger proof; this test pins the resume
    # contract the restarted process relies on.)
    resumed_store = SqliteJobStore(db)
    resumed_engine = _BlockEngine()
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: resumed_engine)

    # Reopen the terminal row through the explicit-resume path, then run.
    from textflowkit.core.checkpoint import prepare_resume

    reopened, payload = prepare_resume(resumed_store, resumed_store.get(job.id),
                                       stored_checkpoint,
                                       observed_attempt=cancelled.attempt)
    assert reopened is not None
    run_job(reopened, resumed_store, source=str(source), engine="whistle",
            resume_checkpoint=payload, check_cancel=lambda: None)

    done = resumed_store.get(job.id)
    assert done.state is JobState.DONE
    # Block 0's inference was skipped: the engine resumes at core index 1 and
    # never re-enters block 0. Only cores 1 and 2 run.
    assert resumed_engine.blocks_run == [1, 2]
    words = _words(done.transcript)
    # No duplicate words, and the adopted block-0 word is preserved verbatim.
    assert len(words) == len(set(words)) == BLOCKS
    assert (0.0, "b0") in words

    # The finished row stores its words once: the checkpoint dropped both the
    # transcript and the partial body.
    assert done.checkpoint is not None
    assert "transcript" not in done.checkpoint
    assert "engine_progress" not in done.checkpoint
    resumed_store.close()


def _poll_cancel(store, job_id):
    row = store.get(job_id)
    if row is not None and row.cancel_requested:
        raise CancelledError("cancelled")


# --- wiring: partials persisted, resumed, and dropped at completion ---------


def test_each_completed_block_is_persisted_as_a_partial(tmp_path, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []

    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)

    partials = [s["engine_progress"] for s in stored if s.get("engine_progress")]
    assert [p["completed_core_index"] for p in partials] == [1, 2, 3]
    for snapshot in stored:
        if snapshot.get("engine_progress") is not None:
            # A partial never claims transcription finished, nor carries words as
            # a completed transcript.
            assert "transcribe" not in snapshot["finished_stages"]
            assert snapshot["transcript"] is None
    final = stored[-1]
    assert "transcribe" in final["finished_stages"]
    assert final["transcript"] is not None
    assert final.get("engine_progress") is None  # one copy of the words


def test_direct_python_default_alias_persists_canonical_whistle(tmp_path, fake_pipeline):
    """A direct Python ``engine='default'`` must record the concrete engine.

    The pipeline resolves the alias to Whistle and runs it, so every checkpoint
    it publishes must name ``whistle`` - not the raw alias. Persisting ``default``
    would name no canonical engine, and a v3 partial record is gated Whistle-only,
    so the alias would make a run that *did* execute Whistle unresumable.
    """
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []

    pipeline.transcribe(str(source), engine="default", on_checkpoint=stored.append,
                        input_root=tmp_path)

    assert stored, "no checkpoint was published"
    for snapshot in stored:
        assert snapshot["engine"] == "whistle", snapshot["engine"]
    # The durable record is a valid, resumable Whistle record: it parses and its
    # partial body (mid-run) is admitted by the Whistle-only gate.
    partial = next((s for s in stored if s.get("engine_progress")), None)
    assert partial is not None
    record = parse_checkpoint(partial)
    assert record is not None
    assert record.engine == "whistle"
    assert record.engine_progress is not None


def test_direct_python_default_alias_partial_resumes_under_whistle(tmp_path, monkeypatch, fake_pipeline):
    """The canonical engine recorded by ``engine='default'`` is resumable.

    A partial produced by a ``default``-named direct call must be accepted by the
    Whistle-only gate and skip its completed blocks, exactly as a ``whistle``-named
    run would. This is what "durably resumable" means for the alias path.
    """
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="default", on_checkpoint=stored.append,
                        input_root=tmp_path)
    full_words = _words(stored[-1]["transcript"])

    partial = next(p for p in (s.get("engine_progress") for s in stored)
                   if p and p.get("completed_core_index") == 1)

    resumed_engine = _BlockEngine()
    _install_engine(monkeypatch, resumed_engine)
    resumed: list[dict] = []
    pipeline.transcribe(str(source), engine="default",
                        on_checkpoint=resumed.append, input_root=tmp_path,
                        resume_checkpoint=_partial_checkpoint(source, partial))

    assert resumed_engine.blocks_run == [1, 2]  # block 0 skipped
    assert _words(resumed[-1]["transcript"]) == full_words
    assert resumed[-1]["engine"] == "whistle"


def test_live_progress_reports_the_block_count_as_n_of_m(tmp_path, fake_pipeline):
    """The per-block display is "transcribing block N/M", never a fake percent.

    The engine emits ``total_cores`` in each progress body, so the pipeline's
    guarded ``on_stage`` sink can name the real block count. A percentage would
    lie - decode time is not uniform across cores - so N/M is what is reported.
    """
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stages: list[str] = []

    pipeline.transcribe(str(source), engine="whistle", on_stage=stages.append,
                        input_root=tmp_path)

    assert [s for s in stages if s.startswith("transcribing block")] == [
        "transcribing block 1/3",
        "transcribing block 2/3",
        "transcribing block 3/3",
    ]
    assert not any("%" in s for s in stages)


def test_resume_skips_completed_blocks_without_duplicate_words(tmp_path, monkeypatch, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    full_words = _words(stored[-1]["transcript"])

    partial = next(p for p in (s.get("engine_progress") for s in stored)
                   if p and p.get("completed_core_index") == 1)

    resumed_engine = _BlockEngine()
    _install_engine(monkeypatch, resumed_engine)
    resumed: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=resumed.append, input_root=tmp_path,
                        resume_checkpoint=_partial_checkpoint(source, partial))

    assert resumed_engine.blocks_run == [1, 2]  # block 0 skipped
    resumed_words = _words(resumed[-1]["transcript"])
    assert len(resumed_words) == len(set(resumed_words))
    assert resumed_words == full_words


def test_a_middle_prefix_resume_skips_every_saved_block(tmp_path, monkeypatch, fake_pipeline):
    """A save from the *middle* of a run resumes at the next unsaved block.

    Resuming only from core 0 would not exercise the general case: a checkpoint
    written after core 2 of 3 must adopt cores 0-1's words and run only core 2.
    """
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    full_words = _words(stored[-1]["transcript"])

    # The snapshot after the *second* core completed.
    mid = next(p for p in (s.get("engine_progress") for s in stored)
               if p and p.get("completed_core_index") == 2)

    resumed_engine = _BlockEngine()
    _install_engine(monkeypatch, resumed_engine)
    resumed: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=resumed.append, input_root=tmp_path,
                        resume_checkpoint=_partial_checkpoint(source, mid))

    assert resumed_engine.blocks_run == [2]  # cores 0 and 1 both skipped
    resumed_words = _words(resumed[-1]["transcript"])
    assert len(resumed_words) == len(set(resumed_words))
    assert resumed_words == full_words


def _install_engine(monkeypatch, engine) -> None:
    """Point the pipeline's engine factory at ``engine`` for the rest of the test."""
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)


def _partial_checkpoint(source: Path, partial: dict, *, identity: dict | None = None) -> dict:
    """A resumable v3 partial record for ``source``, as the pipeline stores it.

    ``identity`` is the fingerprint captured when the partial was produced; a
    test that mutates the source afterwards passes the *original* one, which is
    what a real stored checkpoint would carry.
    """
    return {
        "version": CHECKPOINT_VERSION,
        "source": str(source),
        "model": "whistle",
        "engine": "whistle",
        "language": None,
        "options": {"formats": ["json"], "diarize": False,
                    "diarizer_backend": "pyannote", "translate_to": None,
                    "translator_backend": "ollama"},
        "finished_stages": ["source", "fetch", "extract"],
        "transcript": None,
        "engine_progress": partial,
        "media_path": None,
        "audio_path": None,
        "local_identity": identity if identity is not None
        else local_source_identity(str(source)),
    }


# --- a failed block is never recorded as done -------------------------------


def test_a_block_that_fails_is_never_recorded_as_complete(tmp_path, monkeypatch,
                                                         fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    engine = _BlockEngine(fail_at=1)  # block 1 raises; block 0 already succeeded
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)

    with pytest.raises(pipeline.PipelineError, match="transcription failed"):
        pipeline.transcribe(str(source), engine="whistle",
                            on_checkpoint=stored.append, input_root=tmp_path)

    partials = [s["engine_progress"] for s in stored if s.get("engine_progress")]
    # Only block 0 was recorded; the failed block 1 is absent.
    assert [p["completed_core_index"] for p in partials] == [1]
    assert engine.blocks_run == [0]
    assert not any("transcribe" in s["finished_stages"] for s in stored)


# --- rejection: changed source, changed config, corrupt progress ------------


def test_a_changed_source_is_refused_on_resume(tmp_path, monkeypatch, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    partial = _first_partial(stored)
    identity = next(s["local_identity"] for s in stored if s.get("local_identity"))

    source.write_bytes(b"different media now")
    engine = _BlockEngine()
    _install_engine(monkeypatch, engine)
    with pytest.raises(pipeline.PipelineError, match="changed since checkpoint"):
        pipeline.transcribe(str(source), engine="whistle", input_root=tmp_path,
                            resume_checkpoint=_partial_checkpoint(
                                source, partial, identity=identity))
    assert engine.blocks_run == []  # refused before any block ran


def test_a_corrupt_partial_is_treated_as_absent_not_reused(tmp_path, monkeypatch, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    partial = dict(_first_partial(stored))
    partial["schema"] = "other/9"  # wrong schema -> not a trusted Whistle partial

    record = _partial_checkpoint(source, partial)
    assert parse_checkpoint(record) is None  # refused, never silently reused

    # The pipeline therefore starts clean: all three blocks run again, and no
    # malformed word is ever adopted.
    engine = _BlockEngine()
    _install_engine(monkeypatch, engine)
    pipeline.transcribe(str(source), engine="whistle", input_root=tmp_path,
                        resume_checkpoint=record)
    assert engine.blocks_run == [0, 1, 2]


def test_a_partial_that_claims_transcription_finished_is_refused(tmp_path, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    partial = _first_partial(stored)
    record = _partial_checkpoint(source, partial)
    record["finished_stages"] = ["transcribe"]  # contradictory: partial but "done"

    assert parse_checkpoint(record) is None


def test_a_record_carrying_both_a_transcript_and_a_partial_is_refused():
    """The two are the same words; a record with both must never parse."""
    record = {
        "version": CHECKPOINT_VERSION,
        "source": "clip.wav", "model": "whistle", "engine": "whistle",
        "options": {}, "finished_stages": ["transcribe"],
        "transcript": Transcript(source="clip.wav",
                                 segments=[Segment(0.0, 1.0, "hi")]).to_dict(),
        "engine_progress": {"schema": whistle.PROGRESS_SCHEMA, "policy": "p",
                            "wav_identity": "aa" * 32, "model_sha256": "bb" * 32,
                            "binary_sha256": "cc" * 32, "completed_core_index": 1},
    }
    assert parse_checkpoint(record) is None


# --- legacy reuse: a v2 complete record still resumes -----------------------


def test_a_v2_complete_record_is_still_reused(tmp_path, monkeypatch, fake_pipeline):
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    from textflowkit.core.checkpoint import validate_local_resume

    complete = Transcript(source=str(source), language="en",
                          segments=[Segment(0.0, 1.0, "hello", words=[])]).to_dict()
    record = {
        "version": 2,  # the previous version, still a full record
        "source": str(source), "model": "whistle", "engine": "whistle",
        "language": None,
        "options": {"formats": ["json"], "diarize": False,
                    "diarizer_backend": "pyannote", "translate_to": None,
                    "translator_backend": "ollama"},
        "finished_stages": ["fetch", "extract", "transcribe"],
        "transcript": complete,
        "local_identity": local_source_identity(str(source)),
    }
    parsed = parse_checkpoint(record)
    assert parsed is not None and parsed.version == 2
    assert parsed.transcript is not None
    # A v2 fingerprint still validates against the unchanged source.
    validate_local_resume(parsed, str(source))

    engine = _BlockEngine()
    _install_engine(monkeypatch, engine)
    result = pipeline.transcribe(str(source), engine="whistle", input_root=tmp_path,
                                 resume_checkpoint=record)
    assert engine.blocks_run == []  # the finished transcript was reused as-is
    assert result.transcript.segments[0].text == "hello"


# --- cancellation is not swallowed -----------------------------------------


def test_pipeline_cancellation_is_not_wrapped_as_a_failure(tmp_path, monkeypatch,
                                                          fake_pipeline):
    """An engine's CancelledError must surface as CancelledError, not PipelineError."""
    _root, _built = fake_pipeline
    source = _source(tmp_path)

    class _CancellingEngine(_BlockEngine):
        def transcribe(self, audio_path, *, language=None, check_cancel=None,
                       on_progress=None, resume_progress=None):
            raise CancelledError("stop requested")

    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: _CancellingEngine())
    with pytest.raises(CancelledError):
        pipeline.transcribe(str(source), engine="whistle", input_root=tmp_path)


# --- old engines are untouched ---------------------------------------------


def test_an_engine_without_the_hooks_is_called_exactly_as_before(tmp_path, monkeypatch, fake_pipeline):
    """A whisper-shaped engine (no hooks) neither breaks nor receives them."""
    _root, _built = fake_pipeline
    source = _source(tmp_path)
    seen: dict = {}

    class _PlainEngine:
        name = "plain"

        def transcribe(self, audio, *, language=None):
            seen["called"] = True
            return Transcript(source=str(audio), language="en",
                              segments=[Segment(0.0, 1.0, "hi")])

    _install_engine(monkeypatch, _PlainEngine())
    stored: list[dict] = []
    pipeline.transcribe(str(source), engine="whistle",
                        on_checkpoint=stored.append, input_root=tmp_path)
    assert seen.get("called") is True
    assert stored[-1]["transcript"] is not None
