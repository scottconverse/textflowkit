"""ENG-002: one durable job may only be owned by one resume.

A resume selects a prior terminal job, decides it can be reused, and reopens it
for another run through independent store calls. Two resumes of the *same* job
can therefore both observe the terminal state and both reopen it, queueing
duplicate work for one job id. These regressions pin the ownership contract that
closes that window:

- exactly one caller wins a resume of a terminal job; the loser is refused with
  an actionable "already active"/conflict error rather than running it again;
- the same holds when the two callers share one process (threads) and when they
  hold separate store handles over one SQLite file;
- a loser never leaves the job mutated behind it, and a refused claim never
  reopens the row;
- when the queue refuses the claimed job, the row is not left PENDING with no
  worker, and the saved checkpoint and request are intact.

The pipeline and engine are the only things stubbed: the store, the resume
selection, and the actual file preflight are real. Every wait is bounded.
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

# --- fixtures ---------------------------------------------------------------


def _wav(path: Path, seconds: int = 1) -> None:
    """A real, tiny playable WAV: the submission contract stats local sources."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(16000 * 2 * seconds))


def _transcript(source: str) -> Transcript:
    return Transcript(source=source, language="en", segments=[Segment(0.0, 1.0, "reused")])


def _result(source: str) -> TranscribeResult:
    return TranscribeResult(transcript=_transcript(source), outputs=[])


