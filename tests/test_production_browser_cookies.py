"""A production caller must never make the server read the owner's browser cookies.

Outside review A9: in the opt-in production profile the HTTP and MCP surfaces
serve callers who are not the Windows account owner, so `cookies_from_browser`
must be refused before job creation, queueing, or resume. Developer mode on the
owner's own machine keeps the option. Nothing here touches a real browser,
yt-dlp, the network, or a model.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from textflowkit.adapters import http_server, mcp_server
from textflowkit.core import runner, submission
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import JobState, MemoryJobStore, get_default_store, reset_default_store
from textflowkit.core.submission import SubmissionRequest, resume_job

TOKEN = "a-long-test-token-12345"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def production(monkeypatch, tmp_path):
    reset_default_executor()
    reset_default_store()
    input_root = tmp_path / "input"
    input_root.mkdir()
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_API_TOKEN", TOKEN)
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(input_root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    http_server._RATE_BUCKETS.clear()
    yield
    reset_default_executor()
    reset_default_store()
    http_server._RATE_BUCKETS.clear()


@pytest.fixture
def developer(monkeypatch):
    reset_default_executor()
    reset_default_store()
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    yield
    reset_default_executor()
    reset_default_store()


def _no_transcription(monkeypatch) -> list[int]:
    """Fail loudly if any rejected request reaches the pipeline."""
    calls: list[int] = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise AssertionError("transcription started for a refused request")

    monkeypatch.setattr(runner, "transcribe", fail)
    return calls


def _saved_cookie_request(source: str = "https://example.com/v") -> dict:
    """A request row as an older build would have saved it, cookie option included."""
    return {
        "source": source,
        "language": None,
        "formats": ["json"],
        "output_dir": None,
        "model": "small",
        "engine": "whisper",
        "device": None,
        "cookies_from_browser": "firefox",
        "input_root": None,
        "work_dir": None,
        "diarize": False,
        "diarizer_backend": "pyannote",
        "translate_to": None,
        "translator_backend": "ollama",
    }


# --- production must refuse ------------------------------------------------


def test_production_http_single_job_refuses_browser_cookies(production, monkeypatch):
    calls = _no_transcription(monkeypatch)
    client = TestClient(http_server.app)
    response = client.post(
        "/jobs",
        json={"source": "https://example.com/v", "cookies_from_browser": "firefox"},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "cookies_from_browser" in response.json()["detail"]
    assert "production" in response.json()["detail"]
    assert calls == []
    assert get_default_store().list() == []


def test_production_http_batch_refuses_before_any_item(production, monkeypatch):
    calls = _no_transcription(monkeypatch)
    client = TestClient(http_server.app)
    response = client.post(
        "/jobs/batch",
        json={"jobs": [
            {"source": "https://example.com/clean", "formats": ["json"]},
            {"source": "https://example.com/gated", "formats": ["json"],
             "cookies_from_browser": "firefox"},
        ]},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "cookies_from_browser" in response.json()["detail"]
    assert calls == []
    # The clean item listed first must not have been submitted either.
    assert get_default_store().list() == []


def test_production_mcp_tool_refuses_browser_cookies_before_queue(production, monkeypatch):
    def unexpected_submit(*args, **kwargs):
        raise AssertionError("MCP submitted a cookie-bearing request")

    monkeypatch.setattr(mcp_server, "submit_request", unexpected_submit)
    result = mcp_server.transcribe_media("https://example.com/v", cookies_from_browser="firefox")
    assert "error" in result
    assert "cookies_from_browser" in result["error"]
    assert get_default_store().list() == []


def test_production_resume_refuses_saved_browser_cookie_job(production, monkeypatch):
    calls = _no_transcription(monkeypatch)
    store = get_default_store()
    job = store.create("https://example.com/v", request=_saved_cookie_request())
    store.update(job.id, state=JobState.ERROR, error="interrupted")

    with pytest.raises(ValueError, match="cookies_from_browser"):
        resume_job(store, job.id)

    assert calls == []
    assert store.get(job.id).state is JobState.ERROR
    assert store.list() != []


def test_production_http_resume_refuses_saved_browser_cookie_job(production, monkeypatch):
    calls = _no_transcription(monkeypatch)
    store = get_default_store()
    job = store.create("https://example.com/v", request=_saved_cookie_request())
    store.update(job.id, state=JobState.ERROR, error="interrupted")

    response = TestClient(http_server.app).post(f"/jobs/{job.id}/resume", headers=HEADERS)

    assert response.status_code == 409
    assert "cookies_from_browser" in response.json()["detail"]
    assert calls == []
    assert store.get(job.id).state is JobState.ERROR


def test_production_still_accepts_cookie_free_requests(production, monkeypatch):
    """The refusal is narrow: an ordinary production request still queues.

    A recording stub replaces the process executor so the assertion is about the
    queued request, not about a worker thread winning a race.
    """
    calls = _no_transcription(monkeypatch)
    store = get_default_store()
    queued: list[dict] = []

    class _RecordingExecutor:
        def __init__(self, bound_store):
            self.store = bound_store

        def start(self):
            pass

        def submit(self, *, source, request=None, **kwargs):
            queued.append(request)
            return self.store.create(source, request=request)

    monkeypatch.setattr(submission, "get_default_executor", lambda: _RecordingExecutor(store))
    response = TestClient(http_server.app).post(
        "/jobs", json={"source": "https://example.com/v", "formats": ["json"]},
        headers=HEADERS,
    )
    assert response.status_code == 202
    assert response.json()["state"] == JobState.PENDING.value
    assert len(queued) == 1
    assert queued[0]["source"] == "https://example.com/v"
    assert queued[0]["formats"] == ["json"]
    assert queued[0]["cookies_from_browser"] is None
    assert len(store.list()) == 1
    assert calls == []


# --- developer mode keeps the owner-controlled option ----------------------


def test_developer_mode_submission_keeps_browser_cookies(developer):
    request = SubmissionRequest(source="https://example.com/v", cookies_from_browser="firefox")
    assert request.to_dict()["cookies_from_browser"] == "firefox"


def test_developer_mode_http_and_mcp_forward_browser_cookies(developer, monkeypatch):
    captured: list[dict] = []

    def fake_submit(store_arg, request, **kwargs):
        captured.append(request.to_dict())
        return MemoryJobStore().create(request.source, request=request.to_dict())

    monkeypatch.setattr(http_server, "submit_request", fake_submit)
    monkeypatch.setattr(mcp_server, "submit_request", fake_submit)

    response = TestClient(http_server.app, base_url="http://127.0.0.1").post(
        "/jobs", json={"source": "https://example.com/v", "cookies_from_browser": "firefox"},
    )
    assert response.status_code == 202
    assert "job_id" in mcp_server.transcribe_media(
        "https://example.com/v", cookies_from_browser="firefox"
    )
    assert len(captured) == 2
    assert [entry["cookies_from_browser"] for entry in captured] == ["firefox", "firefox"]
