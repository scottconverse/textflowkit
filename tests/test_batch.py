"""Batch orchestration over the shared single-item submission path.

Batch no longer has its own runner call: every item goes through
`submit_request`, so these tests fake the pipeline at `submission.run_job`,
which is the seam the shared lifecycle actually uses in synchronous mode.
"""
from __future__ import annotations

from textflowkit.core import submission
from textflowkit.core.batch import run_batch
from textflowkit.core.checkpoint import CheckpointRecord
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript


def _record(source: str, *, model: str = "small", language: str | None = "en",
            options: dict | None = None,
            stages: list[str] | None = None) -> CheckpointRecord:
    tr = Transcript(source=source, language=language,
                    segments=[Segment(0.0, 1.0, "seeded")])
    return CheckpointRecord(
        source=source,
        model=model,
        language=language,
        device=None,
        options=options or {
            "formats": ["json"],
            "diarize": False,
            "diarizer_backend": "pyannote",
            "translate_to": None,
            "translator_backend": "ollama",
        },
        finished_stages=stages or ["source", "fetch", "extract", "transcribe"],
        transcript=tr.to_dict(),
    )


def test_batch_reports_every_item_and_continues_after_failure(monkeypatch, tmp_path):
    store = MemoryJobStore()
    seen: list[str] = []
    # The shared contract refuses a local source that is not present, so these
    # batch items have to name real files. Nothing reads them: `run_job` is
    # stubbed below and the report, not the bytes, is what this test is about.
    sources = []
    for name in ("one", "bad", "three"):
        (tmp_path / name).write_bytes(b"")
        sources.append(str(tmp_path / name))

    def fake_run_job(job, store_arg, *, source=None, **kwargs):
        seen.append(source or job.source)
        if source == sources[1]:
            store_arg.update(job.id, state=JobState.ERROR, error="bad source")
            return
        store_arg.update(
            job.id,
            state=JobState.DONE,
            outputs=[f"{source}.json"],
        )

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch(sources, store=store, formats=["json"], output_dir=".")

    assert seen == sources
    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    assert report.skipped == 0
    assert report.ok is False
    assert [item.status for item in report.items] == ["succeeded", "failed", "succeeded"]
    assert report.items[1].error == "bad source"
    assert report.items[0].outputs == [f"{sources[0]}.json"]


def test_batch_catches_per_item_exception_and_continues(monkeypatch, tmp_path):
    store = MemoryJobStore()
    sources = []
    for name in ("ok", "boom", "after"):
        (tmp_path / name).write_bytes(b"")
        sources.append(str(tmp_path / name))

    def fake_run_job(job, store_arg, *, source=None, **kwargs):
        if source == sources[1]:
            raise RuntimeError("exploded")
        store_arg.update(job.id, state=JobState.DONE, outputs=[])

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch(sources, store=store)

    assert report.total == 3
    assert [item.status for item in report.items] == ["succeeded", "failed", "succeeded"]
    assert report.items[1].error == "RuntimeError: exploded"
    assert report.ok is False


def test_batch_reports_cancelled_item_as_skipped(monkeypatch, tmp_path):
    """A cancelled item is a skip, not a failure, and later items still run."""
    store = MemoryJobStore()
    sources = []
    for name in ("stop", "after"):
        (tmp_path / name).write_bytes(b"")
        sources.append(str(tmp_path / name))

    def fake_run_job(job, store_arg, *, source=None, **kwargs):
        if source == sources[0]:
            store_arg.update(job.id, state=JobState.CANCELLED, error="cancelled")
        else:
            store_arg.update(job.id, state=JobState.DONE, outputs=[])

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch(sources, store=store)

    assert [item.status for item in report.items] == ["skipped", "succeeded"]
    assert report.items[0].error == "cancelled"
    assert report.items[0].resumed is False
    assert report.skipped == 1
    assert report.ok is True


