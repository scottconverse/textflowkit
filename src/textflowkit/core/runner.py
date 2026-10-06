"""Job execution.

`run_job` executes one job synchronously and owns its state transitions.
`submit` is the entry point callers use; it delegates to the process-wide
`JobExecutor`, so jobs get bounded concurrency and can be cancelled.

Cancellation contract:

- `JobCancelled` is an orderly stop, not a failure. The job ends CANCELLED.
- A job that finishes while an accepted cancellation is in flight must not
  publish DONE. The terminal row is chosen by one atomic store operation
  (`finalize_done`) against the generation this run owns: CANCELLED when that
  generation carries the accepted flag, DONE otherwise. Reading the flag and
  writing the state cannot be split, so the "DONE with cancel_requested: true"
  race cannot occur.
- The start is guarded too: a run begins only from a still-PENDING, uncancelled
  row at the acquired generation, so a queued cancellation cannot be resurrected
  into RUNNING by the worker that later picks the job up.
- Every *other* run-owned write is guarded the same way. Progress and checkpoint
  writes go through `update_owned`, pinned to the generation the run started, in
  one store operation - so a resume claim or an accepted cancellation that lands
  first is never overwritten by a stale stage label or an old attempt's
  checkpoint. Progress additionally refuses a row carrying an accepted
  cancellation, so `cancelling` survives the next stage; the checkpoint is
  allowed to land on a cancelling row, because that work is the resume material.
- Explicit resume of an interrupted job is the one path allowed past the
  terminal-state guard. Callers use `prepare_resume` first, which resets the job
  to PENDING while preserving its checkpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from textflowkit.core.checkpoint import metadata_only_checkpoint, write_checkpoint
from textflowkit.core.engine import DEFAULT_ENGINE
from textflowkit.core.executor import JobCancelled, get_default_executor
from textflowkit.core.jobs import Job, JobState, JobStore
from textflowkit.core.model import Transcript
from textflowkit.core.pipeline import PipelineError, transcribe


def run_job(
    job: Job,
    store: JobStore,
    *,
    source: str,
    language: str | None = None,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    model: str | None = None,
    engine: str = DEFAULT_ENGINE,
    device: str | None = None,
    cookies_from_browser: str | None = None,
    keep_media: bool = False,
    work_dir: str | Path | None = None,
    check_cancel: Callable[[], None] | None = None,
    input_root: str | Path | None = None,
    diarize: bool = False,
    diarizer_backend: str = "pyannote",
    translate_to: str | None = None,
    translator_backend: str = "ollama",
    resume_checkpoint: dict[str, Any] | None = None,
    on_started: Callable[[int], None] | None = None,
    expected_attempt: int | None = None,
    notify: Callable[[str], None] | None = None,
) -> None:
    """Execute a job, recording its terminal state. Callers decide the thread.

    ``expected_attempt`` is the immutable acquisition identity the caller queued
    this run under: the attempt the row carried when it was created or claimed
    (`submit`/`enqueue`), captured *before* the worker read the store. The start
    is pinned to it, so a stale queue entry whose id was cancelled and then
    resumed cannot start the newer generation. ``None`` means the caller has no
    acquisition identity to pin (an inline direct call), which keeps the
    state-and-flag guard alone.

    ``on_started`` is called once with the exact attempt this run owns, the
    instant the row is marked RUNNING. The executor uses it to pin the run's
    execution identity *before* any pipeline fault can strike, so a later error
    (or a store outage that also breaks reads) does not force it to re-derive the
    identity from a possibly-broken store - the identity-loss failure. Optional:
    an inline caller with no recovery concern passes nothing.

    ``notify`` is an optional, display-only stage observer. It is called with the
    name of a stage the instant that stage *starts*, so a CLI can show in-flight
    feedback; every other surface passes nothing and keeps its silence. It never
    replaces the stored ``progress`` value `_progress` owns - the store write and
    the notice are two sinks for one fact - and the call is made *after* the
    guarded progress write, so a notice is only ever emitted for a stage this run
    still owns. A run that has already been cancelled, or that a newer attempt has
    taken over, writes nothing and therefore announces nothing: the display
    cannot tell an operator about work that did not happen.
    """
    # Do not start work that has already been cancelled or otherwise finished.
    # `submit` only hands us fresh jobs; explicit resume prepares the row first.
    current = store.get(job.id)
    if current is not None and current.is_terminal:
        return

    # Starting a run marks the row RUNNING and bumps its attempt in one *guarded*
    # operation. The guard refuses a row that is no longer PENDING (a queued
    # cancellation already landed CANCELLED), already carries an accepted
    # cancellation, or is no longer the acquired generation. A refusal means this
    # run does not own the row and must not enter the engine - returning here is
    # what stops a cancelled queued job from being resurrected into RUNNING.
    #
    # The identity the run owns is reported the instant it is known, *before* any
    # work, so a later fault never has to re-derive it from a possibly-broken
    # store. When the start does not land, the caller keeps the acquisition
    # identity it captured before queueing; if it captured none, recovery refuses
    # to guess rather than clearing a stranded row on a no-match.
    started = store.begin_attempt(job.id, observed_attempt=expected_attempt)
    if started is None:
        return
    owned = started.attempt
    if on_started is not None:
        on_started(owned)
    # Guarded like every other run-owned write: pinned to the generation this run
    # started, so an accepted cancellation that landed in the same instant keeps
    # its `cancelling` label instead of being overwritten by `starting`.
    store.update_owned(job.id, observed_attempt=owned, progress="starting")

    def _progress(stage: str) -> None:
        """Publish the stage in flight so a polling client sees real progress.

        One guarded write, not a read-then-write: the store refuses a row that is
        no longer this run's generation, has reached a terminal state, or carries
        an accepted cancellation. That last case is the point - once a
        cancellation is accepted the job is `cancelling`, not whichever stage the
        worker is about to start, and a write that tested the row first could
        still lose the acceptance that landed between the test and the write.
        """
        if store.update_owned(job.id, observed_attempt=owned, progress=stage) is None:
            # The guarded write refused: the row is terminal, carries an accepted
            # cancellation, or a newer attempt took it over. The stage is not this
            # run's to report, so no notice goes out either - a display must not
            # announce work the store has already stopped owning.
            return
        if notify is not None:
            notify(stage)

    try:
        result = transcribe(
            source,
            language=language,
            formats=formats,
            output_dir=output_dir,
            model=model,
            engine=engine,
            device=device,
            cookies_from_browser=cookies_from_browser,
            keep_media=keep_media,
            work_dir=work_dir,
            check_cancel=check_cancel,
            input_root=input_root,
            diarize=diarize,
            diarizer_backend=diarizer_backend,
            translate_to=translate_to,
            translator_backend=translator_backend,
            resume_checkpoint=resume_checkpoint,
            on_checkpoint=lambda record: write_checkpoint(
                store, job.id, record, observed_attempt=owned
            ),
            on_stage=_progress,
            output_id=job.id,
        )
    except JobCancelled:
        # Guarded on the owned generation: a row a newer retry took over must not
        # be failed by this run's stale verdict.
        store.claim(
            job.id,
            allowed_states={JobState.PENDING, JobState.RUNNING},
            observed_attempt=owned,
            state=JobState.CANCELLED,
            progress="cancelled",
        )
        return
    except PipelineError as exc:
        store.claim(
            job.id,
            allowed_states={JobState.PENDING, JobState.RUNNING},
            observed_attempt=owned,
            state=JobState.ERROR,
            error=str(exc),
            progress="failed",
        )
        return
    # Last-resort guard: a job must never be left stuck in RUNNING because of an
    # unexpected exception type. The error is recorded on the job, not swallowed.
    # Narrower catches above handle the expected failure modes.
    except Exception as exc:  # noqa: BLE001
        store.claim(
            job.id,
            allowed_states={JobState.PENDING, JobState.RUNNING},
            observed_attempt=owned,
            state=JobState.ERROR,
            error=f"{type(exc).__name__}: {exc}",
            progress="failed",
        )
        return

    # The terminal row is chosen atomically against the generation this run owns:
    # CANCELLED if the same owned generation carries an accepted cancellation,
    # DONE otherwise. A row a newer retry moved on is left alone (None). This is
    # the single place the completion/cancellation race is decided, so there is no
    # window in which a finished-looking DONE row can carry an accepted cancel.
    latest = store.get(job.id)
    fields: dict[str, Any] = {
        "progress": "complete",
        "transcript": result.transcript.to_dict(),
        "outputs": [str(p) for p in result.outputs],
    }
    # The transcript is stored once, in the job's own field: the checkpoint that
    # carried it through the run keeps only the metadata a later request is
    # matched against. Both go in the same atomic finalize, so no reader can catch
    # the row holding neither copy. On the CANCELLED branch the checkpoint is left
    # with its full transcript - that is the resume work, and it is not deleted.
    metadata = metadata_only_checkpoint(
        latest.checkpoint if latest is not None else job.checkpoint
    )
    if metadata is not None:
        fields["checkpoint"] = metadata
    store.finalize_done(job.id, observed_attempt=owned, **fields)


def submit(
    store: JobStore,
    *,
    source: str,
    background: bool = True,
    **kwargs,
) -> Job:
    """Create a job and schedule it.

    `background=False` runs inline (used by tests and by callers that want a
    blocking call). Otherwise the job goes to the process-wide executor, which
    bounds concurrency and can cancel it.

    Both inline routes read the job back from the store afterwards: a durable
    store persists the terminal state and returns a freshly read Job, so the
    object `create` returned would still say PENDING. `MemoryJobStore.update`
    mutates in place, which is why returning that object looks right there.
    """
    if not background:
        job = store.create(source)
        run_job(job, store, source=source, **kwargs)
        return store.get(job.id) or job

    executor = get_default_executor()
    if executor.store is not store:
        # A caller passed a specific store; run it directly rather than silently
        # routing the job to a different store than the one they hold.
        job = store.create(source)
        run_job(job, store, source=source, **kwargs)
        return store.get(job.id) or job
    return executor.submit(source=source, **kwargs)


def transcript_for(job: Job) -> Transcript | None:
    if job.transcript is None:
        return None
    return Transcript.from_dict(job.transcript)
