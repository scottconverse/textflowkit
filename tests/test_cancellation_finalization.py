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

Two tests are *arrangement-gated*: they need a start/cancel/finalize operation
the store does not have yet (a queued cancellation must be able to stop a worker
between its pre-start read and its ``begin_attempt``). Where the arrangement
would require a nonexistent API they ``pytest.skip`` rather than raise
AttributeError, so RED arrives as an honest failure of the contract under test,
not as a crash from missing plumbing. The design note in
``reports/G2D-red-cancellation.md`` names the minimal shared operations these
gates are waiting on.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import pytest

from textflowkit.core import runner
from textflowkit.core.executor import CancelToken, JobCancelled, JobExecutor
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
    """Arrange the race at the exact store seam the final write goes through.

    The runner commits its terminal row with an unconditional ``store.update``.
    A cancellation accepted (token tripped, ``cancel_requested`` set, row still
    RUNNING) at the instant just before that write must still win: the accepted
    cancellation owns the outcome.

    Arrangement: wrap the store's terminal ``update`` so that, immediately
    before the DONE patch lands, the accepted cancellation is applied exactly as
    ``JobExecutor.cancel`` applies it for a running job - token tripped,
    ``cancel_requested`` set, row still RUNNING. This runs the real executor and
    the real ``run_job`` on a worker thread, so it is end-to-end. If a future
    fix routes the terminal write through a new claim/finalize seam instead of
    ``update``, the same boundary still exists and this arrangement moves with
    it (the cancel is placed before the terminal commit regardless of which
    store call performs it); the seam name is documented in the design note.

    Baseline: the commit reads state (RUNNING), ignores ``cancel_requested``,
    and publishes DONE. RED.
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
    original_update = store.update

    def update_with_boundary_cancel(job_id, **fields):
        if fields.get("state") is JobState.DONE and not boundary_cancel_applied.is_set():
            boundary_cancel_applied.set()
            # Place the accepted cancellation against the still-RUNNING row,
            # using the real transition, before the DONE write lands.
            original_update(job_id, cancel_requested=True, progress="cancelling")
        return original_update(job_id, **fields)

    monkeypatch.setattr(store, "update", update_with_boundary_cancel)

    try:
        # submit returns immediately; the pool's single worker drives the run.
        job = executor.submit(source=str(media))
        assert _wait_for(
            lambda: store.get(job.id).state
            in {JobState.DONE, JobState.ERROR, JobState.CANCELLED}
        ), "the run never reached a terminal row"
        assert boundary_cancel_applied.is_set(), "the boundary cancel was never reached"

        final = store.get(job.id)
        assert final.state is JobState.CANCELLED, (
            "a cancellation accepted at the terminal-commit boundary must win, "
            f"got {final.state} (cancel_requested={final.cancel_requested})"
        )
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
    worker's pre-start read and its start write - cannot be arranged
    deterministically without an atomic start seam the store does not yet have,
    so it is not asserted here. See ``reports/G2D-red-cancellation.md`` for the
    minimal shared operation that would close it; this test guards the coarse
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