def test_batch_resume_reopens_error_and_forwards_checkpoint(monkeypatch):
    store = MemoryJobStore()
    prior = store.create("https://example.com/v")
    checkpoint = _record("https://example.com/v")
    store.update(prior.id, state=JobState.ERROR, error="interrupted",
                 checkpoint=checkpoint.to_dict())

    calls = []

    def fake_run_job(job, store_arg, *, source=None, resume_checkpoint=None, **kwargs):
        calls.append((job.id, resume_checkpoint))
        store_arg.update(job.id, state=JobState.DONE, outputs=[])

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch(
        ["https://example.com/v"],
        store=store,
        resume=True,
        model="small",
        language="en",
        formats=["json"],
    )

    assert report.items[0].status == "succeeded"
    assert report.items[0].job_id == prior.id
    assert report.items[0].resumed is True
    assert len(calls) == 1
    assert calls[0][0] == prior.id
    assert calls[0][1] == checkpoint.to_dict()
    assert store.get(prior.id).state is JobState.DONE


def test_batch_resume_reopens_cancelled_item(monkeypatch):
    store = MemoryJobStore()
    prior = store.create("https://example.com/v")
    checkpoint = _record("https://example.com/v")
    store.update(prior.id, state=JobState.CANCELLED, cancel_requested=True,
                 checkpoint=checkpoint.to_dict())

    seen = []

    def fake_run_job(job, store_arg, *, source=None, resume_checkpoint=None, **kwargs):
        seen.append((job.id, resume_checkpoint))
        store_arg.update(job.id, state=JobState.DONE, outputs=[])

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch(["https://example.com/v"], store=store,
                       resume=True, model="small", language="en", formats=["json"])

    assert report.items[0].resumed is True
    assert report.items[0].job_id == prior.id
    assert seen[0][0] == prior.id
    assert seen[0][1] == checkpoint.to_dict()


def test_batch_resume_reuses_done_result_without_new_job(monkeypatch):
    store = MemoryJobStore()
    prior = store.create("https://example.com/v")
    checkpoint = _record("https://example.com/v", stages=[
        "source", "fetch", "extract", "transcribe", "postprocess", "render",
    ])
    transcript = Transcript.from_dict(checkpoint.transcript)
    prior = store.update(
        prior.id,
        state=JobState.DONE,
        transcript=transcript.to_dict(),
        outputs=["kept.json"],
        checkpoint=checkpoint.to_dict(),
    )

    def explode(*args, **kwargs):
        raise AssertionError("DONE resume should not call run_job")

    monkeypatch.setattr(submission, "run_job", explode)
    before = len(store.list(limit=100))

    report = run_batch(
        ["https://example.com/v"],
        store=store,
        resume=True,
        model="small",
        language="en",
        formats=["json"],
    )

    assert len(store.list(limit=100)) == before
    assert report.items[0].status == "succeeded"
    assert report.items[0].job_id == prior.id
    assert report.items[0].resumed is True
    assert report.items[0].outputs == ["kept.json"]


def test_batch_resume_without_checkpoint_starts_new_job(monkeypatch, tmp_path):
    store = MemoryJobStore()
    seen = []
    media = str(tmp_path / "new")
    (tmp_path / "new").write_bytes(b"")

    def fake_run_job(job, store_arg, *, source=None, resume_checkpoint=None, **kwargs):
        seen.append((job.id, resume_checkpoint))
        store_arg.update(job.id, state=JobState.DONE, outputs=[])

    monkeypatch.setattr(submission, "run_job", fake_run_job)

    report = run_batch([media], store=store, resume=True, model="small")

    assert report.items[0].resumed is False
    assert seen[0][1] is None
    assert store.get(report.items[0].job_id).state is JobState.DONE


def test_batch_report_to_dict_shape():
    from textflowkit.core.batch import BatchItem, BatchReport

    report = BatchReport(items=[
        BatchItem(source="a", status="succeeded", job_id="j1", outputs=["a.json"]),
        BatchItem(source="b", status="failed", job_id="j2", error="nope"),
        BatchItem(source="c", status="skipped", job_id="j3", error="cancelled"),
    ])

    data = report.to_dict()

    assert data["total"] == 3
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    assert data["skipped"] == 1
    assert data["items"][0] == {
        "source": "a",
        "status": "succeeded",
        "job_id": "j1",
        "error": None,
        "outputs": ["a.json"],
        "resumed": False,
    }
