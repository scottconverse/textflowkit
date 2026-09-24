"""Polled job progress while a job is running.

An MCP or HTTP client polls `get_job_status` / `GET /jobs/{id}` while a long
transcription runs. Before this unit every poll returned `starting` until the
job reached a terminal state, so a job doing real work was indistinguishable
from a hung one.

These tests drive the real runner and the real pipeline with only the external
boundaries stubbed, and read `progress` back out of the job store - the same
value the adapters serialize. Sampling the callback argument instead would
prove nothing about what a poller sees.
"""

from __future__ import annotations

import threading

import pytest

from textflowkit.core import pipeline, runner
from textflowkit.core.checkpoint import CheckpointRecord
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript

SOURCE = "https://example.com/v"

# Progress values that mean the job has stopped. None of them may appear while
# a stage is still running.
TERMINAL_PROGRESS = frozenset({"complete", "failed", "cancelled"})


class _Ref:
    platform = "youtube"
    kind = "url"
    location = SOURCE


def _progress(store: MemoryJobStore, job_id: str) -> str:
    """Read the job's progress the way a poller's status query would."""
    job = store.get(job_id)
    assert job is not None
    return job.progress


def _wire_pipeline(
    monkeypatch,
    tmp_path,
    store: MemoryJobStore,
    job_id: str,
    samples: list[tuple[str, str]],
    *,
    engine_raises: Exception | None = None,
    engine_gate: tuple[threading.Event, threading.Event] | None = None,
) -> Transcript:
    """Stub the pipeline's external boundaries, sampling progress as each runs.

    Only acquisition, decoding, inference, and the write are stubbed. Everything
    between them - stage order, checkpointing, resume decisions - is the real
    code path.

    `engine_gate` is an (entered, release) pair of events: a test can hold the
    engine open and poll from another thread, with no sleep involved.
    """
    media = tmp_path / "media.bin"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")
    transcript = Transcript(
        source=SOURCE, language="en", segments=[Segment(0.0, 1.0, "hi")]
    )

    class Engine:
        def transcribe(self, audio_path, language=None):
            samples.append(("engine", _progress(store, job_id)))
            if engine_gate is not None:
                entered, release = engine_gate
                entered.set()
                release.wait(timeout=10)
            if engine_raises is not None:
                raise engine_raises
            return transcript

    def fetch(*_args, **_kwargs):
        samples.append(("fetch", _progress(store, job_id)))
        return media

    def extract(*_args, **_kwargs):
        samples.append(("extract", _progress(store, job_id)))
        return audio

    real_write_all = pipeline.write_all

    def write_all(*args, **kwargs):
        samples.append(("render", _progress(store, job_id)))
        return real_write_all(*args, **kwargs)

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: _Ref())
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    monkeypatch.setattr(pipeline, "write_all", write_all)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return transcript


def test_polled_progress_names_the_stage_in_flight(monkeypatch, tmp_path):
    """A full run must expose several distinct nonterminal stages to a poller."""
    store = MemoryJobStore()
    job = store.create(SOURCE)
    samples: list[tuple[str, str]] = []
    _wire_pipeline(monkeypatch, tmp_path, store, job.id, samples)

    runner.run_job(job, store, source=SOURCE, formats=["json"], output_dir=tmp_path)

    by_stage = dict(samples)
    assert by_stage["fetch"] == "fetching"
    assert by_stage["extract"] == "extracting"
    # The engine call is the long stage: this is the value a poller reads
    # *while* Whisper runs, which is the whole point of the unit.
    assert by_stage["engine"] == "transcribing"
    assert by_stage["render"] == "rendering"

    distinct = {value for _stage, value in samples}
    assert len(distinct) >= 3
    assert distinct.isdisjoint(TERMINAL_PROGRESS)

    finished = store.get(job.id)
    assert finished.state is JobState.DONE
    assert finished.progress == "complete"


def test_failed_run_reports_the_stage_then_the_unchanged_terminal_state(
    monkeypatch, tmp_path
):
    """A failure still ends `failed`; the stage in flight was visible before it."""
    store = MemoryJobStore()
    job = store.create(SOURCE)
    samples: list[tuple[str, str]] = []
    _wire_pipeline(
        monkeypatch,
        tmp_path,
        store,
        job.id,
        samples,
        engine_raises=RuntimeError("boom"),
    )

    runner.run_job(job, store, source=SOURCE)

    by_stage = dict(samples)
    assert by_stage["fetch"] == "fetching"
    assert by_stage["engine"] == "transcribing"

    errored = store.get(job.id)
    assert errored.state is JobState.ERROR
    assert errored.progress == "failed"
    assert "transcription failed" in (errored.error or "")


def test_mcp_poll_during_the_run_sees_the_stage_in_flight(monkeypatch, tmp_path):
    """The reported defect: a poll mid-run saw `starting` for the whole run.

    The engine is held open on an event, so the poll provably happens while
    transcription work is in flight. No sleep and no model: the wait is a
    handshake.
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters import mcp_server

    store = MemoryJobStore()
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: store)
    job = store.create(SOURCE)
    samples: list[tuple[str, str]] = []
    entered, release = threading.Event(), threading.Event()
    _wire_pipeline(
        monkeypatch,
        tmp_path,
        store,
        job.id,
        samples,
        engine_gate=(entered, release),
    )

    worker = threading.Thread(
        target=lambda: runner.run_job(
            job, store, source=SOURCE, formats=["json"], output_dir=tmp_path
        ),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=10)

    payload = mcp_server.get_job_status(job.id)
    assert payload["state"] == "running"
    assert payload["progress"] == "transcribing"

    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert store.get(job.id).progress == "complete"


def test_resume_does_not_claim_a_stage_whose_work_is_skipped(monkeypatch, tmp_path):
    """Resuming reuses the transcript; the skipped stages must not be reported.

    A completion callback fires *after* a stage ends, so mapping its label to
    progress would announce `transcribe` for a run that never calls the engine.
    """
    store = MemoryJobStore()
    job = store.create(SOURCE)
    samples: list[tuple[str, str]] = []
    transcript = _wire_pipeline(monkeypatch, tmp_path, store, job.id, samples)
    checkpoint = CheckpointRecord(
        source=SOURCE,
        model="small",
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=transcript.to_dict(),
    )

    runner.run_job(
        job,
        store,
        source=SOURCE,
        formats=["json"],
        output_dir=tmp_path,
        resume_checkpoint=checkpoint.to_dict(),
    )

    values = {value for _stage, value in samples}
    assert "transcribing" not in values
    assert "extracting" not in values
    assert dict(samples)["render"] == "rendering"

    finished = store.get(job.id)
    assert finished.state is JobState.DONE
    assert finished.progress == "complete"
