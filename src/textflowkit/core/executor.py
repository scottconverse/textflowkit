"""Bounded job executor.

Before this, `submit()` spawned one raw `threading.Thread` per job and forgot it.
That had two consequences the review called out:

- **No concurrency bound.** N submissions meant N simultaneous Whisper runs
  competing for the same GPU. On a device Whisper already saturates, parallel
  jobs do not go faster - they thrash memory.
- **No handle on running work.** Nothing held a reference to a running job, so
  "cancel" had nothing to act on.

This module replaces the fire-and-forget thread with a fixed worker pool that
owns job lifecycle. Cancellation is cooperative: a job checks a token at stage
boundaries and stops cleanly. A job inside a single long model call cannot be
interrupted mid-call - that call finishes, then the job stops. Stated plainly
here because it is a real limit, not an implementation detail.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any

from textflowkit.core.cancel import CancelledError
from textflowkit.core.jobs import Job, JobState, JobStore, get_default_store

ENV_CONCURRENCY = "TEXTFLOWKIT_MAX_CONCURRENCY"
DEFAULT_CONCURRENCY = 1
ENV_MAX_PENDING = "TEXTFLOWKIT_MAX_PENDING_JOBS"
DEFAULT_MAX_PENDING = 100


class QueueFullError(RuntimeError):
    """The executor cannot accept another pending job right now."""


# The signal itself lives in a leaf module so the source layer can re-raise it
# around broad exception handling. This alias keeps the name used everywhere
# else in the codebase and in tests.
JobCancelled = CancelledError


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
        self._lock = threading.RLock()
        self._started = False
        self._shutdown = False

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
            if not self._pending_slots.acquire(blocking=False):
                raise QueueFullError(
                    f"job queue is full ({self._max_pending} pending); retry later"
                )
            try:
                job = self._store.create(source, request=request)
                self._queue.put_nowait((job.id, {"source": source, **kwargs}))
            except Exception:
                self._pending_slots.release()
                raise
            return job

    def enqueue(self, job: Job, *, source: str, **kwargs: Any) -> Job:
        """Queue an existing prepared job (the durable resume path)."""
        self.start()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("job executor is shut down")
            if not self._pending_slots.acquire(blocking=False):
                raise QueueFullError(
                    f"job queue is full ({self._max_pending} pending); retry later"
                )
            try:
                self._queue.put_nowait((job.id, {"source": source, **kwargs}))
            except Exception:
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
                # into marking the job cancelled while we start it anyway.
                token = CancelToken()
                with self._lock:
                    self._tokens[job_id] = token
                try:
                    try:
                        self._run_one(job_id, kwargs, token)
                    except Exception as exc:  # noqa: BLE001 - keep the worker alive
                        self._store.update(
                            job_id,
                            state=JobState.ERROR,
                            error=f"{type(exc).__name__}: {exc}",
                            progress="failed",
                        )
                finally:
                    with self._lock:
                        self._tokens.pop(job_id, None)
            finally:
                self._queue.task_done()

    def _run_one(self, job_id: str, kwargs: dict[str, Any], token: CancelToken) -> None:
        from textflowkit.core.runner import run_job

        job = self._store.get(job_id)
        if job is None or job.is_terminal:
            return  # cancelled or reaped before we got to it
        run_job(job, self._store, check_cancel=token.checkpoint, **kwargs)


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
