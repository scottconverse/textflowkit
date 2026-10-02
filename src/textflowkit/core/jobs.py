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

import copy
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
# A resume may reopen a job only from one of these states. DONE is deliberately
# excluded even though it is terminal: a finished job is *reused* (its stored
# transcript is the deliverable), never reopened and re-run. Claiming from DONE
# is what let a fast winner reach DONE and a racer then reopen it and transcribe
# the same job a second time - so the claim's allowed set, not TERMINAL_STATES,
# is the ownership guard.
RESUMABLE_CLAIM_STATES = frozenset({JobState.ERROR, JobState.CANCELLED})
MAX_LIST_LIMIT = 1000


def validate_list_limit(limit: int) -> int:
    if limit < 0 or limit > MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 0 and {MAX_LIST_LIMIT}")
    return limit

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
    request: dict[str, Any] | None = None
    # Which execution attempt this row currently describes. It starts at 0 and is
    # bumped when the job enters RUNNING *and* whenever a claim acquires the row
    # (an explicit resume's reopen). So it names not a literal count of runs but
    # the identity a decision is pinned against: two acquisitions of the same row
    # that never reached RUNNING (a refused admission, a queued cancellation and
    # reclaim) still get distinct attempts. A resume that decided against one
    # attempt must say which one, otherwise its intent is honoured against a later
    # attempt that happens to share the state - the stale-observation ABA. State
    # alone cannot tell the two apart; this counter can.
    attempt: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(
        self, *, include_transcript: bool = False, include_checkpoint: bool = False
    ) -> dict[str, Any]:
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
        }
        # `attempt` is deliberately absent: it is an internal ownership detail
        # used to refuse a stale resume, not part of the job payload the adapters
        # publish. Keeping it out preserves the existing wire fingerprint.
        if include_checkpoint and self.checkpoint is not None:
            data["checkpoint"] = self.checkpoint
        if include_transcript and self.transcript is not None:
            data["transcript"] = self.transcript
        return data


@dataclass(frozen=True)
class ObservedJob:
    """An immutable point-in-time snapshot of a job's identity and state.

    A store may hand callers a *mutable alias* of its live row (`MemoryJobStore`
    returns the `Job` object itself), so reading fields off a `get()` result
    after other store operations is not a coherent observation: a concurrent
    writer can move the attempt or state out from under the reader between the
    two reads. `JobStore.observe` returns this value instead - a frozen copy
    taken under the store's own lock, so every field is from the same instant.

    ``attempt`` is the identity a decision is made against; a claim keyed to it
    is refused once the row has moved on. This is the value a caller pins with
    ``claim(..., observed_attempt=...)``; it exists at *acquisition* (the claim
    itself bumps nothing here), not only when inference starts.

    ``transcript`` and ``outputs`` are carried because a snapshot must stand in
    for the whole row, not only the ownership fields: a DONE job keeps its
    transcript in the job field and its resume metadata in the checkpoint, so
    reading the checkpoint out of a snapshot without the transcript half makes a
    completed job look like it has no reusable checkpoint at all. They are read
    from the same instant as ``attempt``, which is the point - the checkpoint and
    the transcript it is paired with must come from one observation.
    """

    id: str
    source: str
    state: JobState
    attempt: int
    error: str | None
    cancel_requested: bool
    request: dict[str, Any] | None
    checkpoint: dict[str, Any] | None
    transcript: dict[str, Any] | None = None
    outputs: tuple[str, ...] = ()

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _snapshot(job: Job) -> ObservedJob:
    """Copy a job's fields into an immutable observation."""
    return ObservedJob(
        id=job.id,
        source=job.source,
        state=job.state,
        attempt=job.attempt,
        error=job.error,
        cancel_requested=job.cancel_requested,
        request=dict(job.request) if job.request is not None else None,
        checkpoint=dict(job.checkpoint) if job.checkpoint is not None else None,
        transcript=dict(job.transcript) if job.transcript is not None else None,
        outputs=tuple(job.outputs),
    )