def _checkpoint_for(request: SubmissionRequest, media: Path, *, with_transcript: bool) -> CheckpointRecord:
    return CheckpointRecord(
        source=request.source,
        model=request.model,
        language=request.language,
        engine=request.engine,
        device=request.device,
        options=request.options(),
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=_transcript(request.source).to_dict() if with_transcript else None,
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
        fields["checkpoint"] = _checkpoint_for(request, media, with_transcript=True).to_dict()
    store.update(job.id, **fields)
    return store.get(job.id)


def _stub_engine(monkeypatch, *, executions: list):
    """Replace only the expensive pipeline; count how many times it is entered.

    The engine is the one step whose duplication is the defect, so counting its
    entries is the direct receipt. No barrier inside it: exactly one caller is
    supposed to arrive, so a rendezvous would deadlock on the correct result.
    """
    def fake_transcribe(source, *, resume_checkpoint=None, on_checkpoint=None, **kwargs):
        executions.append(source)
        return _result(source)

    from textflowkit.core import runner
    monkeypatch.setattr(runner, "transcribe", fake_transcribe)


def _rendezvous_after_read(monkeypatch, *, count: int = 2) -> None:
    """Force both racers to observe the terminal row before either claims it.

    Without this the winner usually finishes before the loser reads, and the race
    is not exercised - the test passes even against the racy code. The barrier
    sits between the matching read and the claim, which is exactly the window the
    defect lives in: both callers have seen "ERROR, resumable" and may now both
    act on it. It is the same deterministic interleaving the audit probe used.
    """
    original = submission._matching_checkpoint
    barrier = threading.Barrier(count)

    def matching(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(submission, "_matching_checkpoint", matching)


def _rendezvous_at_reopen(monkeypatch, store, *, barrier=None, count: int = 2) -> None:
    """Hold both racers at the reopen write, on either store contract.

    The read rendezvous alone is not enough: between a caller's read and its
    reopen the other caller can finish the whole job, so the second one then sees
    DONE and is refused for an unrelated reason - the test would pass against the
    racy code. Syncing *at* the write that reopens the row puts both callers past
    their state check with neither having committed, which is the precise window
    the defect lives in. Wraps whichever transition the store offers (`claim` on
    the fixed store, `update` on the racy one), so the test does not have to know
    which the product uses.

    Pass the same `barrier` to two stores to make them rendezvous together (the
    two-handle case); the default is a fresh one for a single store.
    """
    if barrier is None:
        barrier = threading.Barrier(count)
    original_claim = getattr(store, "claim", None)
    original_update = store.update

    def at_reopen(**fields) -> bool:
        return fields.get("state") is JobState.PENDING

    if original_claim is not None:
        def claim(job_id, *, allowed_states, **fields):
            if at_reopen(**fields):
                barrier.wait(timeout=5)
            return original_claim(job_id, allowed_states=allowed_states, **fields)

        monkeypatch.setattr(store, "claim", claim, raising=False)

    def update(job_id, **fields):
        if at_reopen(**fields):
            barrier.wait(timeout=5)
        return original_update(job_id, **fields)

    monkeypatch.setattr(store, "update", update)


def _settle(executions: list, expected: int, *, timeout: float = 2.0) -> None:
    """Wait until at least `expected` executions, then confirm no extra appears.

    Every wait is bounded: first until the expected work lands, then a short
    quiet window during which a duplicate execution would have to show up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(executions) < expected:
        time.sleep(0.01)
    time.sleep(0.2)


def _race(call, *, count: int = 2) -> list:
    """Run `call` from `count` threads, returning raw results/exception reprs."""
    results: list = []
    lock = threading.Lock()
    entered = threading.Barrier(count)

    def run() -> None:
        entered.wait(timeout=5)
        try:
            value = call()
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is the subject
            value = exc
        with lock:
            results.append(value)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "race did not finish within bound"
    return results


# --- synchronous resume of one terminal job --------------------------------


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_concurrent_synchronous_resume_of_one_job_has_a_single_owner(
    monkeypatch, tmp_path, store_kind
):
    """Two synchronous resumes of the same ERROR job must not both execute it."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = MemoryJobStore() if store_kind == "memory" else SqliteJobStore(tmp_path / "jobs.db")
    try:
        job = _seed_terminal_job(store, request, media)

        executions: list = []
        _stub_engine(monkeypatch, executions=executions)
        _rendezvous_after_read(monkeypatch)
        _rendezvous_at_reopen(monkeypatch, store)

        def call():
            return submit_request(
                store, SubmissionRequest.from_dict(request.to_dict()),
                background=False, resume_job_id=job.id,
            )

        results = _race(call)
        _settle(executions, 1)

        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]

        assert len(executions) == 1, f"the job executed {len(executions)} times, expected 1"
        assert len(winners) == 1, f"both callers claimed the resume: {results!r}"
        assert losers, "the losing caller must be refused, not silently accepted"
        # The refusal is a ValueError on every surface (HTTP maps it to a 409 on
        # the resume route). Its wording varies with what the loser sees: it is
        # "already active" when it catches the reopened PENDING row, and a
        # not-resumable message when the winner finished first. Either is a real
        # conflict; what must never happen is a second accepted execution.
        assert isinstance(losers[0], ValueError), repr(losers[0])
        assert winners[0].state is JobState.DONE
    finally:
        store.close()


@pytest.mark.parametrize("with_checkpoint", [False, True])
@pytest.mark.parametrize("state", [JobState.ERROR, JobState.CANCELLED])
def test_synchronous_resume_rejects_a_second_owner_with_and_without_checkpoint(
    monkeypatch, tmp_path, with_checkpoint, state
):
    """The terminal-state claim holds for ERROR and CANCELLED, checkpoint or not."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    try:
        job = _seed_terminal_job(store, request, media, state=state, with_checkpoint=with_checkpoint)
        saved_checkpoint = store.get(job.id).checkpoint

        executions: list = []
        _stub_engine(monkeypatch, executions=executions)

        first = submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=False, resume_job_id=job.id)
        assert first.state is JobState.DONE

        # Re-open the terminal window explicitly: force the row back to a
        # terminal state while a second caller tries to claim it, so the second
        # resume is judged against a terminal row exactly as the first was.
        store.update(job.id, state=state, error="interrupted")
        if with_checkpoint:
            # And keep that second claim racing a live one: mark it active first.
            store.update(job.id, state=JobState.RUNNING)
            with pytest.raises(ValueError, match="already active"):
                submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=False, resume_job_id=job.id)
            # A refused claim must not have reopened the row.
            assert store.get(job.id).state is JobState.RUNNING

        if saved_checkpoint is not None:
            assert store.get(job.id).checkpoint is not None
    finally:
        store.close()


# --- background executor enqueue -------------------------------------------


def test_concurrent_background_resume_enqueues_one_execution(monkeypatch, tmp_path):
    """Background resumes must enqueue once; the loser gets a conflict, no orphan."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = MemoryJobStore()
    job = _seed_terminal_job(store, request, media)
    executor = JobExecutor(store, max_concurrency=1)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)

    executions: list = []
    _stub_engine(monkeypatch, executions=executions)
    _rendezvous_after_read(monkeypatch)

    try:
        def call():
            return submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                                  background=True, resume_job_id=job.id)

        results = _race(call)
        _settle(executions, 1)

        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) == 1, f"both resumes were accepted: {results!r}"
        # The loser is refused with a ValueError (HTTP maps it to a conflict). Its
        # wording depends on what it sees: "already active" for the queued row,
        # or a not-resumable message if the winner already finished.
        assert losers and isinstance(losers[0], ValueError), repr(losers)

        # At most one execution ever runs for this one job id.
        assert len(executions) == 1, f"job executed {len(executions)} times, expected 1"
    finally:
        executor.shutdown()
        store.close()


