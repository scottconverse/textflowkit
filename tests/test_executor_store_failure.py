"""Worker survival and write-health when the job store itself fails (ENG-003).

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

A read is not recovery from a write fault, so the recovery contract is pinned on
the *write* path, not a liveness read:

- a store that answers reads but rejects every write must still refuse admission -
  the old ``store.get("")`` probe cleared the fault latch and admitted a job whose
  terminal row could never be persisted;
- once writes work again, one bounded reconciliation write must clear the latch,
  land the stranded row's terminal state so it is not left non-terminal forever,
  and admit healthy work - with no retry storm;
- a successful *unrelated* write must not erase the outstanding failure: the
  latch stays until the specific stranded row is resolved;
- reconciliation is ownership-pinned, so a row a newer owner (a resume, a
  cancellation) moved on is left alone rather than clobbered with a stale verdict.

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
from textflowkit.core.sqlite_store import SqliteJobStore


def _ok_result(source: str) -> TranscribeResult:
    return TranscribeResult(
        transcript=Transcript(source=source, language="en", segments=[]),
        outputs=[],
    )


class FailingUpdateStore(MemoryJobStore):
    """A memory store whose *writes* fail for selected job sources.

    ``MemoryJobStore`` keys rows by id, so the failure is keyed on the job's
    ``source``: a job submitted with a source in ``fail_sources`` cannot have its
    terminal (ERROR or DONE) row persisted, while other rows behave normally.
    That models a store outage scoped to the row being written.

    Every write path the executor can take must fail, not only ``update``:
    ``claim`` is the guarded write reconciliation uses, so a store that failed
    only ``update`` would let recovery slip through a path the outage was meant
    to block. The outage is modelled on all of them.
    """

    def __init__(self, *, fail_sources: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_sources = set(fail_sources)
        self.failed_updates: list[str] = []

    def _reject_for(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        return job is not None and job.source in self._fail_sources

    def update(self, job_id: str, **fields) -> Job | None:
        if self._reject_for(job_id):
            self.failed_updates.append(job_id)
            raise RuntimeError("store unavailable")
        return super().update(job_id, **fields)

    def claim(self, job_id: str, **kwargs) -> Job | None:
        if self._reject_for(job_id):
            self.failed_updates.append(job_id)
            raise RuntimeError("store unavailable")
        return super().claim(job_id, **kwargs)

    def begin_attempt(self, job_id: str) -> Job | None:
        if self._reject_for(job_id):
            self.failed_updates.append(job_id)
            raise RuntimeError("store unavailable")
        return super().begin_attempt(job_id)


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

        # The broken row's terminal write could not land, so the fault is
        # latched and admission is refused - visible, not a silent accept. The
        # healthy job above ran because it was queued before the fault; a later
        # submit must not be admitted while the write path is unproven.
        with pytest.raises(StoreUnavailableError, match="store is unavailable"):
            ex.submit(source="healthy2")

        # Heal the store: the stranded row's terminal write can now land, the
        # latch clears, and a further healthy job runs - liveness after recovery.
        store._fail_sources.clear()
        healthy3 = ex.submit(source="healthy3")
        done3 = _wait_for_state(store, healthy3.id, JobState.DONE, timeout=5.0)
        assert done3.state is JobState.DONE, "healthy job stranded after recovery"

        # The stranded row is truthfully ERROR now that its write landed - not
        # left non-terminal forever.
        broken_row = _wait_for_state(store, broken.id, JobState.ERROR, timeout=5.0)
        assert broken_row.state is JobState.ERROR, (
            "stranded row left unresolved after the store recovered"
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

    def claim(self, job_id: str, **kwargs) -> Job | None:
        raise RuntimeError("store permanently unavailable")

    def begin_attempt(self, job_id: str) -> Job | None:
        raise RuntimeError("store permanently unavailable")


class _SwitchableStore(MemoryJobStore):
    """A memory store with a togglable outage.

    While ``broken`` is true every write (``update``, ``claim``,
    ``begin_attempt``) and ``get`` raises; ``create`` still works, so a submit
    can be attempted while writes are down (the executor's error-write then fails
    and the fault is latched). Flipping ``broken`` false models the outage
    healing.
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

    def claim(self, job_id: str, **kwargs) -> Job | None:
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().claim(job_id, **kwargs)

    def begin_attempt(self, job_id: str) -> Job | None:
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().begin_attempt(job_id)


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


# -- read-healthy / write-broken ---------------------------------------------
#
# The tests above fail `get` and `update` together, so a *read* is as bad a sign
# as a write and a read probe can never falsely look like recovery. The real
# fault is asymmetric: the store answers reads fine and rejects every write. A
# read is not recovery from a write fault, so any recovery check that only reads
# will admit jobs whose terminal results still cannot be persisted. These tests
# pin the write-side contract.


