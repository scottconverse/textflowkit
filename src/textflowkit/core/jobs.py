"""Job model and the store interface.

Long-running transcription is modelled as a job so the same interface works
everywhere: stdio MCP polls in-process, an HTTP adapter polls over the wire, and
a website can queue work.

`JobStore` is the abstraction. Two implementations ship:

- `MemoryJobStore` - fast, process-local, used for tests and ephemeral runs.
- `SqliteJobStore` - durable, survives restart. Selected by setting
  ``TEXTFLOWKIT_DB``.

Ordering is always by insertion sequence, never by wall-clock time: on Windows
with Python < 3.13 ``time.time()`` is coarse enough that jobs created in a tight
loop share a timestamp, and tie-breaking by insertion order silently inverts
"newest first".
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobState.DONE, JobState.ERROR, JobState.CANCELLED})

# A job in one of these states expects a worker to be running it. Across a
# restart there is no worker, so any such job is orphaned and must be reaped -
# otherwise it sits in "running" forever.
INCOMPLETE_STATES = frozenset({JobState.PENDING, JobState.RUNNING})


@dataclass
class Job:
    """A single transcription job."""

    id: str
    source: str
    state: JobState = JobState.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: str = ""
    error: str | None = None
    transcript: dict[str, Any] | None = None
    outputs: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    checkpoint: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self, *, include_transcript: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "source": self.source,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "error": self.error,
            "outputs": list(self.outputs),
            "cancel_requested": self.cancel_requested,
            "checkpoint": self.checkpoint,
        }
        if include_transcript and self.transcript is not None:
            data["transcript"] = self.transcript
        return data


class JobStore(ABC):
    """Storage for jobs.

    Implementations must be safe for concurrent use from multiple threads, and
    must return jobs ordered newest-first by insertion sequence.
    """

    @abstractmethod
    def create(self, source: str) -> Job:
        """Create a pending job and return it."""

    @abstractmethod
    def get(self, job_id: str) -> Job | None:
        """Fetch one job, or None."""

    @abstractmethod
    def update(self, job_id: str, **fields: Any) -> Job | None:
        """Patch fields on a job and bump updated_at. None if absent."""

    @abstractmethod
    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
        """Recent jobs, newest first, optionally filtered by state."""

    @abstractmethod
    def clear(self) -> None:
        """Remove every job."""

    def reap_incomplete(self, *, reason: str) -> int:
        """Fail any job left mid-flight, returning how many were reaped.

        Called at startup. A job in PENDING/RUNNING has no worker after a
        restart, so leaving it alone would report a job that will never finish.
        """
        reaped = 0
        for job in self.list(limit=10_000):
            if job.state in INCOMPLETE_STATES:
                self.update(
                    job.id,
                    state=JobState.ERROR,
                    error=reason,
                    progress="interrupted",
                )
                reaped += 1
        return reaped

    def close(self) -> None:
        """Release resources. No-op by default."""


class MemoryJobStore(JobStore):
    """In-memory, thread-safe job store. Fast; not durable."""

    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._max_jobs = max_jobs

    def create(self, source: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source=source)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields: Any) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            return job

    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
        with self._lock:
            jobs = [self._jobs[i] for i in reversed(self._order) if i in self._jobs]
        if state is not None:
            jobs = [j for j in jobs if j.state == state]
        return jobs[:limit]

    def _evict_locked(self) -> None:
        """Drop oldest terminal jobs once over capacity."""
        while len(self._order) > self._max_jobs:
            for idx, job_id in enumerate(self._order):
                job = self._jobs.get(job_id)
                if job is None:
                    self._order.pop(idx)
                    break
                if job.is_terminal:
                    self._order.pop(idx)
                    self._jobs.pop(job_id, None)
                    break
            else:
                return  # everything still running; keep them all

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._order.clear()


ENV_DB = "TEXTFLOWKIT_DB"

_default_store: JobStore | None = None
_store_lock = threading.Lock()


def _make_store() -> JobStore:
    """Choose a store from the environment. Durable when TEXTFLOWKIT_DB is set."""
    db_path = os.environ.get(ENV_DB)
    if db_path:
        from textflowkit.core.sqlite_store import SqliteJobStore

        return SqliteJobStore(db_path)
    return MemoryJobStore()


def get_default_store() -> JobStore:
    """Process-wide store shared by the MCP and HTTP adapters."""
    global _default_store
    with _store_lock:
        if _default_store is None:
            _default_store = _make_store()
        return _default_store


def set_default_store(store: JobStore | None) -> None:
    """Override the process-wide store (tests, embedding)."""
    global _default_store
    with _store_lock:
        _default_store = store


def reset_default_store() -> None:
    """Drop the cached store so the next call re-reads the environment."""
    set_default_store(None)

