"""Job execution.

`run_job` executes one job synchronously and owns its state transitions.
`submit` is the entry point callers use; it delegates to the process-wide
`JobExecutor`, so jobs get bounded concurrency and can be cancelled.

Cancellation contract:

- `JobCancelled` is an orderly stop, not a failure. The job ends CANCELLED.
- A job that finishes while a cancellation is in flight must not overwrite the
  cancelled state with DONE, so the final transition re-reads the job first.
- Explicit resume of an interrupted job is the one path allowed past the
  terminal-state guard. Callers use `prepare_resume` first, which resets the job
  to PENDING while preserving its checkpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from textflowkit.core.checkpoint import write_checkpoint
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
    model: str = "small",
    engine: str = "whisper",
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
) -> None:
    """Execute a job, recording its terminal state. Callers decide the thread."""
    # Do not start work that has already been cancelled or otherwise finished.
    # `submit` only hands us fresh jobs; explicit resume prepares the row first.
    current = store.get(job.id)
    if current is not None and current.is_terminal:
        return

    store.update(job.id, state=JobState.RUNNING, progress="starting")

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
            on_checkpoint=lambda record: write_checkpoint(store, job.id, record),
        )
    except JobCancelled:
        store.update(job.id, state=JobState.CANCELLED, progress="cancelled")
        return
    except PipelineError as exc:
        store.update(job.id, state=JobState.ERROR, error=str(exc), progress="failed")
        return
    # Last-resort guard: a job must never be left stuck in RUNNING because of an
    # unexpected exception type. The error is recorded on the job, not swallowed.
    # Narrower catches above handle the expected failure modes.
    except Exception as exc:  # noqa: BLE001
        store.update(
            job.id,
            state=JobState.ERROR,
            error=f"{type(exc).__name__}: {exc}",
            progress="failed",
        )
        return

    # A cancellation that arrived while the last stage ran must win over DONE.
    latest = store.get(job.id)
    if latest is not None and latest.state is JobState.CANCELLED:
        return

    store.update(
        job.id,
        state=JobState.DONE,
        progress="complete",
        transcript=result.transcript.to_dict(),
        outputs=[str(p) for p in result.outputs],
    )


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
    """
    if not background:
        job = store.create(source)
        run_job(job, store, source=source, **kwargs)
        return job

    executor = get_default_executor()
    if executor.store is not store:
        # A caller passed a specific store; run it directly rather than silently
        # routing the job to a different store than the one they hold.
        job = store.create(source)
        run_job(job, store, source=source, **kwargs)
        return job
    return executor.submit(source=source, **kwargs)


def transcript_for(job: Job) -> Transcript | None:
    if job.transcript is None:
        return None
    return Transcript.from_dict(job.transcript)
