"""Job model and store.

Long-running transcription is modelled as a job so the same interface works
everywhere: stdio MCP polls in-process, an HTTP adapter polls over the wire, and
a website can queue work. The store is intentionally dependency-free and
thread-safe; swap `JobStore` for a durable backend without touching callers.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobState.DONE, JobState.ERROR, JobState.CANCELLED}


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
        }
        if include_transcript and self.transcript is not None:
            data["transcript"] = self.transcript
        return data


class JobStore:
    """In-memory, thread-safe job store."""

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
        """Recent jobs, newest first.

        Ordering is by insertion sequence, not by `created_at`. Wall-clock
        ordering is not portable: on Windows with Python < 3.13 `time.time()`
        has coarse resolution, so several jobs created in a tight loop share a
        timestamp and their relative order becomes arbitrary. Insertion order is
        deterministic on every platform.
        """
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


# Process-wide default store, shared by the MCP and HTTP adapters.
_default_store: JobStore | None = None
_store_lock = threading.Lock()


def get_default_store() -> JobStore:
    global _default_store
    with _store_lock:
        if _default_store is None:
            _default_store = JobStore()
        return _default_store

