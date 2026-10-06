"""ENG-002 review fix: stale failure observation and the queued-to-running gap.

The first-generation ownership regression (`test_atomic_resume.py`) pins a
*state-only* claim: exactly one of two racers may transition a terminal row back
to PENDING. Two narrower holes survive that:

1. **Stale failure observation (ABA).** A caller selects a prior terminal job,
   reads its state, and *then* claims it. Between the read and the claim the row
   can leave the observed state and come back to it - the first resumer reopens
   the row, runs it, and fails it again, landing on the same ERROR the second
   caller had observed. The second caller's claim then succeeds against a state
   it never actually observed, and a stale intent (a retry of the *previous*
   failure) is honoured against the *new* failure. State is the same value; the
   attempt is different. Correctness needs the identity of the observed attempt,
   not just its state.

2. **Queued-to-running ownership gap.** `enqueue` refuses a duplicate only while
   the id is in the token map or still visible in the queue deque. A worker takes
   the item off the deque *before* it registers a token, so a second enqueue in
   that gap passes the duplicate check and puts two queue entries - and later two
   executions - on one job id.

Both regressions drive the real store, the real submission contract, and the real
executor. Only the expensive `transcribe` stage is stubbed, and every wait is
bounded. They fail against the code at d55e067 (the claim is state-only and
`enqueue` scans the deque) and pass once ownership is keyed to an attempt.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import pytest

from textflowkit.core import submission
from textflowkit.core.checkpoint import CheckpointRecord, local_source_identity
from textflowkit.core.executor import JobExecutor, QueueFullError
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import TranscribeResult
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest, submit_request

# --- fixtures (mirroring test_atomic_resume.py) -----------------------------


def _wav(path: Path, seconds: int = 1) -> None:
    """A real, tiny playable WAV: the submission contract stats local sources."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(16000 * 2 * seconds))


def _transcript(source: str, text: str = "reused") -> Transcript:
    return Transcript(source=source, language="en", segments=[Segment(0.0, 1.0, text)])


def _result(source: str, text: str = "reused") -> TranscribeResult:
    return TranscribeResult(transcript=_transcript(source, text), outputs=[])


def _checkpoint_for(request: SubmissionRequest, media: Path) -> CheckpointRecord:
    return CheckpointRecord(
        source=request.source,
        model=request.model,
        language=request.language,
        engine=request.engine,
        device=request.device,
        options=request.options(),
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=_transcript(request.source).to_dict(),
        local_identity=local_source_identity(str(media)),
    )


def _seed_terminal_job(store, request: SubmissionRequest, media: Path, *,
                       state: JobState = JobState.ERROR, with_checkpoint: bool = False):
    """Create an already terminal job for `request`, optionally with a checkpoint."""
    job = store.create(request.source, request=request.to_dict())
    fields = {
        "state": state,
        "error": "interrupted" if state is JobState.ERROR else None,
        "cancel_requested": state is JobState.CANCELLED,
    }
    if with_checkpoint:
        fields["checkpoint"] = _checkpoint_for(request, media).to_dict()
    store.update(job.id, **fields)
    return store.get(job.id)