class JobStore(ABC):

    """Storage for jobs.

    Implementations must be safe for concurrent use from multiple threads, and
    must return jobs ordered newest-first by insertion sequence.
    """

    @abstractmethod
    def create(self, source: str, *, request: dict[str, Any] | None = None) -> Job:
        """Create a pending job and return it."""

    @abstractmethod
    def get(self, job_id: str) -> Job | None:
        """Fetch one job, or None."""

    def observe(self, job_id: str) -> ObservedJob | None:
        """A coherent, immutable snapshot of one job's identity and state.

        The default reads through `get` and copies the fields, which is coherent
        for a store that returns detached rows. A store that hands out a mutable
        alias of its live row MUST override this to copy under its own lock:
        otherwise a concurrent write can land between the copy's field reads and
        the snapshot mixes two instants. `MemoryJobStore` overrides for exactly
        that reason.

        Ownership decisions are made against this value: it names the attempt the
        caller observed, which a later `claim(..., observed_attempt=...)` requires
        the row to still hold.
        """
        job = self.get(job_id)
        return _snapshot(job) if job is not None else None

    @abstractmethod
    def update(self, job_id: str, **fields: Any) -> Job | None:
        """Patch fields on a job and bump updated_at. None if absent."""

    @abstractmethod
    def claim(
        self,
        job_id: str,
        *,
        allowed_states: frozenset[JobState] | set[JobState],
        observed_attempt: int | None = None,
        advance_attempt: bool = False,
        **fields: Any,
    ) -> Job | None:
        """Atomically transition a job only from `allowed_states`.

        The patch and the state test are one indivisible operation, so two
        callers racing the same job id cannot both observe an allowed state and
        both claim it: exactly one applies the transition and the other gets
        None. `update` deliberately stays unconditional - it is how a worker
        writes progress and terminal results on work it already owns - but any
        transition that *acquires* ownership of a job must go through here.

        ``observed_attempt`` closes the stale-observation window that state alone
        leaves open. A caller that read a terminal row to decide on it passes the
        ``attempt`` it saw; the claim then also requires the row's current
        ``attempt`` to be that same number. If the row left and returned to the
        same state in between - an earlier resumer failed it back to ERROR - the
        attempt has moved on, the observed value no longer matches, and the claim
        is refused. ``None`` means "I did not pin an attempt" (the caller is not
        making a decision against a previously observed failure), which keeps the
        state-only behaviour for callers that do not need it.

        ``advance_attempt`` gives a *claim* its own identity. When true the
        transition bumps ``attempt`` as part of the same atomic write, so
        ownership is named at acquisition rather than only when a run reaches
        RUNNING. That is what lets a decision and a later cleanup be keyed to the
        claim itself: a claim refused admission before RUNNING (its identity
        still moves) and a queued claim cancelled and re-claimed (the newest
        claim carries a distinct identity) are both distinguishable from the
        observation that preceded them. Callers that want their returned row to
        reflect the new identity should set it; the returned `Job` carries it.

        Returns the updated job, or None when the job is absent, its current state
        is not in `allowed_states`, or its attempt is not the observed one.
        Callers turn None into their own conflict message after re-reading, so it
        stays actionable.
        """

    @abstractmethod
    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
        """Recent jobs, newest first, optionally filtered by state."""

    @abstractmethod
    def clear(self) -> None:
        """Remove every job."""

    def update_owned(
        self,
        job_id: str,
        *,
        observed_attempt: int,
        allowed_states: frozenset[JobState] | set[JobState] = frozenset(
            {JobState.PENDING, JobState.RUNNING}
        ),
        refuse_if_cancelled: bool = True,
        **fields: Any,
    ) -> Job | None:
        """Patch a *run-owned* row atomically, pinned to the owning generation.

        A run writes progress and checkpoints while it is running; those writes
        are not ownership transitions, so ``update`` (unconditional by design)
        was used for them. That left a read-then-write: the runner read the row,
        tested it, then wrote, and a newer generation - a resume claim, which
        advances ``attempt``, or an accepted cancellation landing between the two
        - was silently overwritten. This is the guarded form.

        Refuses (``None``) when the row is absent, no longer carries
        ``observed_attempt`` (a newer owner has it), is in a state outside
        ``allowed_states`` (terminal rows are never rewritten by a run), or -
        when ``refuse_if_cancelled`` is true - already carries an accepted
        cancellation. The last guard is what keeps an accepted cancellation's
        ``cancelling`` label from being replaced by the next stage name: the
        flag and the progress write are tested and applied in one operation, so
        no acceptance can slip between them.

        ``refuse_if_cancelled=False`` is for the *checkpoint* write: a run that
        has been asked to stop keeps writing the work it has completed, because
        that checkpoint is exactly the resume material a later attempt reads. A
        cancellation must not throw away work in flight, so the checkpoint is
        allowed to land on a cancelling row - but still only on the row this run
        owns, and never on a terminal or newer one.

        Returns the updated job, or ``None`` when the guard refuses. The default
        reads and then writes through ``update`` and so is *not* atomic; both
        shipped stores override it with the guarded transition in one write.
        """
        current = self.get(job_id)
        if current is None:
            return None
        allowed = frozenset(allowed_states)
        if current.state not in allowed:
            return None
        if current.attempt != observed_attempt:
            return None
        if refuse_if_cancelled and current.cancel_requested:
            return None
        return self.update(job_id, **fields)

    def begin_attempt(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Atomically start a run: mark RUNNING and bump the attempt in one write.

        The start is a *guarded* transition - it refuses (None) unless the row is
        still PENDING, still carries no accepted cancellation, and still holds the
        generation the caller acquired. That is what makes a queued cancellation
        final: ``cancel`` on a still-queued row lands CANCELLED (terminal) through
        ``accept_cancel``, and the worker that later dequeues it finds a start
        that refuses rather than a job resurrected into RUNNING after it was
        already cancelled. The runner treats a refused start as "do not run" and
        returns without entering the engine.

        ``observed_attempt`` pins the generation the submit/enqueue actually
        acquired. A stale queue entry for an id that was cancelled and then
        resumed names the *old* generation, so its start is refused and it cannot
        run beside the newer owner. ``None`` means "the caller did not pin one",
        which keeps the state-and-flag guard alone.

        The returned row carries the run's owned attempt (its execution identity);
        a caller reports it *before* any pipeline fault can strike, so a later
        error never has to re-derive it from a possibly-broken store.

        Both shipped stores override this atomically; this default delegates to
        the store's own ``claim``, so a store whose ``claim`` is atomic inherits a
        correct start. A store that implements neither atomically inherits the
        window this method exists to close.
        """
        return self.claim(
            job_id,
            allowed_states=frozenset({JobState.PENDING}),
            observed_attempt=observed_attempt,
            advance_attempt=True,
            state=JobState.RUNNING,
            cancel_requested=False,
        )

    def accept_cancel(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Atomically accept a cancellation against one named generation.

        Refuses (None) when the row is absent, already terminal (including
        already CANCELLED - a second cancel changes nothing and is not a new
        acceptance), or no longer carries ``observed_attempt``. Otherwise:

        - **PENDING** (queued, or a claimed row no worker has started): land
          CANCELLED immediately. The row is honestly finished - no work is in
          flight to stop - and the terminal state is what stops a later start
          from resurrecting it.
        - **RUNNING**: record ``cancel_requested`` and progress ``cancelling``
          but stay NON-terminal. Cancellation is cooperative and a single long
          model call cannot be interrupted mid-call, so publishing CANCELLED here
          would claim a stop that has not happened. The token drives the actual
          stop; the runner's finalize lands the terminal CANCELLED when the call
          really returns.

        The default is a read-then-write and so is *not* atomic; both shipped
        stores override it with the check-and-set in one write.
        """
        job = self.get(job_id)
        if job is None or job.is_terminal:
            return None
        if observed_attempt is not None and job.attempt != observed_attempt:
            return None
        if job.state is JobState.PENDING:
            return self.update(
                job_id,
                state=JobState.CANCELLED,
                progress="cancelled",
                cancel_requested=True,
            )
        return self.update(job_id, cancel_requested=True, progress="cancelling")

    def finalize_done(
        self, job_id: str, *, observed_attempt: int, **fields: Any
    ) -> Job | None:
        """Atomically choose the terminal row for a successful run.

        Lands CANCELLED when the *same owned generation* carries an accepted
        cancellation (``cancel_requested`` true and still non-terminal), and DONE
        with ``fields`` otherwise. The choice is made in the same atomic write as
        the row update, so there is no window between "read the cancellation
        flag" and "write DONE" - the exact race that let a finished-looking DONE
        row carry ``cancel_requested: true``.

        Refuses (None) when the row is absent, has already reached a terminal
        state (completion landed first, so a later cancel is refused at
        ``cancel`` - it never reaches here), or no longer carries
        ``observed_attempt`` (a newer retry owns the row). It must not return None
        for a row it could still resolve: an accepted cancellation on the owned
        generation is written CANCELLED, never left RUNNING.

        On the CANCELLED branch the run keeps its *resume work*: the checkpoint is
        left exactly as the pipeline wrote it (with its full transcript) and any
        outputs already rendered are recorded, because those artifacts are really
        on disk and deleting or hiding them would misreport the run. Only the DONE
        branch moves the transcript onto the job field and reduces the checkpoint
        to metadata - the single-copy rule.

        The default is a read-then-write and so is *not* atomic; both shipped
        stores override it with the guarded transition in one write.
        """
        job = self.get(job_id)
        if job is None or job.is_terminal or job.attempt != observed_attempt:
            return None
        if job.cancel_requested:
            patch: dict[str, Any] = {"state": JobState.CANCELLED, "progress": "cancelled"}
            if "outputs" in fields:
                patch["outputs"] = list(fields["outputs"] or [])
            return self.update(job_id, **patch)
        return self.update(job_id, state=JobState.DONE, **fields)

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

    def create(self, source: str, *, request: dict[str, Any] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source=source, request=request)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def observe(self, job_id: str) -> ObservedJob | None:
        """Snapshot under the lock: `get` here returns the live row itself.

        Copying *inside* the lock is what makes the observation coherent - a
        caller that copied fields off a `get()` result outside the lock could
        read `attempt` before a concurrent claim and `state` after it, mixing two
        instants into a decision.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return _snapshot(job) if job is not None else None

    def update(self, job_id: str, **fields: Any) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            return job

    def claim(
        self,
        job_id: str,
        *,
        allowed_states: frozenset[JobState] | set[JobState],
        observed_attempt: int | None = None,
        advance_attempt: bool = False,
        **fields: Any,
    ) -> Job | None:
        allowed = frozenset(allowed_states)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in allowed:
                return None
            if observed_attempt is not None and job.attempt != observed_attempt:
                return None
            if advance_attempt:
                job.attempt += 1
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            return job

    def update_owned(
        self,
        job_id: str,
        *,
        observed_attempt: int,
        allowed_states: frozenset[JobState] | set[JobState] = frozenset(
            {JobState.PENDING, JobState.RUNNING}
        ),
        refuse_if_cancelled: bool = True,
        **fields: Any,
    ) -> Job | None:
        """One locked write: the ownership test and the patch are indivisible.

        The test cannot be split from the write, so an acceptance or a newer
        claim that lands first makes this refuse rather than be overwritten.
        """
        allowed = frozenset(allowed_states)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in allowed:
                return None
            if job.attempt != observed_attempt:
                return None
            if refuse_if_cancelled and job.cancel_requested:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            return job

    def begin_attempt(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Start a run atomically, only from a still-PENDING, uncancelled row.

        The guard is the whole point: a queued cancellation moves the row to
        CANCELLED first, so the start refuses and the worker does not resurrect
        it. ``observed_attempt`` additionally pins the acquired generation so a
        stale queue entry cannot start a newer owner's row.

        Returns a *detached* snapshot of the row the atomic start produced (its
        ``attempt`` is the run's owned identity), or None when the start is
        refused. The copy is taken under the lock and then returned, so it is
        one instant's worth of the row: a later write by another owner - a resume
        claim advancing the attempt, a cancellation, a rewritten
        checkpoint/request - cannot mutate the identity this run pins its
        terminal decision to. (Returning the live row would alias the run's
        identity to whatever the row becomes next.)
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal or job.state is not JobState.PENDING:
                return None
            if observed_attempt is not None and job.attempt != observed_attempt:
                return None
            if job.cancel_requested:
                return None
            job.state = JobState.RUNNING
            job.attempt += 1
            job.updated_at = time.time()
            return copy.deepcopy(job)

    def accept_cancel(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Cancel atomically: CANCELLED when still PENDING, flag-only when RUNNING."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return None
            if observed_attempt is not None and job.attempt != observed_attempt:
                return None
            job.cancel_requested = True
            if job.state is JobState.PENDING:
                job.state = JobState.CANCELLED
                job.progress = "cancelled"
            else:
                job.progress = "cancelling"
            job.updated_at = time.time()
            return job

    def finalize_done(
        self, job_id: str, *, observed_attempt: int, **fields: Any
    ) -> Job | None:
        """Choose CANCELLED/DONE for the owned generation in one locked write."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal or job.attempt != observed_attempt:
                return None
            if job.cancel_requested:
                job.state = JobState.CANCELLED
                job.progress = "cancelled"
                if "outputs" in fields:
                    job.outputs = list(fields["outputs"] or [])
            else:
                for key, value in fields.items():
                    setattr(job, key, value)
                job.state = JobState.DONE
            job.updated_at = time.time()
            return job

    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
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

