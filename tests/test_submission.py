"""Canonical request, durable restart, and adapter submission parity."""

from __future__ import annotations

import time

import pytest

from textflowkit.core.checkpoint import CheckpointRecord
from textflowkit.core.executor import JobExecutor
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import TranscribeResult
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request

# Developer HTTP refuses a peer it cannot judge; `TestClient`'s default peer is
# the non-address `testclient`, so the HTTP callers below declare the loopback
# peer a real local caller has. No socket is opened.
LOCAL_PEER = ("127.0.0.1", 50000)


def _result(source: str) -> TranscribeResult:
    return TranscribeResult(
        transcript=Transcript(source=source, language="en",
                              segments=[Segment(0, 1, "reused")]),
        outputs=[],
    )


def test_submission_saves_the_same_request_it_runs(monkeypatch):
    from textflowkit.core import runner

    seen = {}

    def fake_transcribe(source, **kwargs):
        seen.update(kwargs)
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)
    store = MemoryJobStore()
    request = SubmissionRequest(source="media.wav", model="tiny", formats=["json"])
    job = submit_request(store, request, background=False)
    assert job.state is JobState.DONE
    assert job.request == request.to_dict()
    assert seen["model"] == "tiny"
    assert seen["formats"] == ["json"]
    assert seen["output_id"] == job.id


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_missing_export_extra_is_rejected_before_job_creation(monkeypatch, fmt, tmp_path):
    from textflowkit.core import submission

    def unavailable(formats):
        raise ValueError(f"missing {formats[0]} dependency")

    monkeypatch.setattr(submission, "validate_export_requirements", unavailable)
    store = MemoryJobStore()
    with pytest.raises(ValueError, match="missing"):
        request = SubmissionRequest(source="media.wav", formats=[fmt], output_dir=str(tmp_path))
        submit_request(store, request, background=False)
    assert store.list() == []


def test_export_preflight_does_not_block_transcript_only_request(monkeypatch):
    from textflowkit.core import submission

    monkeypatch.setattr(submission, "validate_export_requirements", lambda formats: (_ for _ in ()).throw(
        AssertionError("no file export requested")))
    request = SubmissionRequest(source="media.wav", formats=["pdf"], output_dir=None)
    assert request.formats == ["pdf"]


def test_resume_after_sqlite_restart_reuses_transcript(monkeypatch, tmp_path):
    from textflowkit.core import runner, submission
    from textflowkit.core.checkpoint import local_source_identity

    path = tmp_path / "jobs.db"
    media = tmp_path / "media.wav"
    media.write_bytes(b"source bytes")
    request = SubmissionRequest(source=str(media), model="tiny", formats=["json"])
    first = SqliteJobStore(path)
    job = first.create(request.source, request=request.to_dict())
    transcript = _result(request.source).transcript
    checkpoint = CheckpointRecord(
        source=request.source, model=request.model, options=request.options(),
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=transcript.to_dict(),
        local_identity=local_source_identity(str(media)),
    )
    first.update(job.id, state=JobState.RUNNING, checkpoint=checkpoint.to_dict())
    first.close()

    restarted = SqliteJobStore(path)
    executor = JobExecutor(restarted, max_concurrency=1)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)
    seen = {}

    def fake_transcribe(source, **kwargs):
        seen["checkpoint"] = kwargs.get("resume_checkpoint")
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)
    resumed = resume_job(restarted, job.id)
    assert resumed.id == job.id
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and restarted.get(job.id).state is not JobState.DONE:
        time.sleep(0.02)
    assert restarted.get(job.id).state is JobState.DONE
    assert seen["checkpoint"]["transcript"] == transcript.to_dict()
    executor.shutdown()
    restarted.close()


def test_resume_without_checkpoint_restarts_same_job(monkeypatch, tmp_path):
    from textflowkit.core import runner

    store = SqliteJobStore(tmp_path / "jobs.db")
    request = SubmissionRequest(source="media.wav", formats=["json"])
    job = store.create(request.source, request=request.to_dict())
    store.update(job.id, state=JobState.ERROR, error="interrupted")
    seen = {}

    def fake_transcribe(source, **kwargs):
        seen["checkpoint"] = kwargs.get("resume_checkpoint")
        return _result(source)

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)
    resumed = resume_job(store, job.id, background=False)
    assert resumed.id == job.id
    assert resumed.state is JobState.DONE
    assert seen["checkpoint"] is None
    store.close()


def test_cli_mcp_http_submit_the_same_core_request(monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from textflowkit import cli
    from textflowkit.adapters import http_server, mcp_server

    store = MemoryJobStore()
    captured: list[dict] = []

    def fake_submit(store_arg, request, **kwargs):
        assert store_arg is store
        captured.append(request.to_dict())
        job = store.create(request.source, request=request.to_dict())
        return store.update(job.id, state=JobState.DONE,
                            transcript=_result(request.source).transcript.to_dict())

    for adapter in (cli, http_server, mcp_server):
        monkeypatch.setattr(adapter, "get_default_store", lambda: store)
        monkeypatch.setattr(adapter, "submit_request", fake_submit)

    assert cli.main(["transcribe", "media.wav", "--formats", "json",
                     "--stdout", "--quiet"]) == 0
    capsys.readouterr()
    assert "job_id" in mcp_server.transcribe_media("media.wav", formats="json")
    response = TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(
        "/jobs", json={"source": "media.wav", "formats": ["json"]},
    )
    assert response.status_code == 202
    assert len(captured) == 3
    assert captured[0] == captured[1] == captured[2]


def test_mcp_and_http_expose_batch_and_resume_routes():
    from textflowkit.adapters.http_server import app
    from textflowkit.adapters.mcp_server import mcp

    tools = mcp._tool_manager._tools
    assert "submit_batch_media" in tools
    assert "resume_job" in tools
    routes = {getattr(route, "path", None) for route in app.routes}
    assert "/jobs/batch" in routes
    assert "/jobs/{job_id}/resume" in routes


def test_mcp_http_batch_forward_identical_requests(monkeypatch):
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server, mcp_server

    seen: list[list[dict]] = []

    def fake_batch(store, requests, *, resume=False):
        assert resume is True
        seen.append([request.to_dict() for request in requests])
        return [{"source": request.source, "job_id": "accepted"} for request in requests]

    monkeypatch.setattr(mcp_server, "submit_batch", fake_batch)
    monkeypatch.setattr(http_server, "submit_batch", fake_batch)
    mcp_result = mcp_server.submit_batch_media(["one", "two"], formats="json", resume=True)
    http_response = TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(
        "/jobs/batch", json={"jobs": [
            {"source": "one", "formats": ["json"]},
            {"source": "two", "formats": ["json"]},
        ], "resume": True},
    )
    assert mcp_result["count"] == 2
    assert http_response.status_code == 202
    assert http_response.json()["count"] == 2
    assert seen[0] == seen[1]


def test_mcp_http_resume_forward_job_id(monkeypatch):
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server, mcp_server

    seen: list[str] = []

    def fake_resume(store, job_id, **kwargs):
        seen.append(job_id)
        return store.create("x")

    monkeypatch.setattr(mcp_server, "core_resume_job", fake_resume)
    monkeypatch.setattr(http_server, "core_resume_job", fake_resume)
    assert "job_id" in mcp_server.resume_job("saved-id")
    assert TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post("/jobs/saved-id/resume").status_code == 202
    assert seen == ["saved-id", "saved-id"]