def _settle(executions: list, expected: int, *, timeout: float = 5.0) -> None:
    """Wait until at least `expected` executions, then confirm no extra appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(executions) < expected:
        time.sleep(0.01)
    time.sleep(0.2)


# --- 1. stale failure observation (ABA) ------------------------------------


class _AttemptScript:
    """A transcribe double that fails the first attempt and succeeds afterwards.

    The script is the whole point: the second caller's decision was formed
    against the *first* failure, so if it is honoured it re-enters the engine for
    an attempt that no live decision actually asked for.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.executions: list[str] = []
        self.failures = 0

    def __call__(self, source, *, resume_checkpoint=None, on_checkpoint=None, **kwargs):
        self.executions.append(source)
        if self.failures == 0:
            self.failures += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            raise RuntimeError("controlled first-attempt failure")
        return _result(source, text="second-attempt")


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_resume_that_observed_a_stale_failure_is_refused(monkeypatch, tmp_path, store_kind):
    """A caller that observed the *old* failure must not claim the *new* one.

    Deterministic interleaving, no timing race and no loop:

    - caller A resumes the ERROR job and blocks inside the engine;
    - caller B observes the row (state ERROR, the same state A is about to
      replace) and parks;
    - A's engine raises, so the row returns to ERROR - the exact state B saw;
    - B is released and claims. Because the state matches, the state-only claim
      succeeds and B runs the job a second time.

    The fix must refuse B (its observed attempt is gone) while still allowing a
    genuinely fresh explicit retry to observe the new failure and run.
    """
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), engine="whisper", model="tiny", formats=["json"])
    store = MemoryJobStore() if store_kind == "memory" else SqliteJobStore(tmp_path / "jobs.db")
    try:
        job = _seed_terminal_job(store, request, media)

        script = _AttemptScript()
        from textflowkit.core import runner
        monkeypatch.setattr(runner, "transcribe", script)

        # B observes the terminal row through the public submission read and then
        # parks until A has landed the row back on ERROR.
        observed = threading.Event()
        release_b = threading.Event()
        b_is_observing = threading.Event()
        original_matching = submission._matching_checkpoint

        def matching(store_arg, request_arg, job_id):
            result = original_matching(store_arg, request_arg, job_id)
            if (
                job_id == job.id
                and threading.current_thread().name == "caller-b"
                and not b_is_observing.is_set()
            ):
                b_is_observing.set()
                observed.set()
                assert release_b.wait(timeout=5)
            return result

        monkeypatch.setattr(submission, "_matching_checkpoint", matching)

        results: list = []

        def resume_b():
            try:
                value = submit_request(
                    store, SubmissionRequest.from_dict(request.to_dict()),
                    background=False, resume_job_id=job.id,
                )
            except Exception as exc:  # noqa: BLE001 - the refusal is the subject
                value = exc
            results.append(value)

        # B starts immediately and parks inside the read while the row still holds
        # the original ERROR, so the value it observes is the *first* failure.
        thread_b = threading.Thread(target=resume_b, name="caller-b")
        thread_b.start()
        assert observed.wait(timeout=5), "B never observed the row"

        # A now resumes and blocks in the engine; on release it fails, landing the
        # row on the same state B observed but a different attempt.
        thread_a = threading.Thread(
            target=lambda: submit_request(
                store, SubmissionRequest.from_dict(request.to_dict()),
                background=False, resume_job_id=job.id,
            ),
            name="caller-a",
        )
        thread_a.start()
        assert script.entered.wait(timeout=5), "A never entered the engine"
        assert store.get(job.id).state is JobState.RUNNING

        script.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and store.get(job.id).state is not JobState.ERROR:
            time.sleep(0.01)
        assert store.get(job.id).state is JobState.ERROR, "A did not land the failure"
        thread_a.join(10)
        assert not thread_a.is_alive(), "A did not finish"

        # Only now let B finish its read and act on the stale observation.
        release_b.set()
        thread_b.join(10)
        assert not thread_b.is_alive(), "B did not finish"

        _settle(script.executions, 1)

        accepted = [r for r in results if not isinstance(r, Exception)]
        refused = [r for r in results if isinstance(r, Exception)]

        assert len(script.executions) == 1, (
            f"the stale caller re-ran the job: executions={script.executions!r}"
        )
        assert not accepted, f"the stale caller was accepted: {results!r}"
        assert refused and isinstance(refused[0], ValueError), repr(results)

        # ...and the row is left as a genuinely resumable ERROR, so a *fresh*
        # retry that observes the new failure is still allowed to run it.
        assert store.get(job.id).state is JobState.ERROR

        script.executions.clear()
        retry = submit_request(
            store, SubmissionRequest.from_dict(request.to_dict()),
            background=False, resume_job_id=job.id,
        )
        assert retry.state is JobState.DONE, "a fresh retry must still be allowed"
        assert len(script.executions) == 1, "the fresh retry did not run exactly once"
    finally:
        store.close()


