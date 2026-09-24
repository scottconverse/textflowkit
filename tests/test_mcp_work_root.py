"""The MCP adapter must pass the configured service work root (U52).

HTTP forwards `service_work_root()` into both new and resumed requests; MCP did
not, so in the production profile a job queued over MCP put its scratch tree
under the system temp directory instead of the configured work root, and a
resume reused the stale root saved with the original request. Both are the
production scratch-escape the profile exists to close.

Every check here is at the adapter's boundary - the request handed to the core,
or the request row the core stores - and none of them reaches a model, ffmpeg,
yt-dlp, or the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from textflowkit.adapters import mcp_server
from textflowkit.core import submission
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import JobState, MemoryJobStore, get_default_store, reset_default_store
from textflowkit.core.submission import SubmissionRequest


@pytest.fixture
def production(monkeypatch, tmp_path):
    reset_default_executor()
    reset_default_store()
    input_root = tmp_path / "input"
    input_root.mkdir()
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_API_TOKEN", "a-long-test-token-12345")
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(input_root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    yield
    reset_default_executor()
    reset_default_store()


@pytest.fixture
def developer(monkeypatch):
    reset_default_executor()
    reset_default_store()
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_WORK_ROOT", raising=False)
    yield
    reset_default_executor()
    reset_default_store()


def _configured_work_root() -> str:
    """The work root as the environment names it, resolved the way paths are."""
    return str(Path(os.environ["TEXTFLOWKIT_WORK_ROOT"]).expanduser().resolve())


def _captured_submissions(monkeypatch) -> list[dict]:
    """Record the request handed to the core; queue nothing."""
    captured: list[dict] = []

    def fake_submit(store, request, **kwargs):
        captured.append(request.to_dict())
        return MemoryJobStore().create(request.source, request=request.to_dict())

    monkeypatch.setattr(mcp_server, "submit_request", fake_submit)
    return captured


def _captured_batches(monkeypatch) -> list[list[dict]]:
    """Record the requests handed to the batch core; queue nothing."""
    captured: list[list[dict]] = []

    def fake_batch(store, requests, **kwargs):
        captured.append([request.to_dict() for request in requests])
        return [{"source": r.source, "job_id": f"job-{i}", "state": "pending"}
                for i, r in enumerate(requests)]

    monkeypatch.setattr(mcp_server, "submit_batch", fake_batch)
    return captured


class _RecordingExecutor:
    """Stand in for the process executor, so the stored row is the evidence."""

    def __init__(self, bound_store):
        self.store = bound_store
        self.enqueued: list[tuple[str, dict]] = []

    def start(self):
        pass

    def submit(self, *, source, request=None, **kwargs):
        job = self.store.create(source, request=request)
        return job

    def enqueue(self, job, *, source, **kwargs):
        self.enqueued.append((job.id, kwargs))
        return job


def _saved_request(work_dir: str | None) -> dict:
    """A request row as an earlier build saved it, with a stale scratch root."""
    return SubmissionRequest(
        source="https://example.com/v", formats=["json"], work_dir=work_dir,
    ).to_dict()


# --- production must use the configured work root ---------------------------


def test_production_mcp_single_submission_passes_the_configured_work_root(
    production, monkeypatch
):
    captured = _captured_submissions(monkeypatch)
    result = mcp_server.transcribe_media("https://example.com/v", formats="json")
    assert "job_id" in result
    assert captured[0]["work_dir"] == _configured_work_root()


def test_production_mcp_batch_submission_passes_the_configured_work_root(
    production, monkeypatch
):
    captured = _captured_batches(monkeypatch)
    result = mcp_server.submit_batch_media(
        ["https://example.com/a", "https://example.com/b"], formats="json"
    )
    assert result["count"] == 2
    expected = _configured_work_root()
    assert [entry["work_dir"] for entry in captured[0]] == [expected, expected]


def test_production_mcp_resume_replaces_a_stale_saved_work_root(production, monkeypatch):
    """The current work root replaces the one saved with the original request."""
    store = get_default_store()
    job = store.create(
        "https://example.com/v", request=_saved_request(str(Path("stale") / "scratch"))
    )
    store.update(job.id, state=JobState.ERROR, error="interrupted")
    executor = _RecordingExecutor(store)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)

    result = mcp_server.resume_job(job.id)

    assert result == {"job_id": job.id, "state": JobState.PENDING.value}
    # The row the core stored is the durable evidence: a restored save would
    # resume under the old root even after the operator tightened it.
    assert store.get(job.id).request["work_dir"] == _configured_work_root()


def test_production_mcp_resume_keeps_the_current_input_root(production, monkeypatch):
    """Threading the work root must not displace the input-root confinement."""
    store = get_default_store()
    job = store.create("https://example.com/v", request=_saved_request(None))
    store.update(job.id, state=JobState.ERROR, error="interrupted")
    seen: list[dict] = []

    def fake_resume(store_arg, job_id, **kwargs):
        seen.append(kwargs)
        return store_arg.get(job_id)

    monkeypatch.setattr(mcp_server, "core_resume_job", fake_resume)
    mcp_server.resume_job(job.id)

    assert seen[0]["input_root"] == str(Path(os.environ["TEXTFLOWKIT_INPUT_ROOT"]).resolve())
    assert seen[0]["work_dir"] == _configured_work_root()


# --- developer mode is unchanged -------------------------------------------


def test_developer_mcp_submission_leaves_work_dir_unset(developer, monkeypatch):
    captured = _captured_submissions(monkeypatch)
    assert "job_id" in mcp_server.transcribe_media("https://example.com/v", formats="json")
    assert captured[0]["work_dir"] is None


def test_developer_mcp_resume_leaves_the_saved_work_root_alone(developer, monkeypatch):
    """No profile, no configured root: the saved scratch directory stands."""
    saved = str(Path("dev") / "scratch")
    store = get_default_store()
    job = store.create("https://example.com/v", request=_saved_request(saved))
    store.update(job.id, state=JobState.ERROR, error="interrupted")
    executor = _RecordingExecutor(store)
    monkeypatch.setattr(submission, "get_default_executor", lambda: executor)

    mcp_server.resume_job(job.id)

    assert store.get(job.id).request["work_dir"] == saved
