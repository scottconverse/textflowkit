"""The job executor: concurrency bounding and cancellation.

These drive the real `run_job` and the real cancellation plumbing, stubbing only
`transcribe` (the expensive stage). A test that stubbed `run_job` itself would
prove nothing about the contract that matters.
"""

from __future__ import annotations

import threading
import time

import pytest

from textflowkit.core import runner
from textflowkit.core.executor import (
    CancelToken,
    JobCancelled,
    JobExecutor,
    get_default_executor,
    reset_default_executor,
)
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Transcript
from textflowkit.core.pipeline import TranscribeResult


@pytest.fixture(autouse=True)
def clean_executor():
    yield
    reset_default_executor()


def _ok_result(source: str) -> TranscribeResult:
    return TranscribeResult(
        transcript=Transcript(source=source, language="en", segments=[]),
        outputs=[],
    )


# --- CancelToken ----------------------------------------------------------

def test_cancel_token_starts_uncancelled():
    assert CancelToken().cancelled is False


def test_cancel_token_checkpoint_raises_after_cancel():
    token = CancelToken()
    token.checkpoint()          # no-op before cancel
    token.cancel()
    with pytest.raises(JobCancelled):
        token.checkpoint()


def test_cancel_token_is_thread_safe():
    token = CancelToken()
    token.cancel()
    t = threading.Thread(target=token.cancel)
    t.start()
    t.join()
    assert token.cancelled is True


# --- concurrency bound ----------------------------------------------------

def test_executor_bounds_simultaneous_jobs(monkeypatch):
    """Six jobs on a pool of two must never run more than two at once."""
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=2)

    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.15)
        finally:
            with lock:
                active -= 1
        return _ok_result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    jobs = [ex.submit(source=f"s{i}") for i in range(6)]
    deadline = time.time() + 10
    while time.time() < deadline:
        if all(store.get(j.id).state is JobState.DONE for j in jobs):
            break
        time.sleep(0.02)

    assert peak <= 2, f"concurrency bound violated: peak={peak}"
    assert all(store.get(j.id).state is JobState.DONE for j in jobs)
    assert ex.max_concurrency == 2
    ex.shutdown()


def test_executor_default_concurrency_is_one(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_MAX_CONCURRENCY", raising=False)
    assert JobExecutor(MemoryJobStore()).max_concurrency == 1


def test_executor_concurrency_from_env(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_MAX_CONCURRENCY", "3")
    assert JobExecutor(MemoryJobStore()).max_concurrency == 3


def test_executor_concurrency_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_MAX_CONCURRENCY", "not-a-number")
    assert JobExecutor(MemoryJobStore()).max_concurrency == 1


def test_executor_clamps_concurrency_to_at_least_one(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_MAX_CONCURRENCY", raising=False)
    assert JobExecutor(MemoryJobStore(), max_concurrency=0).max_concurrency == 1


# --- cancellation ---------------------------------------------------------

def test_cancel_queued_job_never_runs(monkeypatch):
    """A job cancelled while queued is marked cancelled and never executed."""
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=1)

    started = threading.Event()
    release = threading.Event()
    ran: list[str] = []

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        ran.append(source)
        if check_cancel:
            check_cancel()
        started.set()
        release.wait(timeout=5)
        if check_cancel:
            check_cancel()
        return _ok_result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    first = ex.submit(source="blocker")
    assert started.wait(timeout=5)
    second = ex.submit(source="queued")     # sits in the queue

    assert ex.cancel(second.id) is True
    assert store.get(second.id).state is JobState.CANCELLED

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and store.get(first.id).state is not JobState.DONE:
        time.sleep(0.02)

    assert "queued" not in ran, "a cancelled queued job must not execute"
    assert store.get(second.id).state is JobState.CANCELLED
    ex.shutdown()


def test_cancel_running_job_stops_at_checkpoint(monkeypatch):
    """A running job stops at its next checkpoint and ends CANCELLED."""
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=1)

    running = threading.Event()
    release = threading.Event()

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        if check_cancel:
            check_cancel()
        running.set()
        release.wait(timeout=5)
        if check_cancel:
            check_cancel()          # raises JobCancelled -> orderly stop
        return _ok_result(source)   # pragma: no cover - must not be reached

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    job = ex.submit(source="long")
    assert running.wait(timeout=5)

    # While running, cancel sets cancel_requested and trips the token.
    assert ex.cancel(job.id) is True
    mid = store.get(job.id)
    assert mid.cancel_requested is True
    assert mid.state is JobState.RUNNING      # honest: not stopped yet
    assert mid.progress == "cancelling"

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and store.get(job.id).state is not JobState.CANCELLED:
        time.sleep(0.02)

    assert store.get(job.id).state is JobState.CANCELLED
    assert store.get(job.id).progress == "cancelled"
    ex.shutdown()


def test_cancel_returns_false_for_terminal_job():
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=1)
    job = store.create("x")
    store.update(job.id, state=JobState.DONE)
    assert ex.cancel(job.id) is False
    ex.shutdown()


