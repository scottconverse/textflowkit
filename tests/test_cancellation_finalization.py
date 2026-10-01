"""G2D-cancel-finalization: an accepted cancellation must own the terminal row.

ENG-006. The runner's terminal commit re-reads the job and only checks
``state is CANCELLED`` before writing DONE; ``JobExecutor.cancel`` accepts a
cancellation by tripping a token and setting ``cancel_requested`` on a row that
is still RUNNING. Between those two points is a window: a run whose pipeline has
already returned successfully, but whose terminal write has not landed, reads a
row that is still RUNNING (``cancel_requested`` True) and publishes DONE
*unconditionally* - a finished-looking job that nonetheless records an accepted
cancellation. The baseline only tests the *state*, so the accepted flag is
ignored and DONE wins. That is the actual bug, reproduced below end to end.

These tests are RED-first and deterministic. They drive the real store
(`MemoryJobStore` and `SqliteJobStore`), the real `JobExecutor`, and the real
`run_job`; only the expensive `runner.transcribe` stage is stubbed. Nothing here
loads a model or touches the network: a local synthetic WAV satisfies the
submission contract's existence check and is never read.

Every wait is bounded and every event released in a ``finally`` so no worker is
left blocked, even when a RED assertion fires mid-test.

The terminal-commit seam is the store's atomic ``finalize_done``: it chooses
CANCELLED when the run's own generation carries an accepted flag and DONE
otherwise, in one write. ``test_cancel_accepted_at_the_terminal_commit_boundary_wins``
arranges its race through that method (documented in the design note). The
remaining tests drive the real store methods the executor and runner use -
``begin_attempt``, ``accept_cancel``, ``finalize_done``, ``claim`` - with no
wrappers on methods the product does not call.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path

import pytest

from textflowkit.core import runner
from textflowkit.core.executor import JobExecutor, TerminalWrite
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import TranscribeResult
from textflowkit.core.sqlite_store import SqliteJobStore

WAIT = 5.0


# --- fixtures -------------------------------------------------------------


def _wav(path: Path) -> None:
    """A real, tiny WAV: the submission contract stats local sources.

    Nothing reads the samples; the bytes only have to satisfy stat/existence.
    """
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(16000 * 2))


def _result(source: str, text: str = "done") -> TranscribeResult:
    return TranscribeResult(
        transcript=Transcript(
            source=source, language="en", segments=[Segment(0.0, 1.0, text)]
        ),
        outputs=[],
    )


def _executor(store, **kwargs) -> JobExecutor:
    return JobExecutor(store, max_concurrency=1, **kwargs)


def _wait_for(predicate, *, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _store(kind: str, tmp_path: Path):
    if kind == "memory":
        return MemoryJobStore()
    return SqliteJobStore(tmp_path / "jobs.db")


STORE_KINDS = ["memory", "sqlite"]


# ==========================================================================
# 1. RED: a late accepted cancellation must not end DONE
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_late_cancellation_after_successful_pipeline_ends_cancelled(
    monkeypatch, tmp_path, kind
):
    """The exact ENG-006 sequence, driven end to end on the real executor.

    The runner's pipeline has already returned a successful result, but the
    terminal row is not yet committed. A cancellation is accepted in that
    window: the token trips and ``cancel_requested`` is set on a RUNNING row.
    The terminal commit must then finish CANCELLED - never DONE with
    ``cancel_requested`` True - because the cancellation is cooperative and
    *accepted*, and the honest end for it is CANCELLED.

    Baseline: the runner reads the latest row and only compares
    ``state is CANCELLED``; the row is still RUNNING, so it writes DONE
    unconditionally. RED asserts on the published row, so it fails here.

    The stub models a long stage that ignores cancellation until it returns
    (real model calls cannot be interrupted mid-call), then returns success -
    which is why the run reaches the terminal commit at all.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    pipeline_returned = threading.Event()
    release_to_commit = threading.Event()

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        if check_cancel:
            check_cancel()  # cooperative checkpoint before the long stage
        pipeline_returned.set()
        # The long stage: it ignores cancellation until it returns, exactly like
        # a single model call. The test holds it here so the cancellation lands
        # squarely between "pipeline succeeded" and "terminal commit".
        assert release_to_commit.wait(timeout=WAIT), "release never fired"
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    try:
        job = executor.submit(source=str(media))

        assert pipeline_returned.wait(timeout=WAIT), "pipeline never reached the long stage"

        # --- accepted cancellation, mid-run -------------------------------
        assert executor.cancel(job.id) is True
        mid = store.get(job.id)
        assert mid.state is JobState.RUNNING, f"expected RUNNING mid-cancel, got {mid.state}"
        assert mid.cancel_requested is True
        assert mid.progress == "cancelling"

        # --- the pipeline now returns success and the run commits ---------
        release_to_commit.set()

        assert _wait_for(
            lambda: store.get(job.id).state in {JobState.DONE, JobState.ERROR, JobState.CANCELLED}
        ), "the run never reached a terminal row"
        final = store.get(job.id)

        assert final.state is JobState.CANCELLED, (
            "an accepted cancellation must finish CANCELLED, not "
            f"{final.state} (cancel_requested={final.cancel_requested})"
        )
        assert final.cancel_requested is True
        assert final.state is not JobState.DONE, "DONE must never carry an accepted cancellation"
    finally:
        release_to_commit.set()
        executor.shutdown()


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_completion_wins_when_cancel_is_refused(monkeypatch, tmp_path, kind):
    """If the run reaches DONE first, cancel returns False and DONE is unchanged.

    This is the complement of the red case: the same runner/engine plumbing,
    but the terminal commit lands *before* cancellation. `cancel` on a terminal
    row must refuse (False) and must not rewrite the finished job. It pins the
    boundary the fix must respect - accepting a late cancellation is only
    correct while the row is still non-terminal.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        return _result(source, "completed")

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    try:
        job = executor.submit(source=str(media))
        assert _wait_for(
            lambda: store.get(job.id).state is JobState.DONE
        ), "the run never completed"

        before = store.get(job.id)
        assert executor.cancel(job.id) is False, "cancel accepted against a DONE job"
        after = store.get(job.id)

        assert after.state is JobState.DONE
        assert after.cancel_requested is False
        assert after.transcript == before.transcript
        assert after.progress == before.progress
    finally:
        executor.shutdown()


# ==========================================================================
# 2. RED: a cancellation accepted AT the terminal commit boundary must win
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_cancel_accepted_at_the_terminal_commit_boundary_wins(monkeypatch, tmp_path, kind):
    """Arrange the race at the exact store seam the terminal write goes through.

    The runner commits its terminal row with the atomic ``store.finalize_done``.
    A cancellation accepted (``cancel_requested`` set, row still RUNNING) at the
    instant just before that write must still win: the accepted cancellation owns
    the outcome.

    Arrangement: wrap ``finalize_done`` so that, immediately before it decides,
    the accepted cancellation is applied through the *real* acceptance path,
    ``JobExecutor.cancel`` - which trips the token and lands the acceptance on
    the still-RUNNING row via the store's atomic ``accept_cancel``. The seam is
    the same boundary the fix makes atomic, so the arrangement lives on the real
    method the runner calls rather than on a call the product no longer makes.
    This runs the real executor and the real ``run_job`` on a worker thread, so
    it is end-to-end.

    ``finalize_done`` is called by the runner as
    ``finalize_done(job.id, observed_attempt=owned, **fields)`` - it does NOT
    pass ``state`` (the terminal choice is the store's, not a caller argument),
    so the boundary hook keys on a call, not on ``fields["state"]``. Acceptance
    is asserted true here: if the cancellation were refused (the row already
    terminal, or a newer generation) the test would be proving nothing.

    Baseline: the terminal write read state (RUNNING), ignored
    ``cancel_requested``, and published DONE. RED.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        # The pipeline returns success. It must NOT honour the token: the
        # cancellation we inject at the commit boundary is a cancellation the
        # run has already passed the last in-pipeline checkpoint of, so only the
        # terminal commit can honour it. Checking the token here would end
        # CANCELLED for the wrong reason and hide the bug.
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    boundary_cancel_applied = threading.Event()
    boundary_cancel_accepted: list[bool] = []
    original_finalize = store.finalize_done

    def finalize_with_boundary_cancel(job_id, **fields):
        # The runner calls this with ``observed_attempt=owned`` and the terminal
        # fields; it does not pass ``state``. Intercept the first call - that *is*
        # the terminal-commit boundary - and apply the accepted cancellation
        # through the real path (trip the token + atomic accept_cancel on the
        # still-RUNNING row) before the real finalize decides.
        #
        # ``observed_attempt`` rides through ``**fields`` to the real method.
        if not boundary_cancel_applied.is_set():
            boundary_cancel_applied.set()
            # The real cancellation path: asserts the acceptance really landed.
            boundary_cancel_accepted.append(executor.cancel(job_id))
        return original_finalize(job_id, **fields)

    monkeypatch.setattr(store, "finalize_done", finalize_with_boundary_cancel)

    try:
        # submit returns immediately; the pool's single worker drives the run.
        job = executor.submit(source=str(media))
        assert _wait_for(
            lambda: store.get(job.id).state
            in {JobState.DONE, JobState.ERROR, JobState.CANCELLED}
        ), "the run never reached a terminal row"
        assert boundary_cancel_applied.is_set(), "the boundary cancel was never reached"
        assert boundary_cancel_accepted == [True], (
            "the boundary cancellation was not accepted against the running row "
            f"(cancel returned {boundary_cancel_accepted!r}); the test would prove "
            "nothing"
        )

        final = store.get(job.id)
        assert final.state is JobState.CANCELLED, (
            "a cancellation accepted at the terminal-commit boundary must win, "
            f"got {final.state} (cancel_requested={final.cancel_requested})"
        )
        assert final.cancel_requested is True
        assert final.progress == "cancelled"
        assert final.state is not JobState.DONE
    finally:
        executor.shutdown()


# ==========================================================================
# 3. RED: a queued cancellation must not be resurrected into RUNNING
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_queued_cancel_is_not_resurrected_by_a_later_start(
    monkeypatch, tmp_path, kind
):
    """A queued job cancelled before pickup stays CANCELLED and never runs.

    Observable half of the resurrection contract, testable against the current
    API: hold the single worker on a blocker so a second job is genuinely queued
    but unstarted, cancel it (the row goes CANCELLED immediately), then release
    the worker so it reaches the second job. The worker must skip it and the row
    must stay CANCELLED - never flip back to RUNNING with the engine entered.

    The narrower *interleaving* - a cancel landing precisely between the
    worker's pre-start read and its start write - is pinned separately by
    ``test_cancel_landing_at_the_start_boundary_is_not_resurrected``, which
    arranges it on the real guarded start. This test guards the coarse
    observable so a fix cannot regress it.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    blocker_started = threading.Event()
    blocker_release = threading.Event()
    ran: list[str] = []

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        ran.append(source)
        if source.endswith("blocker.wav"):
            blocker_started.set()
            assert blocker_release.wait(timeout=WAIT), "blocker never released"
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    try:
        blocker = tmp_path / "blocker.wav"
        _wav(blocker)
        first = executor.submit(source=str(blocker))
        assert blocker_started.wait(timeout=WAIT), "worker never picked the blocker up"

        queued = executor.submit(source=str(media))  # waits in the queue
        assert executor.cancel(queued.id) is True
        assert store.get(queued.id).state is JobState.CANCELLED

        blocker_release.set()
        assert _wait_for(
            lambda: store.get(first.id).state is JobState.DONE
        ), "the blocker never completed"

        final = store.get(queued.id)
        assert final.state is JobState.CANCELLED, (
            f"a queued cancellation was resurrected to {final.state}"
        )
        assert not any(src.endswith("media.wav") for src in ran), (
            "a cancelled queued job still entered the engine"
        )
    finally:
        blocker_release.set()
        executor.shutdown()


# ==========================================================================
# 4. Ownership / generation preservation (adapter-agnostic contract probes)
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_accepted_cancel_does_not_erase_a_newer_retry_identity(tmp_path, kind):
    """A cancelled attempt must not be able to overwrite a later generation.

    Probes the store contract the fix must preserve: a guarded transition named
    against an observed attempt is refused once a newer claim has advanced the
    row. This is what keeps a stale cancellation verdict from landing on a
    freshly reclaimed retry (the cross-unit unknown-worker-fault concern).
    """
    store = _store(kind, tmp_path)
    media = tmp_path / "media.wav"
    _wav(media)
    try:
        job = store.create(str(media))
        observed = store.observe(job.id)

        # A newer owner acquires the row (the retry generation).
        reclaimed = store.claim(
            job.id,
            allowed_states={JobState.PENDING},
            observed_attempt=observed.attempt,
            state=JobState.PENDING,
            advance_attempt=True,
        )
        assert reclaimed is not None
        newer_attempt = reclaimed.attempt
        assert newer_attempt != observed.attempt

        # The stale cancellation, pinned to the observation it was formed
        # against, must be refused now that the row moved on.
        stale = store.claim(
            job.id,
            allowed_states={JobState.PENDING, JobState.RUNNING},
            observed_attempt=observed.attempt,
            state=JobState.CANCELLED,
            cancel_requested=True,
        )
        assert stale is None, "a stale cancellation overwrote a newer generation"
        assert store.get(job.id).attempt == newer_attempt
        assert store.get(job.id).state is JobState.PENDING
    finally:
        store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_cancelled_row_keeps_its_own_generation_identity(tmp_path, kind):
    """The cancellation verdict must be pinnable to the attempt it terminated.

    A guarded terminal write for an accepted cancellation, named against the
    attempt in flight, must land on that attempt and only that attempt. This is
    the identity a later reconciliation (and the unknown-fault read) must carry
    rather than adopting whatever owner is current.
    """
    store = _store(kind, tmp_path)
    media = tmp_path / "media.wav"
    _wav(media)
    try:
        job = store.create(str(media))
        started = store.begin_attempt(job.id)
        owned = started.attempt

        landed = store.claim(
            job.id,
            allowed_states={JobState.RUNNING},
            observed_attempt=owned,
            state=JobState.CANCELLED,
            progress="cancelled",
            cancel_requested=True,
        )
        assert landed is not None, "the accepted cancellation did not land on its own attempt"
        assert landed.attempt == owned
        assert landed.state is JobState.CANCELLED
        assert landed.cancel_requested is True
    finally:
        store.close()


# ==========================================================================
# 5. Canonical transcript is stored once, on DONE, and is not clobbered
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_done_keeps_a_single_canonical_transcript_on_the_job_field(
    monkeypatch, tmp_path, kind
):
    """Completion is untouched by this unit: one transcript, on the job field.

    Guards the contract the fix must not disturb - the transcript travels on the
    job, the checkpoint keeps metadata only, and both land in one update so no
    reader sees a row holding neither copy.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        return _result(source, "canonical")

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    try:
        job = executor.submit(source=str(media))
        assert _wait_for(
            lambda: store.get(job.id).state is JobState.DONE
        ), "the run never completed"

        done = store.get(job.id)
        assert done.transcript is not None
        assert done.transcript["segments"][0]["text"] == "canonical"
        if done.checkpoint is not None:
            assert done.checkpoint.get("transcript") is None, (
                "the checkpoint must keep metadata only, not a second transcript copy"
            )
    finally:
        executor.shutdown()


# ==========================================================================
# 6. The start boundary: a cancellation landing there cannot be resurrected
# ==========================================================================


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_cancel_landing_at_the_start_boundary_is_not_resurrected(
    monkeypatch, tmp_path, kind
):
    """A cancel accepted exactly at the guarded start must not let the run in.

    The coarse queued case is covered above; this pins the narrow interleaving:
    the worker has read the row (still PENDING) and is at the instant before its
    start write when the cancellation is accepted. The guarded
    ``begin_attempt`` must refuse - a row that is no longer PENDING, or already
    carries the accepted flag, is not this run's to start - and the run must
    return without entering the engine.

    Arrangement: wrap the store's ``begin_attempt`` so the accepted cancellation
    is applied through the real ``executor.cancel`` immediately before the start
    is attempted. That is the exact boundary the guard exists for, and it uses
    only methods the product calls.

    Baseline: the start was unconditional, so the row went RUNNING and the engine
    was entered - the resurrection. RED at baseline, GREEN now.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    ran: list[str] = []
    boundary_applied = threading.Event()
    original_begin = store.begin_attempt

    def fake_transcribe(source, *, check_cancel=None, **kwargs):
        ran.append(source)
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    def begin_with_boundary_cancel(job_id, **fields):
        if not boundary_applied.is_set():
            boundary_applied.set()
            # The real cancellation path, landing between the worker's pre-start
            # read and its start write.
            executor.cancel(job_id)
        return original_begin(job_id, **fields)

    monkeypatch.setattr(store, "begin_attempt", begin_with_boundary_cancel)

    try:
        job = executor.submit(source=str(media))
        assert _wait_for(
            lambda: store.get(job.id).state
            in {JobState.CANCELLED, JobState.DONE, JobState.ERROR}
        ), "the run never reached a terminal row"
        assert boundary_applied.is_set(), "the start boundary was never reached"

        final = store.get(job.id)
        assert final.state is JobState.CANCELLED, (
            f"a cancellation accepted at the start boundary was resurrected to "
            f"{final.state}"
        )
        assert not any(src.endswith("media.wav") for src in ran), (
            "a job cancelled at its start boundary still entered the engine"
        )
    finally:
        executor.shutdown()


# ==========================================================================
# 7. Stale verdicts and unknown identity under an outage
# ==========================================================================


class _OutageStore(MemoryJobStore):
    """A memory store whose reads and writes can be forced down after a start.

    ``arm_next`` makes the next ``begin_attempt`` land and then take the store
    down for reads and writes, modelling an outage that begins exactly after the
    run is marked RUNNING: the run owns a real, positive attempt, and every later
    read - including any recovery read - fails.
    """

    def __init__(self) -> None:
        super().__init__()
        self.broken = False
        self.arm_next = False

    def get(self, job_id: str):
        if self.broken:
            raise RuntimeError("store read unavailable")
        return super().get(job_id)

    def update(self, job_id: str, **fields):
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().update(job_id, **fields)

    def update_owned(self, job_id: str, **fields):
        # The run's guarded progress/checkpoint writes are part of the write
        # path the outage takes down; leaving this through to the live store
        # would let the run keep writing while the test claims the store is down.
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().update_owned(job_id, **fields)

    def claim(self, job_id: str, **kwargs):
        if self.broken:
            raise RuntimeError("store write unavailable")
        return super().claim(job_id, **kwargs)

    def begin_attempt(self, job_id: str, **kwargs):
        row = super().begin_attempt(job_id, **kwargs)
        if self.arm_next:
            self.arm_next = False
            self.broken = True
        return row


def _raw_row(store: MemoryJobStore, job_id: str):
    """Read the live row straight from the dict, bypassing a broken ``get``."""
    return store._jobs[job_id]


def test_stale_terminal_verdict_does_not_overwrite_a_newer_generation(monkeypatch):
    """A stranded ERROR must not land on a row a newer cancel/retry moved on.

    The run starts (attempt N) and the outage hits after that, so its terminal
    ERROR write is stranded - pinned to the real owned attempt. While the store
    is down the row is cancelled and then reopened by a retry, so it now belongs
    to generation N+1. On recovery the reconciliation's guarded write must refuse:
    the newer owner's row is left exactly as it is. This is the bounded
    newer-cancel/retry outage control.
    """
    store = _OutageStore()
    ex = JobExecutor(store, max_concurrency=1)

    def crash_once(source, **kwargs):
        if source == "broken":
            raise RuntimeError("pipeline exploded mid-run")
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", crash_once)

    try:
        store.arm_next = True
        broken = ex.submit(source="broken")

        # Fault latched while the store is down (reads and writes both).
        assert _wait_for(lambda: ex.store_failed is not None), "fault was not recorded"
        assert all(w.is_alive() for w in ex._workers), "worker died on write fault"

        stranded = _raw_row(store, broken.id)
        owned_attempt = stranded.attempt
        assert owned_attempt > 0, "precondition: the run started"
        assert stranded.state is JobState.RUNNING, "precondition: row left RUNNING"

        # A newer generation: the stranded row is cancelled and then reopened by
        # a retry while the store is still down. Modelled on the live row (a real
        # cancel+claim could not be persisted through the outage); the point is
        # the reconciliation's ownership guard, not the path that moved it.
        newer_attempt = owned_attempt + 2
        stranded.state = JobState.PENDING
        stranded.attempt = newer_attempt
        stranded.cancel_requested = False
        stranded.progress = "reopened-by-newer-owner"

        # Recover. The bounded reconciliation spends one guarded write; because
        # the row's attempt no longer matches, it refuses and the newer row is
        # untouched.
        store.broken = False
        healthy = ex.submit(source="healthy")
        assert _wait_for(
            lambda: store.get(healthy.id).state is JobState.DONE
        ), "healthy work stranded after recovery"

        # Give any (wrong) clobbering write a bounded window to land.
        time.sleep(0.3)
        row = store.get(broken.id)
        assert row.attempt == newer_attempt, "precondition: newer owner advanced the row"
        assert row.state is not JobState.ERROR, (
            "a stale terminal verdict clobbered a newer generation"
        )
        assert row.progress == "reopened-by-newer-owner", (
            "a stale terminal verdict overwrote a newer owner's progress"
        )
    finally:
        store.broken = False
        ex.shutdown()
        assert not any(w.is_alive() for w in ex._workers)


# ==========================================================================
# 8. RED: run-owned progress and checkpoint writes are not guarded
# ==========================================================================
#
# The terminal write is owned (``finalize_done``/``claim`` pinned to the run's
# attempt), but two *other* run-owned writes are not:
#
# - ``run_job`` wrote ``progress="starting"`` with an unguarded ``store.update``
#   and ``_progress`` re-read the latest row and then unconditionally
#   ``update``d - a read-then-write, not a guarded transition. Between the read
#   and the write a newer generation (a cancel + resume claim, which advances the
#   attempt) can take the row, and the stale write lands on it.
# - ``on_checkpoint`` called ``write_checkpoint``, whose three-argument form is an
#   unguarded ``store.update(job_id, checkpoint=payload)``. A checkpoint produced
#   by an old attempt can therefore overwrite a newer attempt's checkpoint, or
#   contaminate a row that is no longer this run's.
#
# The product now routes both through the store's guarded ``update_owned``, pinned
# to the run's owned generation. The tests below arrange each stale write at that
# seam and assert the newer/terminal row is left exactly as it was. They remain
# RED-first in intent: the assertions fail against the unguarded baseline and pass
# once the guarded write refuses (pinned to the run's owned generation, and - for
# progress - to a non-terminal, same-generation, uncancelled row).


# The arrangement used by each test: wrap the store method the run's write goes
# through (``update_owned``) so that, immediately *before* that write is
# attempted, the row is moved to a newer generation (or an acceptance is
# recorded). That is the exact instant the old unguarded progress write and the
# unguarded ``write_checkpoint`` left open, made deterministic: the hook fires
# synchronously on the named write, so the race does not depend on thread
# scheduling. Tests hold the run after the write under test (an event gate) so
# the terminal finalize cannot mask the value it asserts, and use only a bounded
# ``time.sleep`` window on top of that.


class _MoveOnWriteStore(MemoryJobStore):
    """A store that moves the row to a newer owner just before a named write.

    ``on_next_write(hook, *, match=...)`` installs a one-shot hook that runs
    immediately *before* the matching write is applied. ``match`` selects which
    write fires it (the runner writes more than one progress value: ``starting``
    before the pipeline, then a stage name from ``update_owned``), so a test
    names the write it means. The hook is handed the *row object that is about
    to be written* and mutates it in place; each store persists exactly that
    object, so the stale write under test really does meet a row that has already
    moved on. Handing the row in (rather than having the hook fetch its own) is
    deliberate: on SQLite a fresh ``get`` returns a detached copy, so a hook that
    fetched its own row would mutate a throwaway and never move the stored one.
    """

    def __init__(self) -> None:
        super().__init__()
        self._hook = None
        self._match = None
        self.moved = False

    def on_next_write(self, hook, *, match=None) -> None:
        self._hook = hook
        self._match = match

    def _move_row(self, job_id: str) -> None:
        """Apply the hook's move. Memory hands out the live row: mutate it."""
        row = self._jobs.get(job_id)
        if row is not None:
            self._hook(row)

    def _maybe_move(self, job_id: str, fields: dict) -> None:
        if self._hook is None or self.moved:
            return
        if self._match is not None and not self._match(fields):
            return
        self.moved = True
        self._move_row(job_id)

    def update_owned(self, job_id: str, **fields):
        self._maybe_move(job_id, fields)
        return super().update_owned(job_id, **fields)


class _MoveOnWriteSqlite(SqliteJobStore):
    """The durable twin of ``_MoveOnWriteStore`` (same hook, same seam).

    ``get`` on SQLite returns a *detached* row, so the hook cannot mutate the
    stored row through a fresh read. ``_move_row`` therefore reads the row once,
    hands *that* object to the hook, and persists the mutated row with a direct
    UPDATE - so the stale write under test meets a row that really moved on.
    """

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path / "jobs.db")
        self._hook = None
        self._match = None
        self.moved = False

    def on_next_write(self, hook, *, match=None) -> None:
        self._hook = hook
        self._match = match

    def _move_row(self, job_id: str) -> None:
        # Read the row once and hand it to the hook; the hook mutates *this*
        # object, which is then persisted. A second `get` inside the hook would
        # return a different detached copy and the mutation would be lost.
        row = SqliteJobStore.get(self, job_id)
        if row is None:
            return
        self._hook(row)
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET attempt = ?, state = ?, progress = ?,"
                " cancel_requested = ?, checkpoint = ? WHERE id = ?",
                (
                    row.attempt,
                    row.state.value,
                    row.progress,
                    int(row.cancel_requested),
                    json.dumps(row.checkpoint) if row.checkpoint is not None else None,
                    job_id,
                ),
            )
            self._conn.commit()

    def _maybe_move(self, job_id: str, fields: dict) -> None:
        if self._hook is None or self.moved:
            return
        if self._match is not None and not self._match(fields):
            return
        self.moved = True
        self._move_row(job_id)

    def update_owned(self, job_id: str, **fields):
        self._maybe_move(job_id, fields)
        return super().update_owned(job_id, **fields)


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_stale_progress_write_cannot_land_on_a_newer_attempt(monkeypatch, tmp_path, kind):
    """A run's stage progress from the owned attempt must not overwrite a newer one.

    ``_progress`` reads the latest row and then unconditionally ``update``s -
    the read and the write are two steps. Arrange a newer owner to take the row
    in that window: the progress write must be refused, so the newer generation's
    row (its attempt, state, and progress) is exactly as it was.

    RED: the unguarded ``store.update(job_id, progress=stage)`` lands on the
    newer row. GREEN: the write is pinned to the run's owned attempt and a
    non-terminal, same-generation row, so it refuses.
    """
    store = _MoveOnWriteStore() if kind == "memory" else _MoveOnWriteSqlite(tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    newer_progress = "reopened-by-newer-owner"
    stage_fired = threading.Event()
    hold = threading.Event()

    def fake_transcribe(source, *, check_cancel=None, on_stage=None, **kwargs):
        if on_stage is not None:
            on_stage("fetching")
            stage_fired.set()
        # Hold the run after the stage write so the terminal finalize cannot run
        # and mask the value under test.
        assert hold.wait(timeout=WAIT), "hold never released"
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    # Install the hook *before* submit: the worker may reach the stage write
    # before this thread resumes, so arming afterwards would race.
    def move(row):
        row.attempt += 1  # the newer owner's generation
        row.state = JobState.RUNNING
        row.progress = newer_progress
        row.cancel_requested = False

    # Fire on the *stage* write, not the earlier ``starting`` write.
    store.on_next_write(move, match=lambda f: f.get("progress") == "fetching")

    try:
        job = executor.submit(source=str(media))
        assert stage_fired.wait(timeout=WAIT), "the pipeline stage callback never ran"
        assert _wait_for(lambda: store.moved), "the newer-owner hook never fired"

        # Give a stale write a bounded window to land while the run is held.
        time.sleep(0.3)
        row = store.get(job.id)
        assert row.attempt == 2, "precondition: the newer owner advanced the row"
        assert row.progress == newer_progress, (
            "a run's stale stage progress overwrote a newer generation's progress "
            f"(got {row.progress!r})"
        )
    finally:
        hold.set()
        executor.shutdown()
        store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_stale_checkpoint_write_cannot_land_on_a_newer_attempt(monkeypatch, tmp_path, kind):
    """A checkpoint from the owned attempt must not overwrite a newer generation's.

    ``on_checkpoint`` calls the unguarded ``write_checkpoint``. Arrange a newer
    owner (with its own checkpoint) to take the row just before the run's
    checkpoint write: the checkpoint must be refused, leaving the newer owner's
    checkpoint and progress intact.

    RED: the unguarded ``store.update(job_id, checkpoint=payload)`` lands on the
    newer row. GREEN: the write is pinned to the run's owned attempt and a
    non-terminal row, so it refuses.
    """
    store = _MoveOnWriteStore() if kind == "memory" else _MoveOnWriteSqlite(tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    newer_checkpoint = {
        "version": 1,
        "source": "newer-owner",
        "model": "small",
        "finished_stages": ["transcribe"],
        "transcript": {"segments": []},
    }

    checkpoint_written = threading.Event()
    hold = threading.Event()

    def fake_transcribe(source, *, check_cancel=None, on_checkpoint=None, **kwargs):
        if on_checkpoint is not None:
            on_checkpoint(
                {
                    "version": 1,
                    "source": source,
                    "model": "small",
                    "finished_stages": ["source"],
                    "transcript": {"stale": True},
                }
            )
            checkpoint_written.set()
        # Hold the run after its checkpoint write so the terminal finalize (which
        # also touches the checkpoint field on the DONE branch) cannot run and
        # mask the value under test.
        assert hold.wait(timeout=WAIT), "hold never released"
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    # Install the hook *before* submit: the worker may reach the checkpoint write
    # before this thread resumes, so arming afterwards would race.
    def move(row):
        row.attempt += 1
        row.state = JobState.RUNNING
        row.progress = "reopened-by-newer-owner"
        row.cancel_requested = False
        row.checkpoint = dict(newer_checkpoint)

    store.on_next_write(move, match=lambda f: "checkpoint" in f)

    try:
        job = executor.submit(source=str(media))
        assert checkpoint_written.wait(timeout=WAIT), "checkpoint write never ran"
        assert _wait_for(lambda: store.moved), "the newer-owner hook never fired"

        time.sleep(0.3)
        row = store.get(job.id)
        assert row.attempt == 2, "precondition: the newer owner advanced the row"
        assert row.checkpoint == newer_checkpoint, (
            "a run's stale checkpoint overwrote a newer generation's checkpoint "
            f"(got {row.checkpoint!r})"
        )
    finally:
        hold.set()
        executor.shutdown()
        store.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_accepted_cancellation_progress_is_not_overwritten_by_a_stage(
    monkeypatch, tmp_path, kind
):
    """Once acceptance is recorded, a later stage must not replace ``cancelling``.

    ``_progress`` guards the cancelling label by *reading* the row first, but the
    read-then-write is not atomic: an acceptance landing between the two is lost.
    Arrange the acceptance (the same transition ``accept_cancel`` performs on a
    RUNNING row: flag set, progress ``cancelling``, still RUNNING) to land just
    before the run's stage write. The write must refuse, so the accepted label
    survives.

    RED: the stage ``update`` replaces the accepted ``cancelling`` with a stage
    name. GREEN: the write is pinned to the owned attempt and refuses a row that
    carries the accepted flag.
    """
    store = _MoveOnWriteStore() if kind == "memory" else _MoveOnWriteSqlite(tmp_path)
    executor = _executor(store)
    media = tmp_path / "media.wav"
    _wav(media)

    stage_fired = threading.Event()
    hold = threading.Event()

    def fake_transcribe(source, *, check_cancel=None, on_stage=None, **kwargs):
        if on_stage is not None:
            on_stage("transcribing")
            stage_fired.set()
        # Hold the run *after* the stage write, so the terminal finalize cannot
        # mask a clobbered label by landing CANCELLED (whose progress is also
        # honest). The value under test is the one on the row right now.
        assert hold.wait(timeout=WAIT), "hold never released"
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)

    # Install the acceptance hook *before* submit: the worker may reach the stage
    # write before this thread resumes, so arming afterwards would race.
    def accept(row):
        row.state = JobState.RUNNING
        row.progress = "cancelling"
        row.cancel_requested = True  # same attempt; only the flag/label change

    store.on_next_write(accept, match=lambda f: f.get("progress") == "transcribing")

    try:
        job = executor.submit(source=str(media))
        assert stage_fired.wait(timeout=WAIT), "the pipeline stage callback never ran"
        assert _wait_for(lambda: store.moved), "the acceptance hook never fired"

        # Give a stale stage write a bounded window to land while the run is held.
        time.sleep(0.3)
        row = store.get(job.id)
        assert row.state is JobState.RUNNING, "precondition: the run is still live"
        assert row.cancel_requested is True, "the accepted cancellation was lost"
        assert row.progress == "cancelling", (
            "an accepted cancellation's progress was overwritten by a stage label "
            f"(got {row.progress!r}, cancel_requested={row.cancel_requested})"
        )
    finally:
        hold.set()
        executor.shutdown()
        store.close()


# ==========================================================================
# 9. The returned start identity must be the one the atomic write made
# ==========================================================================
#
# ``begin_attempt`` performs its guarded UPDATE (RUNNING, attempt + 1) in one
# statement and returns a row. For the returned ``attempt`` to be the execution
# identity the run can pin later decisions to, it must be the value that atomic
# write produced, and the rest of the returned row must be that same instant's
# worth. ``MemoryJobStore`` mutates the row under its lock and returns a
# *detached copy* of it, so the caller's snapshot cannot be mutated afterwards.
# ``SqliteJobStore`` performs the UPDATE and returns the *written* row - through
# ``UPDATE ... RETURNING *`` on a modern runtime, or a read taken inside the same
# open transaction (pre-commit) where RETURNING is unavailable. It no longer
# commits, releases the lock, and re-reads: that shape let a concurrent writer
# advance the row (or rewrite its checkpoint/request) between the commit and the
# re-read, and the caller pinned a snapshot it never wrote.


class _NoGetSqlite(SqliteJobStore):
    """A SQLite store that records every ``get`` a test's store makes.

    The coherent-return fix must return the row the guarded write produced, not
    a later read. The tests clear ``get_calls`` immediately before the call under
    test and then assert it stayed empty, so a non-empty list would be direct
    proof ``begin_attempt`` obtained its snapshot by re-reading the row after the
    write.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.get_calls: list[str] = []

    def get(self, job_id: str):
        self.get_calls.append(job_id)
        return super().get(job_id)


def test_begin_attempt_returns_the_attempt_its_atomic_write_produced(tmp_path):
    """The returned identity must be the attempt the atomic write wrote.

    After the start lands (attempt 1), a second connection advances the row to a
    newer generation. The returned ``attempt`` must still be the attempt the
    guarded start produced, not the newer one a post-write read would have seen -
    otherwise the run pins its terminal decision to a generation it does not own.
    """
    store = _NoGetSqlite(str(tmp_path / "jobs.db"))
    try:
        job = store.create("media.wav")
        assert job.attempt == 0
        store.get_calls.clear()  # ignore reads before the start under test

        started = store.begin_attempt(job.id, observed_attempt=0)
        assert started is not None, "the start was refused"
        assert started.state is JobState.RUNNING
        # The store did not obtain its snapshot by re-reading the row.
        assert store.get_calls == [], (
            f"begin_attempt fell back to a post-write get: {store.get_calls!r}"
        )

        # A competing writer moves the row on *after* the start committed.
        other = SqliteJobStore(store.path)
        try:
            other.update(job.id, attempt=99)
        finally:
            other.close()

        # The atomic write set attempt = 0 + 1 = 1. The returned identity must be
        # that value, not the newer one the competing writer produced.
        assert started.attempt == 1, (
            "begin_attempt returned a later generation than its own atomic write "
            f"(returned attempt {started.attempt}, wrote 1)"
        )
        # The row really did move on, so the value above is the written one and
        # not merely a coincidence of the row standing still.
        assert store.get(job.id).attempt == 99
    finally:
        store.close()


def test_begin_attempt_returned_row_is_the_complete_write_snapshot(tmp_path):
    """The whole returned row is the write's snapshot, not a later read's.

    A second connection rewrites the row's checkpoint and request - fields the
    start does not touch - after the start commits. The returned snapshot must
    carry the values the row held *at the write*, so a caller reading
    ``checkpoint``/``request`` off it is reading the same instant as ``attempt``.
    """
    store = _NoGetSqlite(str(tmp_path / "jobs.db"))
    try:
        job = store.create("media.wav", request={"request": "original"})
        store.update(job.id, checkpoint={"checkpoint": "original"})
        store.get_calls.clear()  # ignore reads before the start under test

        started = store.begin_attempt(job.id)
        assert started is not None
        assert store.get_calls == [], (
            f"begin_attempt fell back to a post-write get: {store.get_calls!r}"
        )

        other = SqliteJobStore(store.path)
        try:
            other.update(
                job.id,
                attempt=99,
                checkpoint={"checkpoint": "later-owner"},
                request={"request": "later-owner"},
            )
        finally:
            other.close()

        assert started.attempt == 1, "identity is not the written generation"
        assert started.state is JobState.RUNNING
        assert started.cancel_requested is False
        assert started.checkpoint == {"checkpoint": "original"}, (
            "the returned snapshot carried a later writer's checkpoint"
        )
        assert started.request == {"request": "original"}, (
            "the returned snapshot carried a later writer's request"
        )
        # The row itself now holds the later writer's values, so the assertions
        # above are about the write's snapshot and not a still row.
        current = store.get(job.id)
        assert current.checkpoint == {"checkpoint": "later-owner"}
        assert current.attempt == 99
    finally:
        store.close()


def test_begin_attempt_returned_identity_is_coherent_with_the_row_it_wrote(tmp_path):
    """The returned row must describe the same instant the atomic write landed.

    The run pins its terminal write to the returned attempt. If the returned row
    is a later generation, that terminal write is refused (or worse, mis-pinned).
    This asserts the returned ``(state, attempt, cancel_requested)`` triple is a
    coherent snapshot of the start the store actually made.
    """
    store = _NoGetSqlite(str(tmp_path / "jobs.db"))
    try:
        job = store.create("media.wav")
        started = store.begin_attempt(job.id)
        assert started is not None
        # What begin_attempt writes: RUNNING, uncancelled, attempt advanced by 1.
        assert started.state is JobState.RUNNING
        assert started.cancel_requested is False
        assert started.attempt == 1, (
            f"returned identity {started.attempt} is not the written generation 1"
        )

        # The identity it returned must be usable as an ownership pin: a guarded
        # terminal write keyed to it resolves the row this run started.
        landed = store.claim(
            job.id,
            allowed_states={JobState.RUNNING},
            observed_attempt=started.attempt,
            state=JobState.ERROR,
            progress="failed",
        )
        assert landed is not None, (
            "a pin to the written generation failed to resolve the row it wrote"
        )
        assert landed.attempt == started.attempt
    finally:
        store.close()


def test_begin_attempt_memory_snapshot_is_detached_from_later_writes():
    """The memory store's returned snapshot must not alias the live row.

    ``MemoryJobStore`` hands out the live row from ``get``; its ``begin_attempt``
    must return a detached copy instead, so a later write by another owner cannot
    mutate the identity this run pins its terminal decision to.
    """
    store = MemoryJobStore()
    job = store.create("media.wav", request={"request": "original"})
    store.update(job.id, checkpoint={"checkpoint": "original"})

    started = store.begin_attempt(job.id)
    assert started is not None
    assert started.attempt == 1

    # A later owner rewrites the row in place (memory `claim`/`update` mutate the
    # live object).
    newer = store.claim(
        job.id,
        allowed_states={JobState.RUNNING},
        observed_attempt=started.attempt,
        advance_attempt=True,
        checkpoint={"checkpoint": "later-owner"},
        request={"request": "later-owner"},
    )
    assert newer is not None

    # The returned snapshot is frozen at the start, not aliased to the live row.
    assert started.attempt == 1, "the returned snapshot tracked a later attempt"
    assert started.checkpoint == {"checkpoint": "original"}, (
        "the returned snapshot aliased a later writer's checkpoint"
    )
    assert started.request == {"request": "original"}, (
        "the returned snapshot aliased a later writer's request"
    )
    live = store.get(job.id)
    assert live.checkpoint == {"checkpoint": "later-owner"}
    assert live.attempt == 2


# ==========================================================================
# 10. A refused cancellation must not trip a newer owner's token
# ==========================================================================
#
# ``executor.cancel`` observes the row (identity + state) and then, under the
# executor lock, applies the guarded ``accept_cancel`` and trips the live token.
# The observation is taken *before* that lock, so the row can move on in between:
# a newer owner replaces the row and registers its own token. The guarded
# ``accept_cancel`` then refuses - it was formed against a generation that no
# longer owns the row - and the refusal must leave the newer owner alone.
# Tripping the token before asking would stop the newer owner with a
# cancellation it never accepted, so the token may only be tripped once the
# guarded accept has really landed. The arrangements below pin exactly that
# window: the row is moved on, and the newer owner's token installed, after the
# canceller holds its stale generation and before ``cancel`` takes its lock.


class _ObservedRaceStore:
    """Wraps a store so the cancellation race can be arranged deterministically.

    It passes every other call straight through; only the *second* ``observe``
    (the one ``executor.cancel`` itself makes, after the test has held the stale
    generation) is deferred until the test has released it. The executor's
    guarded ``accept_cancel`` therefore runs strictly after the replacement owner
    exists - not merely beside it.

    The deferred call captures the row *before* it signals and waits, and returns
    that captured snapshot after the wait. Reading the store only afterwards would
    hand ``cancel`` the *newer* generation's snapshot, which is legitimately
    accepted - hiding the race the test exists to pin. The observation is the
    detached stale row, so the guarded accept refuses it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._observes = 0
        self.observed = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def observe(self, job_id: str):
        self._observes += 1
        if self._observes == 2:
            observation = self._inner.observe(job_id)
            self.observed.set()
            assert self.release.wait(timeout=WAIT), "the observation was never released"
            return observation
        return self._inner.observe(job_id)


class _FakeToken:
    """A cancel token stand-in the newer owner registers in the executor.

    ``cancel`` reaches the token through the executor's own registry, so the
    test's replacement owner can place this object there and observe afterwards
    whether a cancellation it never accepted tripped it.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_refused_cancel_does_not_trip_a_newer_owners_token(tmp_path, kind):
    """``executor.cancel`` may trip a token only for an accepted cancellation.

    The canceller pins a cancellation to attempt N and then loses the race: a
    newer owner (attempt N+1) replaces the row and registers its own token in the
    executor. The guarded ``accept_cancel`` refuses, so ``cancel`` must return
    False and the newer token must still be untripped.

    Baseline (token tripped before the accept): the newer owner's token was
    cancelled even though the acceptance was refused - a stale cancellation
    stopping the run that actually owned the row.
    """
    inner = _store(kind, tmp_path)
    race = _ObservedRaceStore(inner)
    executor = _executor(race)
    try:
        job = inner.create("media.wav")

        # The generation the canceller will pin to: observed first, before the
        # executor's own observation.
        stale = race.observe(job.id)
        assert stale is not None and stale.attempt == 0

        newer_token = _FakeToken()
        newer_attempt: list[int] = []
        replaced: list[str] = []

        def install_newer_owner() -> None:
            # Runs while the executor's observation is held, before its lock:
            # advance the row (a resume claim's generation bump) and register the
            # newer owner's token exactly as the worker does.
            newer = inner.claim(
                job.id,
                allowed_states={JobState.RUNNING, JobState.PENDING},
                observed_attempt=stale.attempt,
                advance_attempt=True,
                state=JobState.RUNNING,
                progress="newer-owner",
            )
            assert newer is not None, "the replacement owner's claim was refused"
            newer_attempt.append(newer.attempt)
            replaced.append(newer.id)
            with executor._lock:
                executor._tokens[job.id] = newer_token

        accepted: list[bool] = []

        def run_cancel() -> None:
            accepted.append(executor.cancel(job.id))

        canceller = threading.Thread(target=run_cancel)
        canceller.start()
        try:
            assert race.observed.wait(timeout=WAIT), (
                "cancel never reached its observation of the row"
            )
            install_newer_owner()
            race.release.set()
        finally:
            race.release.set()
            canceller.join(timeout=WAIT)
        assert not canceller.is_alive(), "the cancellation never completed"

        assert replaced, "precondition: the newer owner was never installed"
        assert accepted == [False], (
            "a cancellation formed against a replaced generation was accepted"
        )
        assert newer_token.cancelled is False, (
            "a refused cancellation tripped the newer owner's token"
        )
        row = inner.get(job.id)
        assert row.attempt == newer_attempt[-1], "the newer owner's generation was moved"
        assert row.progress == "newer-owner"
        assert row.cancel_requested is False
    finally:
        race.release.set()
        executor.shutdown()
        inner.close()


@pytest.mark.parametrize("kind", STORE_KINDS)
def test_accepted_cancel_still_trips_the_owned_token(tmp_path, kind):
    """The complement: an accepted cancellation must still trip the live token.

    The fix moves the trip inside the acceptance, so this pins that the trip is
    not simply lost: a cancellation that really lands (nothing moved on) must
    return True and leave the owner's token tripped.
    """
    store = _store(kind, tmp_path)
    executor = _executor(store)
    try:
        job = store.create("media.wav")
        started = store.begin_attempt(job.id)
        assert started is not None, "precondition: the run owns the row"
        token = _FakeToken()
        with executor._lock:
            executor._tokens[job.id] = token

        assert executor.cancel(job.id) is True, "a live cancellation was refused"
        assert token.cancelled is True, (
            "an accepted cancellation did not trip the owner's token"
        )
        row = store.get(job.id)
        assert row.cancel_requested is True
        assert row.progress == "cancelling"
        assert row.state is JobState.RUNNING, (
            "the accepted cancellation must not claim a terminal stop mid-call"
        )
    finally:
        executor.shutdown()
        store.close()


def test_unknown_owned_identity_stays_unresolved_without_adopting_new_owner():
    """An unknown execution identity must not be back-filled from the current row.

    When no acquisition identity was captured and the start never landed, the run
    does not know which generation it owns. Reading the row's *current* attempt
    and pinning to that is forbidden: the outage may have spanned a cancellation
    and a retry, so the current owner is not necessarily the stranded one. The
    reconciliation must leave the entry outstanding - admission keeps refusing and
    ``store_failed`` keeps reporting the fault - rather than adopt the new owner.
    """
    store = MemoryJobStore()
    ex = JobExecutor(store, max_concurrency=1)
    try:
        job = store.create("media.wav")
        # A terminal write whose execution identity was never captured (`None`),
        # exactly as the worker records it when neither the start nor an
        # acquisition capture produced an attempt.
        ex._record_store_failure(
            RuntimeError("store write unavailable"),
            pending=TerminalWrite(
                job_id=job.id,
                attempt=None,
                fields={"state": JobState.ERROR, "error": "boom", "progress": "failed"},
            ),
        )

        # A newer owner takes the row on (a cancel + retry advanced it).
        reclaimed = store.claim(
            job.id,
            allowed_states={JobState.PENDING},
            advance_attempt=True,
            state=JobState.PENDING,
            progress="reopened-by-newer-owner",
        )
        assert reclaimed is not None
        newer_attempt = reclaimed.attempt

        # One bounded reconciliation must not adopt the newer generation.
        drained = ex._reconcile_pending_terminal()
        assert drained is False, "an unknown identity was treated as resolved"
        row = store.get(job.id)
        assert row.state is not JobState.ERROR, "an unknown identity clobbered a newer owner"
        assert row.progress == "reopened-by-newer-owner"
        assert row.attempt == newer_attempt
        assert ex.store_failed is not None, "the fault latch was cleared on a guess"
    finally:
        ex.shutdown()
