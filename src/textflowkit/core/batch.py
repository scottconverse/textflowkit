"""Batch transcription orchestration.

Batch is deliberately a thin loop over one-item work. It does not know about
media acquisition, engines, or rendering; it only creates jobs through the
existing runner and reports what each job produced.

The contract that matters operationally is the same as the ticket's wording:
one bad source must never erase the results of the other sources. Every item is
attempted, and every item gets an explicit outcome in the returned report.

Resuming a batch item uses the same `prepare_resume` path as the CLI, so an
interrupted ERROR/CANCELLED job is reopened rather than silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textflowkit.core.checkpoint import (
    find_resumable_checkpoint,
    prepare_resume,
    reusable_done_result,
)
from textflowkit.core.executor import JobCancelled
from textflowkit.core.jobs import JobState, JobStore
from textflowkit.core.runner import run_job


@dataclass(slots=True)
class BatchItem:
    """One source's outcome in a batch run."""

    source: str
    status: str
    job_id: str | None = None
    error: str | None = None
    outputs: list[str] = field(default_factory=list)
    resumed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "job_id": self.job_id,
            "error": self.error,
            "outputs": list(self.outputs),
            "resumed": self.resumed,
        }


@dataclass(slots=True)
class BatchReport:
    """Aggregate and per-item outcomes for a batch invocation."""

    items: list[BatchItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> int:
        return sum(item.status == "succeeded" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "items": [item.to_dict() for item in self.items],
        }


def run_batch(
    sources: list[str],
    *,
    store: JobStore,
    resume: bool = False,
    **kwargs: Any,
) -> BatchReport:
    """Run every source through the single-item runner and return a report.

    `run_job` already records terminal state on the store. This layer adds no
    second pipeline path; it simply catches per-item failures so one bad source
    does not stop the rest.
    """
    report = BatchReport()
    for source in sources:
        item_kwargs = dict(kwargs)
        item = BatchItem(source=source, status="failed")
        if resume:
            found = find_resumable_checkpoint(
                store,
                source=source,
                model=item_kwargs.get("model", "small"),
                language=item_kwargs.get("language"),
                engine=item_kwargs.get("engine", "whisper"),
                device=item_kwargs.get("device"),
                options=_resume_options(item_kwargs),
            )
            if found is not None:
                prior, checkpoint = found
                item.job_id = prior.id
                prepared = prepare_resume(store, prior, checkpoint)
                if prepared is None:
                    reused = reusable_done_result(store, prior)
                    if reused is not None:
                        _transcript, outputs = reused
                        item.status = "succeeded"
                        item.resumed = True
                        item.outputs = [str(p) for p in outputs]
                        item.error = None
                        report.items.append(item)
                        continue
                    job = store.create(source)
                    item.job_id = job.id
                else:
                    job, payload = prepared
                    item.resumed = True
                    item_kwargs["resume_checkpoint"] = payload
            else:
                job = store.create(source)
                item.job_id = job.id
        else:
            job = store.create(source)
            item.job_id = job.id

        try:
            run_job(job, store, source=source, **item_kwargs)
            current = store.get(job.id)
            if current is None:
                item.status = "failed"
                item.error = "job disappeared from the store"
            elif current.state is JobState.DONE:
                item.status = "succeeded"
                item.outputs = list(current.outputs)
            elif current.state is JobState.CANCELLED:
                item.status = "skipped"
                item.error = current.error or "cancelled"
            else:
                item.status = "failed"
                item.error = current.error or f"job ended in state {current.state.value}"
        except JobCancelled:
            item.status = "skipped"
            item.error = "cancelled"
        except Exception as exc:  # noqa: BLE001 - isolate one item from the rest
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"
        report.items.append(item)
    return report


def _resume_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "formats": list(kwargs.get("formats") or []),
        "diarize": bool(kwargs.get("diarize", False)),
        "diarizer_backend": kwargs.get("diarizer_backend", "pyannote"),
        "translate_to": kwargs.get("translate_to"),
        "translator_backend": kwargs.get("translator_backend", "ollama"),
    }
