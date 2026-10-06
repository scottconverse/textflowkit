"""G2A-admission-ownership: ownership must be identity-pinned through admission.

The first two ownership generations pinned *state* and then the *failure attempt*
(`test_atomic_resume.py`, `test_atomic_resume_aba.py`). Two narrower holes survive
both, because the attempt identity is fixed only when inference *starts*:

1. **Queued-cancellation ABA.** A resume reopens a terminal row to PENDING and
   queues it. A caller cancels it before a worker ever starts it, so it reads
   CANCELLED. A second caller observes that CANCELLED row, a third reopens and
   re-queues it (again PENDING, *still attempt 0* - inference has not started), and
   the first caller's queue-full/shutdown refusal then fires its cleanup. Because
   the counter advances only at RUNNING, the stale cleanup's row and the newer
   reclaim's row carry the *same* attempt, so a state-only conditional cleanup
   overwrites the newer PENDING with the stale "stale queue refusal" ERROR.

2. **Unadmitted identity.** A caller observes a terminal row, reclaims it, and the
   claim is refused admission *before* RUNNING (engine absent, queue full, pool
   shut down). The row returns to a terminal state without the counter having
   moved, so the same caller's stale observation - formed against the *old*
   failure - is accepted against a failure it never observed.

Both are the same root cause: a claim acquires no identity of its own until
inference starts. The repairs pin the identity at *acquisition* and require
cleanup and later decisions to name the exact identity they own. Only the
expensive `transcribe` stage is stubbed; the store, submission contract, and real
executor are exercised, and every wait is bounded.
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

# --- fixtures (mirroring the two generation-1/2 suites) ---------------------


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


def _make_store(store_kind: str, tmp_path: Path):
    return MemoryJobStore() if store_kind == "memory" else SqliteJobStore(tmp_path / "jobs.db")


# --- 1. stale cleanup must not overwrite a newer queued reclaim ---------------


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_stale_cleanup_does_not_overwrite_a_newer_queued_reclaim(
    monkeypatch, tmp_path, store_kind
):
    """The exact sequence the coordinator reproduced, driven end to end.

    old attempt 0 (ERROR) -> old claim (PENDING, queued-but-unadmitted) ->
    cancellation (CANCELLED) -> new claim (PENDING, *still attempt 0* under the
    old code, since no worker started either claim) -> the *old* claim's
    admission refusal fires its cleanup. That cleanup owns the row it reopened;
    it must not write its stale ERROR over the newer owner's PENDING.

    Admission and release here are entirely legitimate: no semaphore is poked by
    hand. ``occupier`` holds the single worker (so nothing is picked up while we
    arrange the interleaving); ``filler`` legitimately holds the single pending
    slot, which is what refuses A's enqueue with the real ``QueueFullError``.
    The newer reclaim is then admitted because the worker *naturally* frees the
    slot by dequeuing ``filler`` when ``occupier`` is released. Every wait is
    bounded, and the newer owner is a genuine fresh retry that must still run to
    DONE with its saved checkpoint intact.
    """
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), engine="whisper", model="tiny", formats=["json"])
    store = _make_store(store_kind, tmp_path)
    try:
        job = _seed_terminal_job(store, request, media, with_checkpoint=True)
        saved_checkpoint = store.get(job.id).checkpoint

        # Occupier holds the one worker; filler holds the one pending slot. Both
        # block in the engine so the worker cannot advance past them on its own.
        occupier_started = threading.Event()
        occupier_release = threading.Event()
        filler_started = threading.Event()
        filler_release = threading.Event()
        entered_engine = threading.Event()

        def block_transcribe(source, **kwargs):
            if source == "occupier":
                occupier_started.set()
                assert occupier_release.wait(timeout=5)
            elif source == "filler":
                filler_started.set()
                assert filler_release.wait(timeout=5)
            else:
                entered_engine.set()
            return _result(source)

        from textflowkit.core import runner
        monkeypatch.setattr(runner, "transcribe", block_transcribe)

        executor = JobExecutor(store, max_concurrency=1, max_pending=1)
        monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
        try:
            executor.submit(source="occupier")
            assert occupier_started.wait(timeout=5), "worker never picked the occupier up"
            # Fills the one pending slot: A's enqueue is now refused for real.
            executor.submit(source="filler")

            # --- old claim + its admission refusal, deferred ------------------
            # A is the first resume: it reopens the row to PENDING and *fails
            # admission* (queue full). Hold it right before its cleanup runs so
            # its reopen is committed but the cleanup has not yet fired, letting
            # the cancellation and the newer reclaim happen in between.
            at_cleanup = threading.Event()
            release_cleanup = threading.Event()
            original_fail_unadmitted = submission._fail_unadmitted

            def gated_fail_unadmitted(store_arg, job_id, progress, error, **kwargs):
                at_cleanup.set()
                assert release_cleanup.wait(timeout=5)
                return original_fail_unadmitted(store_arg, job_id, progress, error, **kwargs)

            monkeypatch.setattr(submission, "_fail_unadmitted", gated_fail_unadmitted)

            result_a: list = []

            def old_resume():
                try:
                    value = submit_request(
                        store, SubmissionRequest.from_dict(request.to_dict()),
                        background=True, resume_job_id=job.id,
                    )
                except Exception as exc:  # noqa: BLE001 - the refusal is the subject
                    value = exc
                result_a.append(value)

            thread_a = threading.Thread(target=old_resume, name="caller-a")
            thread_a.start()
            assert at_cleanup.wait(timeout=5), "A's admission refusal never fired"

            # The old claim reopened the row to PENDING (claimed, never queued).
            assert store.get(job.id).state is JobState.PENDING

            # --- cancellation before any worker starts it ---------------------
            assert executor.cancel(job.id) is True
            assert store.get(job.id).state is JobState.CANCELLED

            # --- the worker naturally frees the pending slot ------------------
            # Releasing the occupier lets the worker finish it and dequeue the
            # filler, which frees the one pending slot *through the product*, not
            # by poking the semaphore. The filler then blocks, keeping the worker
            # busy so the newer reclaim stays queued (PENDING) rather than running.
            occupier_release.set()
            assert filler_started.wait(timeout=5), "the worker never freed the pending slot"

            # --- newer claim: a fresh retry observes CANCELLED and reclaims ----
            new_pending = threading.Event()

            def new_resume():
                try:
                    value = submit_request(
                        store, SubmissionRequest.from_dict(request.to_dict()),
                        background=True, resume_job_id=job.id,
                    )
                except Exception as exc:  # noqa: BLE001
                    value = exc
                new_pending.set()
                return value

            thread_new = threading.Thread(target=new_resume, name="caller-new")
            thread_new.start()
            assert new_pending.wait(timeout=5), "the newer reclaim never returned"
            thread_new.join(10)
            assert store.get(job.id).state is JobState.PENDING, (
                f"newer reclaim left {store.get(job.id).state}, expected PENDING"
            )

            # --- the stale cleanup now fires against the newer PENDING ---------
            release_cleanup.set()
            thread_a.join(10)
            assert not thread_a.is_alive(), "A did not finish"
            assert isinstance(result_a[0], QueueFullError), repr(result_a)

            after = store.get(job.id)
            assert after.state is JobState.PENDING, (
                "the stale cleanup clobbered the newer owner: "
                f"state={after.state} error={after.error!r}"
            )
            assert after.error is None, f"stale error written over the newer claim: {after.error!r}"
            assert after.checkpoint == saved_checkpoint, "the saved checkpoint was lost"

            # --- the newer owner still runs, exactly once, to DONE -------------
            # Release the filler; the worker then dequeues the newer owner. The
            # saved transcript checkpoint means it completes without re-entering
            # the engine, but it must reach a terminal DONE rather than being left
            # PENDING (or having the stale ERROR land on it).
            filler_release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and store.get(job.id).state is not JobState.DONE:
                time.sleep(0.01)
            assert store.get(job.id).state is JobState.DONE, (
                f"the newer owner never completed: {store.get(job.id).state}"
            )
            assert entered_engine.is_set()
        finally:
            occupier_release.set()
            filler_release.set()
            executor.shutdown()
    finally:
        store.close()


# --- 2. an observation must not be accepted after a claim is unadmitted -------


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_observation_is_refused_after_an_unadmitted_claim(monkeypatch, tmp_path, store_kind):
    """A failed-before-RUNNING claim must still move the identity off the observed one.

    old claim reopens an ERROR row (attempt 0) to PENDING and is refused admission
    before any worker starts it, so the row never reaches RUNNING. The cleanup
    fails it back to ERROR - still attempt 0. A's decision was formed against that
    same ERROR/attempt 0, so a state-and-attempt claim accepts it against a
    failure it never observed. Ownership must be pinned to the claim's *own*
    identity at acquisition, not the (unmoved) inference attempt.
    """
    media = tmp_path / "media.wav"
    _wav(media)
    request = SubmissionRequest(source=str(media), engine="whisper", model="tiny", formats=["json"])
    store = _make_store(store_kind, tmp_path)
    try:
        job = _seed_terminal_job(store, request, media)

        # A's own admission is refused before RUNNING (engine absent). A holds its
        # reopened row committed and its cleanup deferred, so B can form a fresh
        # observation and reclaim in the window.
        at_unadmitted = threading.Event()
        release_a = threading.Event()
        original_fail_unadmitted = submission._fail_unadmitted

        def gated(store_arg, job_id, progress, error, **kwargs):
            at_unadmitted.set()
            assert release_a.wait(timeout=5)
            return original_fail_unadmitted(store_arg, job_id, progress, error, **kwargs)

        monkeypatch.setattr(submission, "_fail_unadmitted", gated)
        # A's admission must fail *before* any RUNNING, the way a queue-full or
        # shutdown refusal does. Build a real executor whose `enqueue` refuses, so
        # the shipped `submit_request` reopen-then-cleanup path runs unchanged.
        executor = JobExecutor(store, max_concurrency=1)
        monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
        monkeypatch.setattr(executor, "enqueue",
                            lambda job, **kwargs: (_ for _ in ()).throw(QueueFullError("full")))
        monkeypatch.setattr(executor, "start", lambda: None)

        result_a: list = []

        def resume_a():
            try:
                value = submit_request(
                    store, SubmissionRequest.from_dict(request.to_dict()),
                    background=True, resume_job_id=job.id,
                )
            except Exception as exc:  # noqa: BLE001 - the refusal is the subject
                value = exc
            result_a.append(value)

        thread_a = threading.Thread(target=resume_a, name="caller-a")
        thread_a.start()
        assert at_unadmitted.wait(timeout=5), "A's admission refusal never fired"

        # A reopened the row to PENDING; the queue then refuses it and the gated
        # fail_unadmitted holds there, with A's own acquisition committed.
        assert store.get(job.id).state is JobState.PENDING

        # Record the identity A observed and the identity its claim acquired.
        observed_attempt = 0
        acquired_attempt = store.get(job.id).attempt

        # A's cleanup now fires: it fails the row back to ERROR. The defect is that
        # this lands the row on the *observed* identity, so A's stale decision is
        # still honoured by an identity-keyed claim.
        release_a.set()  # let A's cleanup fail the row back to ERROR
        thread_a.join(10)
        assert not thread_a.is_alive(), "A did not finish"
        assert store.get(job.id).state is JobState.ERROR

        # A claim that names the identity A observed must be refused: the row has
        # moved on past it (A's acquisition advanced it). If it is accepted, the
        # unadmitted claim failed to advance the identity and a stale observation
        # reads as current - the defect.
        current = store.get(job.id)
        assert current.attempt != observed_attempt or acquired_attempt != observed_attempt, (
            "the unadmitted claim did not advance the identity: a stale observation "
            f"(ERROR attempt {observed_attempt}) was accepted as current"
        )
        stale_claim = store.claim(
            job.id, allowed_states={JobState.ERROR},
            observed_attempt=observed_attempt,
            state=JobState.PENDING,
        )
        assert stale_claim is None, (
            "a stale observation was accepted after an unadmitted claim: "
            f"attempt={current.attempt}"
        )

        # And a genuinely fresh retry that observes the current row still runs.
        executions: list = []

        def fake_transcribe(source, **kwargs):
            executions.append(source)
            return _result(source)

        from textflowkit.core import runner
        monkeypatch.setattr(runner, "transcribe", fake_transcribe)

        retry = submit_request(store, SubmissionRequest.from_dict(request.to_dict()),
                               background=False, resume_job_id=job.id)
        assert retry.state is JobState.DONE, "a fresh retry must still be allowed"
        assert len(executions) == 1, "the fresh retry did not run exactly once"
    finally:
        store.close()