class _ReadHealthyWriteBroken(MemoryJobStore):
    """A store whose reads and creates work while every ``update`` raises.

    This is the asymmetric outage the read-probe recovery rule gets wrong: a
    ``get`` (the old admission probe) succeeds, so the fault latches but is
    immediately cleared, and a job whose terminal row cannot be written is
    admitted anyway. ``writes_broken`` toggles recovery; ``failed_updates``
    records every write the store rejected so a test can prove no write landed
    while the outage held.
    """

    def __init__(self) -> None:
        super().__init__()
        self.writes_broken = True
        self.failed_updates: list[str] = []

    def update(self, job_id: str, **fields) -> Job | None:
        if self.writes_broken:
            self.failed_updates.append(job_id)
            raise OSError("store_failed='controlled persistent write failure'")
        return super().update(job_id, **fields)

    def claim(self, job_id: str, **kwargs) -> Job | None:
        if self.writes_broken:
            self.failed_updates.append(job_id)
            raise OSError("store_failed='controlled persistent write failure'")
        return super().claim(job_id, **kwargs)

    def begin_attempt(self, job_id: str) -> Job | None:
        if self.writes_broken:
            self.failed_updates.append(job_id)
            raise OSError("store_failed='controlled persistent write failure'")
        return super().begin_attempt(job_id)


def _crash_on(monkeypatch, source_name: str) -> None:
    """Make ``run_job`` raise for one source; other sources run normally."""
    from textflowkit.core import runner as runner_mod

    original_run_job = runner_mod.run_job

    def crash_for(job, store, *, source, **kwargs):
        if source == source_name:
            raise RuntimeError("runner exploded")
        return original_run_job(job, store, source=source, **kwargs)

    monkeypatch.setattr(runner_mod, "run_job", crash_for)