def test_cancel_returns_false_for_unknown_job():
    ex = JobExecutor(MemoryJobStore(), max_concurrency=1)
    assert ex.cancel("nope") is False
    ex.shutdown()


def test_cancelled_job_does_not_run(monkeypatch):
    """run_job must not start work already marked terminal."""
    store = MemoryJobStore()
    called: list[str] = []

    def fake_transcribe(source, **kwargs):
        called.append(source)
        return _ok_result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    job = store.create("x")
    store.update(job.id, state=JobState.CANCELLED)
    runner.run_job(job, store, source="x")

    assert called == []
    assert store.get(job.id).state is JobState.CANCELLED


def test_done_is_not_overwritten_by_cancellation(monkeypatch):
    """If work finishes while a cancellation is in flight, CANCELLED wins."""
    store = MemoryJobStore()

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        # Cancel from "elsewhere" mid-run without going through the token, which
        # is the race the final re-read in run_job exists to handle.
        store.update(job_id_holder[0], state=JobState.CANCELLED)
        return _ok_result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    job = store.create("x")
    job_id_holder = [job.id]
    runner.run_job(job, store, source="x")

    assert store.get(job.id).state is JobState.CANCELLED


# --- default executor -----------------------------------------------------

def test_default_executor_is_cached(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    reset_default_executor()
    try:
        assert get_default_executor() is get_default_executor()
    finally:
        reset_default_executor()


def test_default_executor_shares_default_store(monkeypatch):
    from textflowkit.core.jobs import get_default_store, reset_default_store

    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    reset_default_store()
    reset_default_executor()
    try:
        assert get_default_executor().store is get_default_store()
    finally:
        reset_default_executor()
        reset_default_store()


def test_cancel_during_fetch_ends_cancelled_not_error(monkeypatch, tmp_path):
    """A cancellation raised mid-fetch must surface as CANCELLED.

    The source layer catches broad exceptions while downloading. If it does not
    re-raise cancellation first, an orderly stop is reported as a failure -
    which is exactly the bug this covers.
    """
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=1)
    fetch_started = threading.Event()
    release = threading.Event()

    from textflowkit.core import pipeline

    def fake_fetch(ref, *, work_dir, cookies_from_browser=None, check_cancel=None):
        fetch_started.set()
        release.wait(timeout=5)
        if check_cancel:
            check_cancel()          # simulates the progress hook firing
        raise AssertionError("unreachable")   # pragma: no cover

    monkeypatch.setattr(pipeline, "fetch_media", fake_fetch)
    # the source layer's own broad catch is what we are testing around, so use it
    monkeypatch.setattr("textflowkit.sources.acquire.require_tool", lambda *a, **k: "ffmpeg")

    job = ex.submit(source="https://example.com/v")
    assert fetch_started.wait(timeout=5)
    assert ex.cancel(job.id) is True
    release.set()

    deadline = time.time() + 5
    while time.time() < deadline and store.get(job.id).state is not JobState.CANCELLED:
        time.sleep(0.02)

    got = store.get(job.id)
    assert got.state is JobState.CANCELLED, f"expected cancelled, got {got.state}: {got.error}"
    assert got.error is None
    ex.shutdown()


def test_cancelled_error_is_shared_leaf_type():
    """The source layer and the executor must agree on one cancellation type."""
    from textflowkit.core.cancel import CancelledError
    from textflowkit.core.executor import JobCancelled

    assert JobCancelled is CancelledError
