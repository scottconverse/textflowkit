"""Background job execution for long-running transcription."""

from __future__ import annotations

import threading
from pathlib import Path

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
    speaker_labels: bool = False,
    cookies_from_browser: str | None = None,
    keep_media: bool = False,
    work_dir: str | Path | None = None,
) -> None:
    """Execute a job synchronously. Callers decide the thread."""
    store.update(job.id, state=JobState.RUNNING, progress="starting")

    def _progress(message: str) -> None:
        store.update(job.id, progress=message)

    try:
        _progress("resolving source")
        result = transcribe(
            source,
            language=language,
            formats=formats,
            output_dir=output_dir,
            model=model,
            engine=engine,
            device=device,
            speaker_labels=speaker_labels,
            cookies_from_browser=cookies_from_browser,
            keep_media=keep_media,
            work_dir=work_dir,
        )
    except PipelineError as exc:
        store.update(job.id, state=JobState.ERROR, error=str(exc), progress="failed")
        return
    except Exception as exc:  # defensive: never leave a job stuck RUNNING
        store.update(job.id, state=JobState.ERROR, error=f"{type(exc).__name__}: {exc}", progress="failed")
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
    """Create a job and optionally run it on a daemon thread."""
    job = store.create(source)
    if not background:
        run_job(job, store, source=source, **kwargs)
        return job

    thread = threading.Thread(
        target=run_job,
        args=(job, store),
        kwargs={"source": source, **kwargs},
        daemon=True,
        name=f"textflowkit-job-{job.id}",
    )
    thread.start()
    return job


def transcript_for(job: Job) -> Transcript | None:
    if job.transcript is None:
        return None
    return Transcript.from_dict(job.transcript)