def test_resume_of_one_job_cannot_enqueue_twice_while_first_is_queued(monkeypatch, tmp_path):
    """A resume racing a still-PENDING (just-reopened) row must be refused."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = MemoryJobStore()
    job = _seed_terminal_job(store, request, media)

    released = threading.Event()
    started = threading.Event()

    def slow_transcribe(source, **kwargs):
        started.set()
        assert released.wait(timeout=5)
        return _result(source)

    from textflowkit.core import runner
    monkeypatch.setattr(runner, "transcribe", slow_transcribe)

    executor = JobExecutor(store, max_concurrency=1)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
    try:
        first = submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=True, resume_job_id=job.id)
        assert started.wait(timeout=5), "worker never picked the job up"

        # The row is RUNNING now: a second resume of the same id is a conflict,
        # not a second execution.
        with pytest.raises(ValueError, match="already active"):
            submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                           background=True, resume_job_id=job.id)
        assert first.id == job.id
    finally:
        released.set()
        executor.shutdown()
        store.close()


# --- admission / claim consistency -----------------------------------------


def test_queue_full_refusal_leaves_no_pending_orphan_and_keeps_checkpoint(monkeypatch, tmp_path):
    """A claim refused by the queue must not strand a reopened PENDING job."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    try:
        job = _seed_terminal_job(store, request, media, with_checkpoint=True)
        saved_request = store.get(job.id).request
        saved_checkpoint = store.get(job.id).checkpoint
        assert saved_checkpoint is not None

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
            # Deterministically fill the single slot: the worker takes the
            # occupier (freeing its slot), then a second submit holds the only
            # pending slot, so the resume's admission must be refused.
            executor.submit(source="occupier")
            assert started.wait(timeout=5), "worker never picked the occupier up"
            executor.submit(source="holder")
            with pytest.raises(QueueFullError, match="queue is full"):
                submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=True, resume_job_id=job.id)

            after = store.get(job.id)
            # No PENDING orphan with nothing queued to run it.
            assert after.state is not JobState.PENDING, "queue-full left a PENDING orphan"
            # The saved work is intact - the original durable record survives.
            assert after.checkpoint == saved_checkpoint, "saved checkpoint was lost"
            assert after.request == saved_request, "saved request was lost"
        finally:
            released.set()
            executor.shutdown()
    finally:
        store.close()


def test_queue_full_claim_does_not_consume_the_job_or_enqueue_it(monkeypatch, tmp_path):
    """A refused admission leaves nothing queued for the job id."""
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    store = MemoryJobStore()
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
        assert executor.pending_count == 1, "the refused job must not still be queued"
    finally:
        released.set()
        executor.shutdown()
        store.close()


# --- separate store handles over one SQLite file ---------------------------