# --- 2. queued-to-running ownership gap ------------------------------------


def test_a_job_off_the_deque_is_still_owned_against_a_second_enqueue(monkeypatch, tmp_path):
    """A job in the dequeue-to-token gap must not be enqueueable a second time.

    The worker removes the queue item *before* it registers the running token, and
    the guard a duplicate enqueue hits (whether a scan of the queue deque or a
    token-map lookup) sees an id in neither between the dequeue and the token
    registration - so a second `enqueue` in that window is admitted and the job
    runs twice.

    The gap is entered deterministically by hooking the worker's pending-slot
    release, which is the first thing after `self._queue.get()` returned and
    before the token is registered, and holding a second `enqueue` there. No
    queue internals are read or written.
    """
    store = MemoryJobStore()

    executions: list[str] = []
    release = threading.Event()
    in_gap = threading.Event()
    second_done = threading.Event()

    def gate_transcribe(source, **kwargs):
        executions.append(source)
        return _result(source)

    from textflowkit.core import runner
    monkeypatch.setattr(runner, "transcribe", gate_transcribe)

    executor = JobExecutor(store, max_concurrency=1)
    # Start the pool before seeding, exactly as the resume path does: `start()`
    # reaps PENDING/RUNNING rows as interrupted, so a prepared row must be
    # created after it (the submission contract calls `start()` before claiming).
    executor.start()
    # A prepared job for the durable-resume path (already claimed, so PENDING).
    prepared = store.create("https://example.com/v")
    store.update(prepared.id, state=JobState.PENDING)

    # Widen exactly the dequeue-to-token window: the worker releases its pending
    # slot (the first thing after `self._queue.get()` returned) before it
    # registers the running token. Hooking that release holds the item *out of
    # the deque and out of the token map* - the gap - so a duplicate enqueue can
    # be attempted against it. No queue internals are read or written.
    original_release = executor._pending_slots.release
    original_run_one = executor._run_one
    gated = {"done": False}

    def release_slot():
        original_release()
        if (
            threading.current_thread().name.startswith("textflowkit-worker")
            and not gated["done"]
        ):
            gated["done"] = True
            in_gap.set()
            release.wait(timeout=5)

    def run_one(job_id, kwargs, token):
        # Reached only after the token is registered - past the gap.
        release.set()
        return original_run_one(job_id, kwargs, token)

    monkeypatch.setattr(executor._pending_slots, "release", release_slot)
    monkeypatch.setattr(executor, "_run_one", run_one)
    try:
        # First owner enters the queue; the worker picks it up and parks on the
        # store read that precedes its token registration and the engine.
        executor.enqueue(prepared, source="https://example.com/v")
        assert in_gap.wait(timeout=5), "worker never reached the dequeue-to-token gap"

        second: list = []

        def enqueue_again():
            try:
                executor.enqueue(prepared, source="https://example.com/v")
                second.append("admitted")
            except Exception as exc:  # noqa: BLE001 - the refusal is the subject
                second.append(exc)
            finally:
                second_done.set()

        thread = threading.Thread(target=enqueue_again)
        thread.start()
        assert second_done.wait(timeout=5), "the second enqueue did not return"
        thread.join(10)

        # Let the worker finish its (first) item and drain any duplicate the gap
        # admitted, then stop the pool.
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(executions) < 2:
            time.sleep(0.01)
        time.sleep(0.2)
        executor.shutdown()
    finally:
        release.set()
        store.close()

    assert second and isinstance(second[0], Exception), (
        f"the gap admitted a duplicate enqueue: {second!r}"
    )
    assert len(executions) == 1, f"the job executed {len(executions)} times, expected 1"


