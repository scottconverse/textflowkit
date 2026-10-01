"""Bounded job executor.

`submit()` runs jobs on a fixed worker pool that owns job lifecycle, so the
number of simultaneous model runs is capped by configuration instead of by how
many jobs were submitted. On a device Whisper already saturates, extra parallel
jobs do not go faster - they thrash memory.

The pool holds a reference to every running job, which is what gives
cancellation something to act on. Cancellation is cooperative: a job checks a
token at stage boundaries and stops cleanly. A job inside a single long model
call cannot be interrupted mid-call - that call finishes, then the job stops.
Stated plainly here because it is a real limit, not an implementation detail.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from textflowkit.core.cancel import CancelledError
from textflowkit.core.jobs import Job, JobState, JobStore, get_default_store

ENV_CONCURRENCY = "TEXTFLOWKIT_MAX_CONCURRENCY"
DEFAULT_CONCURRENCY = 1
ENV_MAX_PENDING = "TEXTFLOWKIT_MAX_PENDING_JOBS"
DEFAULT_MAX_PENDING = 100


class QueueFullError(RuntimeError):
    """The executor cannot accept another pending job right now."""


class StoreUnavailableError(RuntimeError):
    """The executor is refusing admission because its store failed.

    Distinct from `QueueFullError`: that is backpressure (the queue is full and
    will drain), this is an infrastructure fault. A caller that retries on full
    should not retry this the same way - the store must recover first, which the
    executor re-checks on the next admission attempt.
    """


# The signal itself lives in a leaf module so the source layer can re-raise it
# around broad exception handling. This alias keeps the name used everywhere
# else in the codebase and in tests.
JobCancelled = CancelledError


@dataclass
class TerminalWrite:
    """A terminal row a run could not persist, kept for bounded reconciliation.

    ``fields`` is the exact patch the run meant to write (state, progress, error,
    transcript, outputs). ``attempt`` is the execution identity the run owned, so
    reconciliation can require the row to still carry it: a row a later resume
    reopened (attempt advanced) is left to its new owner instead of being
    overwritten with this run's stale verdict.

    ``attempt`` may be ``None``: the run's start write never landed, so the
    identity it owned is *unknown* - it was never captured. An unknown identity
    must stay unresolved rather than be guessed, because a guarded write pinned
    to a guessed identity that matches nothing would read as "already resolved"
    and clear the latch while the row is still stranded. ``None`` therefore makes
    reconciliation refuse to spend the entry (it stays outstanding) rather than
    treat a no-match as success.

    ``allowed_states`` bounds the guarded write further: reconciliation must only
    land on a row still in the state this run left it in (PENDING/RUNNING), never
    on one that reached a terminal state by another path (e.g. a cancellation).
    """

    job_id: str
    attempt: int | None
    fields: dict[str, Any] = field(default_factory=dict)
    allowed_states: frozenset[JobState] = field(
        default_factory=lambda: frozenset({JobState.PENDING, JobState.RUNNING})
    )


class CancelToken:
    """A cooperative cancellation flag, checked at stage boundaries."""

    __slots__ = ("_cancelled", "_lock")

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def checkpoint(self) -> None:
        """Raise `JobCancelled` if cancellation was requested."""
        if self.cancelled:
            raise JobCancelled()


class JobExecutor:
    """A fixed pool of workers that run jobs from a queue.

    The pool size is the concurrency bound. Defaults to 1 because Whisper
    saturates a GPU on its own; override with `TEXTFLOWKIT_MAX_CONCURRENCY`.
    """

    def __init__(
        self,
        store: JobStore,
        *,
        max_concurrency: int | None = None,
        max_pending: int | None = None,
    ) -> None:
        self._store = store
        if max_concurrency is None:
            raw = os.environ.get(ENV_CONCURRENCY)
            try:
                max_concurrency = int(raw) if raw else DEFAULT_CONCURRENCY
            except ValueError:
                max_concurrency = DEFAULT_CONCURRENCY
        self._max_concurrency = max(1, max_concurrency)

        if max_pending is None:
            raw = os.environ.get(ENV_MAX_PENDING)
            try:
                max_pending = int(raw) if raw else DEFAULT_MAX_PENDING
            except ValueError:
                max_pending = DEFAULT_MAX_PENDING
        self._max_pending = max(1, max_pending)
        # This semaphore counts queued jobs, not running workers. It also lets
        # shutdown enqueue sentinels without deadlocking on a full queue.
        self._pending_slots = threading.BoundedSemaphore(self._max_pending)

        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._tokens: dict[str, CancelToken] = {}
        # Ids the executor owns: queued but not yet picked up, plus running. This
        # is the authoritative ownership set, and it is updated under `self._lock`
        # at both ends of a job's life in the pool:
        #
        # - `enqueue` records the id *before* it puts the item on the queue, so a
        #   duplicate enqueue cannot slip in even though the deque itself is not
        #   lock-protected (a `queue.Queue` has its own internal lock; scanning
        #   `_queue.queue` by hand is not synchronised against the worker's
        #   `get`) and even though the worker only registers a cancel token *after*
        #   it dequeues. The dequeue-to-token gap is exactly the window a
        #   deque-scan guard misses; an owned-id set does not have that window.
        # - the worker keeps the id owned across the run and drops it in the same
        #   critical section that pops the token, so ownership spans the whole
        #   queued/running lifetime and nothing external can observe a hole.
        self._owned: set[str] = set()
        # The execution identity each live run owns, keyed by job id: the attempt
        # its row carries, captured from the successful start (`begin_attempt`)
        # and, before that, from the worker's own pre-start read. Held under
        # `self._lock` and dropped when the run ends. It exists so a run's error
        # handler pins repair to the identity the run actually owned instead of
        # re-reading the store after a fault - a store outage that also breaks
        # reads would otherwise force a guess (0) and lose ownership of a row that
        # is really at a positive attempt, the identity-loss failure.
        self._owned_attempts: dict[str, int | None] = {}
        self._lock = threading.RLock()
        self._started = False
        self._shutdown = False
        # The last store failure the pool could not persist past, or None when
        # the store last answered. This is the *visible* half of the worker-
        # survival policy: a store that cannot record a terminal row is an
        # infrastructure fault, and it is not swallowed - it gates admission
        # (see `submit`/`enqueue`) so the executor does not accept jobs whose
        # results it could not record.
        #
        # A read is not recovery from a write fault: a store can answer `get`
        # perfectly while rejecting every `update`. So this latch is cleared only
        # by a *successful write* - the reconciliation of a stranded terminal row
        # (`_reconcile_pending_terminal`), never by a probe read and never by an
        # unrelated job's successful write. An unrelated success proves the store
        # has a write path but says nothing about the row whose terminal state is
        # still missing, so it must not erase the outstanding failure.
        self._store_failed: str | None = None
        # Terminal writes a worker could not persist, keyed by job id: the exact
        # fields the run intended to write, plus the attempt it owned. Bounded by
        # construction - one entry per stranded job, replaced (never appended) on
        # a repeat failure for the same id. Each is retried once per refused
        # admission, guarded by `observed_attempt`, so a row a newer owner moved
        # on is left alone rather than clobbered with a stale verdict.
        self._pending_terminal: dict[str, TerminalWrite] = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def store(self) -> JobStore:
        """The store this executor writes to."""
        return self._store

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def store_failed(self) -> str | None:
        """The last unpersisted store failure, or None when no row is stranded.

        Set by a terminal-write failure in a worker; cleared only when a write
        lands again (the reconciliation of the stranded row), never by a read and
        never by an unrelated job's successful write. Callers and adapters read
        this to report an infrastructure fault instead of a job failure - the
        job's own row may still read PENDING/RUNNING because the write that would
        have failed it never landed.
        """
        with self._lock:
            return self._store_failed

    def _record_store_failure(
        self, exc: BaseException, *, pending: TerminalWrite | None = None
    ) -> None:
        """Note a store fault without raising; keeps the worker alive.

        When the failed write was a terminal row, its exact patch and attempt are
        kept in ``_pending_terminal`` so a later recovery write can resolve the
        row truthfully instead of leaving it RUNNING forever. Recording replaces
        any earlier entry for the same id rather than appending, so the pending
        set stays bounded by the number of stranded jobs.
        """
        with self._lock:
            self._store_failed = f"{type(exc).__name__}: {exc}"
            if pending is not None:
                self._pending_terminal[pending.job_id] = pending

    def _clear_store_failure(self) -> None:
        """A write landed, so admission may reopen."""
        with self._lock:
            self._store_failed = None

    def _reconcile_pending_terminal(self) -> bool:
        """One bounded write that both tests the write path and heals a row.

        Called on the admission path while the fault latch is set. It retries the
        oldest stranded terminal write through the store's guarded ``claim``:
        the write only lands if the row is still in the state this run left it in
        *and* still carries the attempt the run owned, so a row a newer owner
        moved on (a resume, a cancellation) is left alone - the stale verdict is
        never forced onto it.

        Returns True only when *every* stranded row has been resolved (the write
        path is proven healthy and nothing remains outstanding) so the latch may
        clear; False when the store still rejects writes, or when a stranded row
        remains (this call resolved one, but others are still outstanding), so
        admission stays refused. One store call per refused admission, never a
        loop - no retry storm; each later refused admission spends one more.

        A row whose owned identity is unknown (``attempt is None``) is handled
        without a guess: recovery tries to *read* the row to learn the attempt
        currently on it, and pins the guarded write to that. Reading to pin a
        write is not the forbidden "read as recovery" - the latch still clears
        only when a *write* lands. If the read also fails, the entry stays
        outstanding (unresolved) rather than being cleared on a guessed identity.
        """
        with self._lock:
            pending = next(iter(self._pending_terminal.values()), None)

        if pending is None:
            # No stranded row to write, yet the latch is set. Today every latched
            # failure is a terminal write, so this cannot happen; if it ever does
            # the write path is unproven, so admission stays refused (fail-safe)
            # rather than cleared on a read that says nothing about writes.
            return False

        observed_attempt = pending.attempt
        if observed_attempt is None:
            # The run's start never landed and no pre-start identity was captured,
            # so pinning is not yet possible. Learn the row's current attempt from
            # a read so the guarded write can be keyed to a real identity - a read
            # here only *pinpoints* the write, it never clears the latch. If the
            # read fails too, the identity stays unknown and the entry stays
            # outstanding: a conditional write that matches nothing must not be
            # read as "resolved" when we do not know what it should have matched.
            try:
                row = self._store.get(pending.job_id)
            except Exception:  # noqa: BLE001 - a broken read leaves it unresolved
                return False
            observed_attempt = None if row is None else row.attempt

        if observed_attempt is None:
            # Row absent: there is nothing left to resolve for this id.
            with self._lock:
                self._pending_terminal.pop(pending.job_id, None)
                drained = not self._pending_terminal
                if drained:
                    self._clear_store_failure()
            return drained

        try:
            # Guarded: only lands on a row still at the attempt/states this run
            # left it. None means "row moved on / already terminal" - that is a
            # *success* of the store (the call completed), so the entry is spent.
            self._store.claim(
                pending.job_id,
                allowed_states=pending.allowed_states,
                observed_attempt=observed_attempt,
                **pending.fields,
            )
        except Exception:  # noqa: BLE001 - a failed write keeps the fault latched
            return False

        with self._lock:
            self._pending_terminal.pop(pending.job_id, None)
            drained = not self._pending_terminal
            if drained:
                self._clear_store_failure()
        return drained

    def _refuse_if_store_failed(self) -> None:
        """Gate admission on store health. Called under `self._lock`.

        While the last terminal write could not be persisted, the executor
        refuses new work rather than accepting jobs whose results it cannot
        record - the failure is made visible (this error names it) instead of
        silently accepting work that cannot run. Before refusing we spend one
        bounded *write* (``_reconcile_pending_terminal``): if it lands, the write
        path is healthy again, the latch clears, and admission proceeds. A read
        is never accepted as recovery - a store that reads but cannot write stays
        refused, and the caller sees an infrastructure error, not a job failure.
        """
        if self._store_failed is None:
            return
        if self._reconcile_pending_terminal():
            return
        raise StoreUnavailableError(
            "job store is unavailable; refusing new work until it recovers "
            f"(last failure: {self._store_failed})"
        )

    def start(self) -> None:
        """Start worker threads. Idempotent; called lazily by submit()."""

        def _start_locked() -> None:
            for i in range(self._max_concurrency):
                t = threading.Thread(
                    target=self._worker,
                    name=f"textflowkit-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
            self._started = True

        with self._lock:
            if self._started or self._shutdown:
                return
            # A durable store may hold jobs left mid-flight by a previous
            # process. They have no worker now, so fail them rather than
            # reporting jobs that can never finish. This assumes one owning
            # process per store, which is the documented deployment model.
            self._store.reap_incomplete(
                reason="interrupted by restart; no worker is running this job"
            )
            _start_locked()

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop accepting work and drain the pool."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        for _ in self._workers:
            self._queue.put(None)  # sentinel: one per worker
        if wait:
            for t in self._workers:
                t.join(timeout=timeout)

    # -- work --------------------------------------------------------------

    def submit(
        self, *, source: str, request: dict[str, Any] | None = None, **kwargs: Any
    ) -> Job:
        """Queue a job and return it immediately."""
        self.start()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("job executor is shut down")
            self._refuse_if_store_failed()
            if not self._pending_slots.acquire(blocking=False):
                raise QueueFullError(
                    f"job queue is full ({self._max_pending} pending); retry later"
                )
            job: Job | None = None
            try:
                job = self._store.create(source, request=request)
                self._owned.add(job.id)
                self._queue.put_nowait((job.id, {"source": source, **kwargs}))
            except Exception:
                if job is not None:
                    self._owned.discard(job.id)
                self._pending_slots.release()
                raise
            return job

    def enqueue(self, job: Job, *, source: str, **kwargs: Any) -> Job:
        """Queue an existing prepared job (the durable resume path).

        Per-id ownership: a job id already owned by the pool - queued but not yet
        picked up, or running - is not enqueued again. The caller has claimed the
        row before calling, but a duplicate enqueue would put two queue entries
        (and later two worker tokens) on one job, so this is the last line of
        defence rather than the claim itself. Ownership is recorded in `_owned`
        under `self._lock` before the item is put on the queue, and the worker
        keeps it there until it finishes, so there is no instant in which a live
        id is unprotected by the lock - unlike a scan of the queue deque, which
        is not synchronised with the worker's `get` and is empty during the
        dequeue-to-token gap.
        """
        self.start()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("job executor is shut down")
            self._refuse_if_store_failed()
            if job.id in self._owned:
                raise RuntimeError(f"job '{job.id}' is already queued")
            if not self._pending_slots.acquire(blocking=False):
                raise QueueFullError(
                    f"job queue is full ({self._max_pending} pending); retry later"
                )
            try:
                self._owned.add(job.id)
                self._queue.put_nowait((job.id, {"source": source, **kwargs}))
            except Exception:
                self._owned.discard(job.id)
                self._pending_slots.release()
                raise
            return job

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. True if the job was live.

        Two cases, because they are genuinely different:

        - **Not yet started** (queued, or the worker has not reached it): the job
          is marked CANCELLED immediately and the worker skips it.
        - **Running**: `cancel_requested` is set and the token is tripped. The
          job stops at its next stage boundary and is then marked CANCELLED.
          Until that boundary is reached it stays RUNNING with
          `cancel_requested: true` - honest about work still in flight rather
          than claiming an instant stop we cannot deliver.
        """
        job = self._store.get(job_id)
        if job is None or job.is_terminal:
            return False

        with self._lock:
            token = self._tokens.get(job_id)

        if token is None:
            self._store.update(
                job_id,
                state=JobState.CANCELLED,
                progress="cancelled",
                cancel_requested=True,
            )
        else:
            token.cancel()
            self._store.update(job_id, cancel_requested=True, progress="cancelling")
        return True

    def running_jobs(self) -> list[str]:
        with self._lock:
            return list(self._tokens)

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._pending_slots.release()
                job_id, kwargs = item

                # Register the token BEFORE inspecting state, so a concurrent
                # cancel() always finds a token to trip rather than racing us
                # into marking the job cancelled while we start it anyway. The id
                # stays in `_owned` from here until the run finishes, so the
                # dequeue-to-token window is closed: a duplicate enqueue is
                # refused for the whole time the item is off the queue.
                token = CancelToken()
                with self._lock:
                    self._tokens[job_id] = token
                try:
                    try:
                        self._run_one(job_id, kwargs, token)
                    except Exception as exc:  # noqa: BLE001 - keep the worker alive
                        # The error write itself goes to the store the runner
                        # just used, so it can fail too. A failure here must not
                        # escape the loop - that would kill this worker and
                        # silently remove a concurrency slot, stranding every
                        # later job. Record the fault instead: it is visible on
                        # `store_failed` and it gates admission, so the executor
                        # stops accepting work it cannot record rather than
                        # swallowing the failure. A transient fault needs no
                        # retry storm - a single bounded write on the next refused
                        # admission clears it.
                        error_text = f"{type(exc).__name__}: {exc}"
                        with self._lock:
                            # The execution identity captured by `_run_one` from
                            # the successful start (`begin_attempt`). It was
                            # recorded *before* the fault struck, so the error
                            # handler never re-derives it from a store read the
                            # same outage may have broken - the identity-loss
                            # failure. `None` means the start never landed and no
                            # pre-start read was possible: genuinely unknown.
                            owned_attempt = self._owned_attempts.pop(job_id, None)
                        pending = TerminalWrite(
                            job_id=job_id,
                            attempt=owned_attempt,
                            fields={
                                "state": JobState.ERROR,
                                "error": error_text,
                                "progress": "failed",
                            },
                        )
                        try:
                            self._store.update(job_id, **pending.fields)
                        except Exception as store_exc:  # noqa: BLE001
                            # Keep the failed terminal row so a later recovery
                            # write can resolve it truthfully rather than leaving
                            # it RUNNING forever, pinned to the attempt this run
                            # owned so a newer owner's row is never clobbered. An
                            # unknown attempt stays None so recovery refuses to
                            # guess it.
                            self._record_store_failure(store_exc, pending=pending)
                finally:
                    with self._lock:
                        self._tokens.pop(job_id, None)
                        self._owned.discard(job_id)
                        self._owned_attempts.pop(job_id, None)
            finally:
                self._queue.task_done()

    def _run_one(self, job_id: str, kwargs: dict[str, Any], token: CancelToken) -> None:
        from textflowkit.core.runner import run_job

        job = self._store.get(job_id)
        if job is None or job.is_terminal:
            return  # cancelled or reaped before we got to it

        # Seed the owned identity with the attempt this read saw: it is the row's
        # attempt *before* the run, which is what repair must pin if the start
        # never lands (the run did not advance past it). A start that does land
        # overrides this with the new attempt. Kept under `self._lock` and keyed
        # by id (like `_tokens`), so the worker's error handler can read it back
        # without `_run_one` having to return anything.
        with self._lock:
            self._owned_attempts[job_id] = job.attempt

        def _on_started(attempt: int | None) -> None:
            # Called by the runner the instant the row is marked RUNNING, with the
            # exact attempt it owns - or ``None`` when neither the start nor the
            # pre-start read was possible. Recorded here, before the pipeline can
            # fail, so the error handler pins repair to the real identity rather
            # than re-reading (and possibly misreading) it after a fault.
            if attempt is not None:
                with self._lock:
                    self._owned_attempts[job_id] = attempt

        run_job(
            job, self._store, check_cancel=token.checkpoint, on_started=_on_started, **kwargs
        )


# Process-wide default executor, shared by the adapters.
_default_executor: JobExecutor | None = None
_executor_lock = threading.Lock()


def get_default_executor() -> JobExecutor:
    global _default_executor
    with _executor_lock:
        if _default_executor is None:
            _default_executor = JobExecutor(get_default_store())
        return _default_executor


def reset_default_executor() -> None:
    """Drop the cached executor (tests, embedding)."""
    global _default_executor
    with _executor_lock:
        if _default_executor is not None:
            _default_executor.shutdown(wait=False)
        _default_executor = None
