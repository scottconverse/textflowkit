"""Batch transcription orchestration.

Batch is deliberately a thin loop over one-item work. It does not know about
media acquisition, engines, or rendering; it only submits each source through
the shared one-item path and reports what each job produced.

The contract that matters operationally is the same as the ticket's wording:
one bad source must never erase the results of the other sources. Every item is
attempted, and every item gets an explicit outcome in the returned report.

Every item goes through the same `submit_request` lifecycle as CLI transcribe,
MCP, and HTTP, so resume means the same thing everywhere: the same checkpoint
matching, the same local-source fingerprint validation, and the same rendering
of requested formats into the requested output directory. The one thing batch
adds is isolation - a rejected item is reported and the loop continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textflowkit.core.executor import JobCancelled
from textflowkit.core.jobs import JobState, JobStore
from textflowkit.core.submission import SubmissionRequest, submit_request


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
    """Run every source through the shared submission path and return a report.

    `submit_request` owns creation, resume matching, fingerprint validation, and
    output rendering, and it records terminal state on the store. This layer adds
    no second pipeline path; it catches per-item failures so one bad source does
    not stop the rest.
    """
    report = BatchReport()
    for source in sources:
        item = BatchItem(source=source, status="failed")
        try:
            request = SubmissionRequest(source=source, **kwargs)
        except (TypeError, ValueError) as exc:
            item.error = str(exc)
            report.items.append(item)
            continue
        try:
            known = _known_job_ids(store)
            job = submit_request(store, request, background=False, resume=resume)
        except JobCancelled:
            item.status = "skipped"
            item.error = "cancelled"
            report.items.append(item)
            continue
        except Exception as exc:  # noqa: BLE001 - isolate one item from the rest
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"
            report.items.append(item)
            continue
        item.job_id = job.id
        # `submit_request` resumes or reuses an existing job when it can, and
        # creates a fresh one otherwise. Comparing the id it returned against the
        # ids that existed before the call is what makes that difference visible
        # here, without a second resume implementation to keep in step.
        item.resumed = job.id in known
        if job.state is JobState.DONE:
            item.status = "succeeded"
            item.outputs = list(job.outputs)
            item.error = None
        elif job.state is JobState.CANCELLED:
            item.status = "skipped"
            item.error = job.error or "cancelled"
        else:
            item.status = "failed"
            item.error = job.error or f"job ended in state {job.state.value}"
        report.items.append(item)
    return report


def _known_job_ids(store: JobStore, *, limit: int = 10_000) -> set[str]:
    """Every job id already in the store, for reporting whether an item resumed.

    The bound matches the one `find_resumable_checkpoint` searches, so an item
    can never be resumed from a job this set did not cover.
    """
    return {job.id for job in store.list(limit=limit)}
