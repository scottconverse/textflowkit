"""One submission contract for CLI, MCP, HTTP, and batch callers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from textflowkit.core.checkpoint import (
    find_resumable_checkpoint,
    load_checkpoint,
    matches,
    prepare_resume,
    reusable_done_result,
)
from textflowkit.core.executor import QueueFullError, get_default_executor
from textflowkit.core.jobs import Job, JobState, JobStore
from textflowkit.core.runner import run_job
from textflowkit.render import SUPPORTED_FORMATS


@dataclass(slots=True)
class SubmissionRequest:
    source: str
    language: str | None = None
    formats: list[str] = field(default_factory=lambda: ["json", "srt", "txt"])
    output_dir: str | None = None
    model: str = "small"
    engine: str = "whisper"
    device: str | None = None
    cookies_from_browser: str | None = None
    input_root: str | None = None
    work_dir: str | None = None
    diarize: bool = False
    diarizer_backend: str = "pyannote"
    translate_to: str | None = None
    translator_backend: str = "ollama"

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("source is required")
        self.formats = [str(fmt).lower().lstrip(".") for fmt in self.formats]
        bad = [fmt for fmt in self.formats if fmt not in SUPPORTED_FORMATS]
        if bad:
            raise ValueError(f"unsupported format(s): {', '.join(bad)}")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("duplicate output format")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmissionRequest:
        return cls(**data)

    def options(self) -> dict[str, Any]:
        return {
            "formats": list(self.formats),
            "diarize": self.diarize,
            "diarizer_backend": self.diarizer_backend,
            "translate_to": self.translate_to,
            "translator_backend": self.translator_backend,
        }

    def run_kwargs(self) -> dict[str, Any]:
        return self.to_dict()


def _matching_checkpoint(store: JobStore, request: SubmissionRequest, job_id: str | None):
    if job_id is not None:
        job = store.get(job_id)
        if job is None:
            raise ValueError(f"no job with id '{job_id}'")
        if job.request is not None:
            saved = dict(job.request)
            incoming = request.to_dict()
            for key in ("input_root", "work_dir"):
                saved.pop(key, None)
                incoming.pop(key, None)
            if saved != incoming:
                raise ValueError("resume request does not match the saved request")
        checkpoint = load_checkpoint(job)
        if checkpoint is None:
            return job, None
        if not matches(
            checkpoint, source=request.source, model=request.model,
            language=request.language, engine=request.engine, device=request.device,
            options=request.options(),
        ):
            raise ValueError("resume request does not match the saved checkpoint")
        return job, checkpoint
    return find_resumable_checkpoint(
        store, source=request.source, model=request.model,
        language=request.language, engine=request.engine, device=request.device,
        options=request.options(),
    )


def submit_request(
    store: JobStore,
    request: SubmissionRequest,
    *,
    background: bool = True,
    resume: bool = False,
    resume_job_id: str | None = None,
) -> Job:
    """Submit or resume via the same durable job lifecycle on every surface."""
    executor = get_default_executor() if background else None
    if executor is not None and executor.store is store:
        # Reap old PENDING/RUNNING records before selecting one to resume.
        executor.start()
    found = _matching_checkpoint(store, request, resume_job_id) if resume or resume_job_id else None
    if found is not None:
        prior, checkpoint = found
        if prior.state is JobState.DONE:
            if request.output_dir is None:
                return prior
            result = reusable_done_result(store, prior, formats=request.formats,
                                          output_dir=request.output_dir,
                                          stem=_output_stem(prior))
            if result is not None:
                _transcript, outputs = result
                updated = store.update(prior.id, outputs=[str(p) for p in outputs])
                return updated or prior
        if prior.state in {JobState.PENDING, JobState.RUNNING}:
            raise ValueError(f"job '{prior.id}' is already active")
        prepared = prepare_resume(store, prior, checkpoint) if checkpoint else None
        if checkpoint is None:
            reopened = store.update(
                prior.id, state=JobState.PENDING, progress="resuming from start",
                error=None, cancel_requested=False,
            )
            if reopened is None:
                raise ValueError(f"job '{prior.id}' disappeared before resume")
            prepared = reopened, None
        if prepared is not None:
            job, checkpoint_payload = prepared
            kwargs = request.run_kwargs()
            store.update(job.id, request=request.to_dict())
            if checkpoint_payload is not None:
                kwargs["resume_checkpoint"] = checkpoint_payload
            if executor is not None and executor.store is store:
                try:
                    return executor.enqueue(job, **kwargs)
                except QueueFullError:
                    store.update(job.id, state=JobState.ERROR, progress="queue full",
                                 error="resume not queued; job queue is full")
                    raise
            run_job(job, store, **kwargs)
            return store.get(job.id) or job

    kwargs = request.run_kwargs()
    if executor is not None and executor.store is store:
        return executor.submit(request=request.to_dict(), **kwargs)
    job = store.create(request.source, request=request.to_dict())
    run_job(job, store, **kwargs)
    return store.get(job.id) or job


def resume_job(
    store: JobStore, job_id: str, *, background: bool = True,
    input_root: str | None = None, work_dir: str | None = None,
) -> Job:
    """Resume an interrupted durable job using its original saved request."""
    job = store.get(job_id)
    if job is None:
        raise ValueError(f"no job with id '{job_id}'")
    if job.request is None:
        raise ValueError("job predates saved requests; resubmit its original options")
    request_data = dict(job.request)
    if input_root is not None:
        request_data["input_root"] = input_root
    if work_dir is not None:
        request_data["work_dir"] = work_dir
    return submit_request(
        store, SubmissionRequest.from_dict(request_data),
        background=background, resume=True, resume_job_id=job_id,
    )


def submit_batch(
    store: JobStore, requests: list[SubmissionRequest], *, resume: bool = False
) -> list[dict[str, Any]]:
    """Accept each source independently; a bad item never hides later items."""
    results: list[dict[str, Any]] = []
    for request in requests:
        try:
            job = submit_request(store, request, resume=resume)
            results.append({"source": request.source, "job_id": job.id, "state": job.state.value})
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            results.append({"source": request.source, "error": f"{type(exc).__name__}: {exc}"})
    return results


def _output_stem(job: Job) -> str:
    if job.outputs:
        return Path(job.outputs[0]).stem
    parsed = urlparse(job.source)
    name = Path(parsed.path).stem if parsed.scheme in {"http", "https"} else Path(job.source).stem
    return f"{name or 'transcript'}-{job.id}"