# --- 3. admission failure cleanup ------------------------------------------


def test_queue_full_refusal_does_not_clobber_a_newer_claim(monkeypatch, tmp_path):
    """A refused admission must fail only the row *it* reopened, not a newer owner.

    The refusal writes an ERROR to the row it reopened. If another caller has
    re-claimed that row in the meantime (a fresh, deliberate retry), the refusal
    must not overwrite the newer owner's PENDING. The cleanup is conditional on the
    row still being the PENDING this refusal owns.
    """
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), engine="whisper", model="tiny", formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    try:
        job = _seed_terminal_job(store, request, media)

        released = threading.Event()
        started = threading.Event()

        def slow_transcribe(source, **kwargs):
            started.set()
            assert released.wait(timeout=5)
            return _result(source)

        from textflowkit.core import runner
        monkeypatch.setattr(runner, "transcribe", slow_transcribe)
        executor = JobExecutor(store, max_concurrency=1, max_pending=1)
        monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
        try:
            executor.submit(source="occupier")
            assert started.wait(timeout=5), "worker never picked the occupier up"
            executor.submit(source="holder")
            with pytest.raises(QueueFullError):
                submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=True, resume_job_id=job.id)

            after = store.get(job.id)
            # The row was reopened by our claim, then failed by the cleanup: it is
            # ERROR, not a PENDING orphan.
            assert after.state is JobState.ERROR, f"left {after.state}"
            assert "queue is full" in (after.error or "")
        finally:
            released.set()
            executor.shutdown()
    finally:
        store.close()


def test_shutdown_refusal_does_not_leave_a_pending_orphan(monkeypatch, tmp_path):
    """An executor that refuses because it is shut down must not strand the row.

    `enqueue` raises RuntimeError once the pool is shut down. The row was already
    reopened by the claim, so without cleanup it would sit PENDING with no worker.
    """
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), engine="whisper", model="tiny", formats=["json"])
    store = MemoryJobStore()
    job = _seed_terminal_job(store, request, media)

    executor = JobExecutor(store, max_concurrency=1)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
    try:
        executor.shutdown()  # not started: a submit would start it, enqueue refuses
        with pytest.raises(RuntimeError):
            submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                           background=True, resume_job_id=job.id)
        after = store.get(job.id)
        assert after.state is not JobState.PENDING, "shutdown refusal left a PENDING orphan"
        assert after.state is JobState.ERROR
    finally:
        store.close()


# --- 4. durable schema migration --------------------------------------------


def test_pre_attempt_sqlite_file_opens_and_reads_as_attempt_zero(tmp_path):
    """A file written before the attempt column existed must migrate cleanly.

    The migration adds the column with default 0, so a terminal row left by the
    old code reads as attempt 0 - which is exactly what a caller that observed it
    under the old code would have pinned.
    """
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE jobs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE, source TEXT NOT NULL, state TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            progress TEXT NOT NULL DEFAULT '', error TEXT, transcript TEXT,
            outputs TEXT NOT NULL DEFAULT '[]',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT, request TEXT);
        """
    )
    conn.execute(
        "INSERT INTO jobs (id, source, state, created_at, updated_at, outputs)"
        " VALUES ('old1', 'https://example.com/v', 'error', 1.0, 1.0, '[]')"
    )
    conn.commit()
    conn.close()

    store = SqliteJobStore(db)
    try:
        job = store.get("old1")
        assert job is not None
        assert job.state is JobState.ERROR
        assert job.attempt == 0
        # The migrated row is claimable with the attempt a reader would have seen.
        claimed = store.claim(
            "old1", allowed_states={JobState.ERROR}, observed_attempt=0,
            state=JobState.PENDING,
        )
        assert claimed is not None
        assert claimed.state is JobState.PENDING
    finally:
        store.close()