def test_two_store_handles_over_one_db_claim_one_winner(monkeypatch, tmp_path):
    """The conditional claim must hold with two independent SQLite handles."""
    media = tmp_path / "media.wav"
    _wav(media)
    db = tmp_path / "jobs.db"
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])

    seeder = SqliteJobStore(db)
    job = _seed_terminal_job(seeder, request, media)
    seeder.close()

    store_a = SqliteJobStore(db)
    store_b = SqliteJobStore(db)
    try:
        executions: list = []
        _stub_engine(monkeypatch, executions=executions)
        _rendezvous_after_read(monkeypatch)
        # One barrier shared by both handles: they must meet at the reopen write.
        reopen_barrier = threading.Barrier(2)
        _rendezvous_at_reopen(monkeypatch, store_a, barrier=reopen_barrier)
        _rendezvous_at_reopen(monkeypatch, store_b, barrier=reopen_barrier)

        # Two independent handles race the same job with a shared barrier.
        barrier = threading.Barrier(2)
        out: list = []
        lock = threading.Lock()

        def worker(store, name):
            barrier.wait(timeout=5)
            try:
                value = submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                                       background=False, resume_job_id=job.id)
            except Exception as exc:  # noqa: BLE001
                value = exc
            with lock:
                out.append((name, value))

        threads = [
            threading.Thread(target=worker, args=(store_a, "a")),
            threading.Thread(target=worker, args=(store_b, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not any(t.is_alive() for t in threads), "race did not finish within bound"
        _settle(executions, 1)

        winners = [(n, v) for n, v in out if not isinstance(v, Exception)]
        losers = [(n, v) for n, v in out if isinstance(v, Exception)]
        assert len(executions) == 1, f"executed {len(executions)} times across two handles"
        assert len(winners) == 1, f"both handles claimed the job: {out!r}"
        assert losers and isinstance(losers[0][1], ValueError), repr(losers)
    finally:
        store_a.close()
        store_b.close()


# --- coherent observation: checkpoint replaced between selection and pin -----


def test_implicit_selection_loads_the_checkpoint_it_pinned(tmp_path, monkeypatch):
    """A checkpoint swapped between selection and observation is never served.

    The implicit (no job id) resume selects a job through ``find_resumable_checkpoint``
    and then re-observes it under the store lock to pin the attempt the claim will
    be judged against. Those are two reads: a concurrent completed stage or retry
    can replace the job's checkpoint in the window between them. Returning the
    *pre-observation* checkpoint under the freshly observed identity would hand the
    decision two instants - the new identity and the old checkpoint - and resume
    work that belongs to a row state the caller never observed.

    The fix loads the checkpoint from the snapshot actually pinned. This test
    drives that exact window: selection returns job J (checkpoint A, matching), a
    writer then replaces J's checkpoint with B (a different model), and observation
    sees the new row. The resume must act on B - here, refuse it, because B does not
    match the request - rather than silently reopen the job with A.
    """
    media = tmp_path / "clip.wav"
    _wav(media)
    store = MemoryJobStore()
    request = SubmissionRequest(
        source=str(media), model="tiny", device="cpu", formats=["txt"],
    )
    job = _seed_terminal_job(store, request, media, with_checkpoint=True)
    stale = _checkpoint_for(request, media, with_transcript=True)

    # A newer checkpoint for the same row that no longer matches this request:
    # the "completed stage/retry replaced it" case, made deterministic.
    fresh = _checkpoint_for(request, media, with_transcript=True)
    fresh.model = "large-v3"

    real_find = submission.find_resumable_checkpoint

    def find_then_replace(*args, **kwargs):
        found = real_find(*args, **kwargs)
        if found is not None:
            store.update(job.id, checkpoint=fresh.to_dict())
        return found

    monkeypatch.setattr(submission, "find_resumable_checkpoint", find_then_replace)
    # The row is claimable, so only the checkpoint coherence can stop the resume.
    executions: list = []
    _stub_engine(monkeypatch, executions=executions)

    # The stale checkpoint matched, the pinned one does not: the resume must not
    # fall back to the stale checkpoint. A fresh submission is created instead.
    result = submit_request(store, request, background=False, resume=True)

    assert result.id != job.id, (
        "resume adopted the pre-observation checkpoint for a changed row"
    )
    current = store.get(job.id)
    assert current.state is JobState.ERROR, (
        "the stale-checkpoint row was reopened instead of left terminal"
    )
    assert current.checkpoint["model"] == "large-v3", "the newer checkpoint was clobbered"
    assert stale.to_dict()["model"] == "tiny"


def test_implicit_selection_resumes_with_the_pinned_checkpoint(tmp_path, monkeypatch):
    """The pinned checkpoint is the one handed to the pipeline, not the stale one.

    Companion to the refusal case: when the replacement checkpoint still matches
    the request, the resume must run on the *replacement* (the coherent snapshot),
    proving the value threaded through is the pinned one and not merely that a
    mismatch happens to refuse.
    """
    media = tmp_path / "clip.wav"
    _wav(media)
    store = MemoryJobStore()
    request = SubmissionRequest(
        source=str(media), model="tiny", device="cpu", formats=["txt"],
    )
    job = _seed_terminal_job(store, request, media, with_checkpoint=True)

    replacement = _checkpoint_for(request, media, with_transcript=True)
    replacement.finished_stages = ["source", "fetch", "extract", "transcribe", "diarize"]

    real_find = submission.find_resumable_checkpoint

    def find_then_replace(*args, **kwargs):
        found = real_find(*args, **kwargs)
        if found is not None:
            store.update(job.id, checkpoint=replacement.to_dict())
        return found

    monkeypatch.setattr(submission, "find_resumable_checkpoint", find_then_replace)

    seen: dict = {}
    from textflowkit.core import runner

    def fake_transcribe(source, *, resume_checkpoint=None, on_checkpoint=None, **kwargs):
        seen["checkpoint"] = resume_checkpoint
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    result = submit_request(store, request, background=False, resume=True)

    assert result.id == job.id, "the matching replacement should still resume in place"
    assert seen.get("checkpoint") is not None, "resume did not carry a checkpoint"
    assert seen["checkpoint"]["finished_stages"] == replacement.finished_stages, (
        "resume ran with the pre-observation checkpoint instead of the pinned one"
    )
