"""Worker survival when the job store itself fails (ENG-003).

The executor's error handler writes a terminal ERROR row for a job whose runner
raised. That write goes to the same store the runner used, so it can fail too.
The existing ``test_worker_records_crash_and_runs_next_job`` exercises a runner
crash against a *working* ``MemoryJobStore``; it never fails the error write. If
the error write raises and the worker does not protect it, the exception escapes
``_worker``, the thread dies, and the pool silently loses capacity: later healthy
jobs sit in the queue forever. These tests pin that down, and the recovery shape:

- a transient store outage (a single failed get/update) must not permanently
  strand future healthy work;
- a sustained store failure must not silently swallow capacity either - the
  failure must become visible and further admission must be refused rather than
  accepting jobs that cannot be run.

Every wait is bounded and every executor is shut down and asserted to leave no
live worker threads. No real inference or network: ``transcribe`` is stubbed.
"""

from __future__ import annotations

import threading
import time

import pytest

from textflowkit.core import runner
from textflowkit.core.executor import JobExecutor

try:  # added by this unit's fix; absent at the RED baseline
    from textflowkit.core.executor import StoreUnavailableError
except ImportError:  # pragma: no cover - baseline only
    StoreUnavailableError = None  # type: ignore[assignment]
from textflowkit.core.jobs import Job, JobState, JobStore, MemoryJobStore
from textflowkit.core.model import Transcript
from textflowkit.core.pipeline import TranscribeResult


def _ok_result(source: str) -> TranscribeResult:
    return TranscribeResult(
        transcript=Transcript(source=source, language="en", segments=[]),
        outputs=[],
    )