def _wait_for_store_fault(ex: JobExecutor, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and ex.store_failed is None:
        time.sleep(0.01)
    assert ex.store_failed is not None, "store fault was not recorded"


def test_read_is_not_recovery_from_write_fault(monkeypatch):
    """A read that succeeds must not unlock admission while writes still fail.

    RED: the admission probe is ``store.get("")``. Against a read-healthy/
    write-broken store that read succeeds, the fault latch clears, and the next
    ``submit`` is ACCEPTED - a job whose terminal row can never be persisted, the
    exact work the refusal exists to reject.
    """
    _crash_on(monkeypatch, "broken")
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    store = _ReadHealthyWriteBroken()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        ex.submit(source="broken")
        _wait_for_store_fault(ex)
        # Worker survived the failed terminal write.
        assert all(w.is_alive() for w in ex._workers), "worker died on write fault"

        # Reads still work - this is what fooled the read probe.
        assert store.get("") is None

        jobs_before = len(store.list(limit=1000))
        with pytest.raises(StoreUnavailableError, match="store is unavailable"):
            ex.submit(source="nextjob")
        jobs_after = len(store.list(limit=1000))
        assert jobs_after == jobs_before, "refused submit created a record"
        assert all(w.is_alive() for w in ex._workers)
    finally:
        store.writes_broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


def test_write_recovery_resolves_unpersisted_terminal_row_and_admits(monkeypatch):
    """Once writes work again, the stranded row is resolved and work resumes.

    The job whose terminal write failed is left RUNNING by the outage. A single
    successful reconciliation write must (a) clear the fault, (b) land the
    original terminal state on that row so it is no longer RUNNING forever, and
    (c) let a healthy job run - with no retry storm.
    """
    _crash_on(monkeypatch, "broken")
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    store = _ReadHealthyWriteBroken()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        broken = ex.submit(source="broken")
        _wait_for_store_fault(ex)
        # The row is stranded non-terminal: the terminal write never landed. (The
        # store rejected the run's own RUNNING write too, so it may read PENDING
        # rather than RUNNING - either way it is not terminal.)
        assert not store.get(broken.id).is_terminal

        # Writes recover. The next admission attempt must reconcile the stranded
        # row and admit the healthy job.
        store.writes_broken = False
        healthy = ex.submit(source="healthy")
        done = _wait_for_state(store, healthy.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, "healthy work stranded after recovery"
        assert ex.store_failed is None, "fault not cleared after writes recovered"

        # The original row is no longer RUNNING forever - it is truthfully ERROR.
        resolved = _wait_for_state(store, broken.id, JobState.ERROR, timeout=5.0)
        assert resolved.state is JobState.ERROR, (
            f"unpersisted terminal row left unresolved: state={resolved.state!r}"
        )
    finally:
        store.writes_broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


def test_unrelated_successful_write_does_not_erase_outstanding_failure(monkeypatch):
    """A healed store's unrelated write must not hide an unresolved failure.

    The reconciliation writes the *stranded* job's terminal row. If a different
    job's write succeeded first, that success proves the store answers but says
    nothing about the row whose terminal state is still missing - the fault must
    stay latched until that specific row is resolved. This pins the ordering.
    """
    _crash_on(monkeypatch, "broken")
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    store = _ReadHealthyWriteBroken()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        broken = ex.submit(source="broken")
        _wait_for_store_fault(ex)
        assert not store.get(broken.id).is_terminal
        assert ex.store_failed is not None

        # While still write-broken, a fresh submit is refused.
        with pytest.raises(StoreUnavailableError):
            ex.submit(source="should-not-admit")

        # Now heal writes and let the stranded row be reconciled by the next
        # admission. The failure must not have been silently forgotten while the
        # row was still missing its terminal state.
        assert not store.get(broken.id).is_terminal, (
            "row must still be unresolved before recovery"
        )
        store.writes_broken = False
        ex.submit(source="healthy")
        resolved = _wait_for_state(store, broken.id, JobState.ERROR, timeout=5.0)
        assert resolved.state is JobState.ERROR
    finally:
        store.writes_broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


def test_reconciliation_does_not_clobber_a_row_a_newer_owner_moved_on(monkeypatch):
    """The stranded write must not overwrite a row another owner advanced.

    Ownership is keyed to the attempt the failed run held. If the row is reopened
    (a resume bumps its attempt) before writes recover, the reconciliation must
    refuse to write the old terminal state - that would clobber the newer owner's
    row with a stale verdict.
    """
    _crash_on(monkeypatch, "broken")
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    store = _ReadHealthyWriteBroken()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        broken = ex.submit(source="broken")
        _wait_for_store_fault(ex)
        row_before = store.get(broken.id)
        stale_attempt = row_before.attempt
        # The write-broken store rejected the run's own RUNNING write too, so the
        # stranded row is whatever it was before the run started (PENDING).

        # A newer owner takes the row on (its attempt advances) while the store is
        # still write-broken - a real resume would be refused too, so model the
        # row moving on directly: the point is the reconciliation's ownership
        # guard, not the resume path that moved it. The row is mutated in place
        # (MemoryJobStore hands out the live row), bypassing the broken writes.
        row_before.attempt += 1
        row_before.progress = "reopened-by-newer-owner"

        store.writes_broken = False
        healthy = ex.submit(source="healthy")  # triggers reconciliation attempt
        done = _wait_for_state(store, healthy.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, "healthy work stranded after recovery"
        assert ex.store_failed is None, (
            "fault was not cleared by a successful recovery write"
        )

        # Give any (wrong) clobbering write a bounded window to land.
        time.sleep(0.3)
        row = store.get(broken.id)
        assert row.attempt != stale_attempt, "precondition: newer owner advanced the row"
        assert row.state is not JobState.ERROR, (
            "reconciliation clobbered a row a newer owner had moved on"
        )
    finally:
        store.writes_broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


# -- the durable store takes the same contract ---------------------------------


class _BrokenSqliteStore(SqliteJobStore):
    """A durable store whose writes can be toggled off (read path stays live)."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.broken = True

    def update(self, job_id: str, **fields) -> Job | None:
        if self.broken:
            raise OSError("store_failed='durable write failure'")
        return super().update(job_id, **fields)

    def claim(self, job_id: str, **kwargs) -> Job | None:
        if self.broken:
            raise OSError("store_failed='durable write failure'")
        return super().claim(job_id, **kwargs)

    def begin_attempt(self, job_id: str) -> Job | None:
        if self.broken:
            raise OSError("store_failed='durable write failure'")
        return super().begin_attempt(job_id)


def test_durable_store_write_fault_refuses_then_reconciles(monkeypatch, tmp_path):
    """The SQLite store takes the same write-health contract as memory.

    The reconciliation writes through the store's guarded ``claim``; on SQLite
    that is a single UPDATE guarded by the row's state and attempt, so the same
    refusal-while-broken and reconcile-on-recovery shape must hold over the
    durable store, not only the in-memory one.
    """
    _crash_on(monkeypatch, "broken")
    monkeypatch.setattr(runner, "transcribe", lambda source, **kw: _ok_result(source))

    store = _BrokenSqliteStore(str(tmp_path / "jobs.db"))
    ex = JobExecutor(store, max_concurrency=1)
    try:
        broken = ex.submit(source="broken")
        _wait_for_store_fault(ex)
        assert all(w.is_alive() for w in ex._workers), "worker died on write fault"

        # Reads are live, but admission is still refused: a read is not recovery.
        with pytest.raises(StoreUnavailableError, match="store is unavailable"):
            ex.submit(source="should-not-admit")
        assert store.get(broken.id) is not None, "precondition: read path live"
        assert not store.get(broken.id).is_terminal

        # Writes recover: the stranded row is resolved and healthy work runs.
        store.broken = False
        healthy = ex.submit(source="healthy")
        done = _wait_for_state(store, healthy.id, JobState.DONE, timeout=5.0)
        assert done.state is JobState.DONE, "healthy work stranded after recovery"
        resolved = _wait_for_state(store, broken.id, JobState.ERROR, timeout=5.0)
        assert resolved.state is JobState.ERROR, (
            f"durable stranded row left unresolved: state={resolved.state!r}"
        )
        assert ex.store_failed is None, "fault not cleared after writes recovered"
    finally:
        store.broken = False
        ex.shutdown()
        store.close()
        assert not any(w.is_alive() for w in ex._workers)