class FailingUpdateStore(MemoryJobStore):
    """A memory store whose ``update`` fails for selected job sources.

    ``MemoryJobStore`` keys rows by id, so the failure is keyed on the job's
    ``source``: a job submitted with a source in ``fail_sources`` cannot have its
    terminal (ERROR or DONE) row persisted, while other rows behave normally.
    That models a store outage scoped to the row being written.
    """

    def __init__(self, *, fail_sources: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_sources = set(fail_sources)
        self.failed_updates: list[str] = []

    def update(self, job_id: str, **fields) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None and job.source in self._fail_sources:
            self.failed_updates.append(job_id)
            raise RuntimeError("store unavailable")
        return super().update(job_id, **fields)


def _wait_for_state(store: JobStore, job_id: str, state: JobState, timeout: float = 5.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job is not None and job.state is state:
            return job
        time.sleep(0.01)
    job = store.get(job_id)
    return job  # caller asserts; returning the last read keeps the message useful


def test_worker_survives_error_write_failure_and_runs_next_job(monkeypatch):
    """A failing error write must not kill the worker or strand later jobs.

    RED: the inner ``except Exception`` in ``_worker`` calls ``store.update``
    with no nested protection, so a store failure there escapes the loop, the
    single worker thread dies, and the healthy job behind it is never picked up.
    """
    from textflowkit.core import runner as runner_mod

    original_run_job = runner_mod.run_job

    def crash_for_broken(job, store, *, source, **kwargs):
        if source == "broken":
            raise RuntimeError("runner exploded")
        return original_run_job(job, store, source=source, **kwargs)

    monkeypatch.setattr(runner_mod, "run_job", crash_for_broken)
    monkeypatch.setattr(runner_mod, "transcribe", lambda source, **kw: _ok_result(source))

    store = FailingUpdateStore(fail_sources={"broken"})
    ex = JobExecutor(store, max_concurrency=1)
    try:
        broken = ex.submit(source="broken")
        healthy = ex.submit(source="healthy")

        # The healthy job must still run: the error-write failure is about one
        # row, not the pool.
        done = _wait_for_state(store, healthy.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, (
            f"healthy job stranded after error-write failure: state={done.state!r} "
            f"error={done.error!r}; worker alive="
            f"{[w.is_alive() for w in ex._workers]}"
        )

        # The worker thread is alive (capacity preserved).
        assert all(w.is_alive() for w in ex._workers), "worker thread died"

        # A worker that dies would leave a thread that never runs again; prove
        # liveness by running one more healthy job after the failure.
        healthy2 = ex.submit(source="healthy2")
        done2 = _wait_for_state(store, healthy2.id, JobState.DONE, timeout=5.0)
        assert done2.state is JobState.DONE, "second healthy job stranded"

        # We must NOT claim a durable ERROR row when the store could not persist
        # it - the row's last persisted state is whatever it was before.
        broken_row = store.get(broken.id)
        assert broken_row.state is not JobState.ERROR, (
            "test must not assert success when the store cannot persist ERROR; "
            "if this fails the store is now succeeding, which invalidates the case"
        )
    finally:
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers), "shutdown left threads"


def test_transient_get_failure_does_not_strand_future_work(monkeypatch):
    """A one-shot ``get`` failure must not permanently strand later jobs.

    Models a transient outage that heals: the first read fails, subsequent ones
    succeed. The executor must not lose the worker over it; the affected job may
    not reach its intended terminal state, but a healthy later job must still run.
    """
    store = MemoryJobStore()
    flaky = _FlakyStore(store, fail_gets=1)
    ex = JobExecutor(flaky, max_concurrency=1)

    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    try:
        # Submit a first job; its store read fails once (the flaky get). We do
        # not assert its terminal state - the transient failure may leave it
        # PENDING - only that the pool survives and later work still runs.
        ex.submit(source="first")
        # Give the flaky get a chance to fire on the first job.
        time.sleep(0.1)
        # A subsequent healthy job must run: a single transient failure does not
        # remove capacity.
        second = ex.submit(source="second")
        done = _wait_for_state(store, second.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, (
            f"transient get failure stranded later work: state={done.state!r}; "
            f"workers alive={[w.is_alive() for w in ex._workers]}"
        )
        assert all(w.is_alive() for w in ex._workers)
    finally:
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


class _FlakyStore(MemoryJobStore):
    """Memory store that fails the first N ``get`` calls, then behaves."""

    def __init__(self, inner: MemoryJobStore, *, fail_gets: int) -> None:
        super().__init__()
        # Delegate storage to the inner store's dicts so both views agree.
        self._jobs = inner._jobs
        self._order = inner._order
        self._lock = inner._lock
        self._max_jobs = inner._max_jobs
        self._fail_gets = fail_gets
        self._get_count = 0
        self._count_lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._count_lock:
            self._get_count += 1
            n = self._get_count
        if n <= self._fail_gets:
            raise RuntimeError("store get unavailable")
        return super().get(job_id)


class _AlwaysFailingStore(MemoryJobStore):
    """A store whose updates always fail (persistent outage)."""

    def update(self, job_id: str, **fields) -> Job | None:
        raise RuntimeError("store permanently unavailable")


class _SwitchableStore(MemoryJobStore):
    """A memory store with a togglable outage.

    While ``broken`` is true every ``update`` and ``get`` raises; ``create``
    still works, so a submit can be attempted while writes are down (the
    executor's error-write then fails and the fault is latched). Flipping
    ``broken`` false models the outage healing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def get(self, job_id: str) -> Job | None:
        if self.broken:
            raise RuntimeError("store read unavailable")
        return super().get(job_id)

    def update(self, job_id: str, **fields) -> Job | None:
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().update(job_id, **fields)


def test_transient_error_write_failure_recovers_on_next_admission(monkeypatch):
    """A fault that heals must not keep admission closed.

    The worker's error write fails while the store is down (fault latched). The
    store then heals. The next submit must be admitted - the admission probe
    clears the transient fault - and the job must reach DONE. This is the clause
    "a transient store outage must not permanently strand future healthy work".
    """
    from textflowkit.core import runner as runner_mod

    original_run_job = runner_mod.run_job

    def crash_for_broken(job, store, *, source, **kwargs):
        if source == "broken":
            raise RuntimeError("runner exploded")
        return original_run_job(job, store, source=source, **kwargs)

    monkeypatch.setattr(runner_mod, "run_job", crash_for_broken)
    monkeypatch.setattr(runner_mod, "transcribe", lambda source, **kw: _ok_result(source))

    store = _SwitchableStore()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        # Bring the store down, then crash a job so its ERROR write cannot land.
        store.broken = True
        ex.submit(source="broken")

        # Fault becomes visible.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ex.store_failed is None:
            time.sleep(0.01)
        assert ex.store_failed is not None, "store fault was not recorded"
        assert all(w.is_alive() for w in ex._workers), "worker died on error-write failure"

        # Store heals; the next submit must be admitted (probe clears the latch).
        store.broken = False
        healthy = ex.submit(source="healthy")
        done = _wait_for_state(store, healthy.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, (
            f"healthy work stranded after transient outage: state={done.state!r}"
        )
        assert ex.store_failed is None, "fault not cleared after store recovered"
    finally:
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


def test_sustained_store_failure_refuses_admission_visibly(monkeypatch):
    """While the store stays down, admission is refused with a visible error.

    A persistent outage makes execution unsafe: jobs would be accepted whose
    results cannot be recorded. The executor refuses with a store-unavailable
    error (not a silent accept, not a generic queue-full) and keeps its workers.
    """
    from textflowkit.core import runner as runner_mod

    original_run_job = runner_mod.run_job

    def crash_for_broken(job, store, *, source, **kwargs):
        if source == "broken":
            raise RuntimeError("runner exploded")
        return original_run_job(job, store, source=source, **kwargs)

    monkeypatch.setattr(runner_mod, "run_job", crash_for_broken)
    monkeypatch.setattr(runner_mod, "transcribe", lambda source, **kw: _ok_result(source))

    store = _SwitchableStore()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        store.broken = True
        ex.submit(source="broken")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ex.store_failed is None:
            time.sleep(0.01)
        assert ex.store_failed is not None

        # Still down: admission is refused, visibly, and no job is created.
        assert StoreUnavailableError is not None, "executor has no store-unavailable error"
        with pytest.raises(StoreUnavailableError, match="store is unavailable"):
            ex.submit(source="should-not-admit")
        assert all(w.is_alive() for w in ex._workers)
    finally:
        store.broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


def test_sustained_store_failure_becomes_visible_and_refuses_admission(monkeypatch):
    """Persistent storage failure must be visible, not silently accepted."""
    store = _AlwaysFailingStore()
    ex = JobExecutor(store, max_concurrency=1)
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))
    try:
        ex.submit(source="doomed")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ex.store_failed is None:
            time.sleep(0.01)
        assert ex.store_failed is not None, "sustained store failure was silent"
    finally:
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)
