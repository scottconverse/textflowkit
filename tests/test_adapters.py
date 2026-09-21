"""Adapter surface tests.

These exercise the adapter layer without requiring network access or a live
transcription: tool listing, job plumbing, and error handling.
"""

from __future__ import annotations

import pytest

from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.core.runner import submit


@pytest.fixture(autouse=True)
def clean_store():
    get_default_store().clear()
    yield
    get_default_store().clear()


# --- runner ---------------------------------------------------------------

def test_submit_sync_records_error_for_missing_file():
    store = get_default_store()
    job = submit(store, source="C:/definitely/missing.mp4", background=False, model="tiny")
    assert job.state is JobState.ERROR
    assert "no such file" in (job.error or "")


def test_submit_background_returns_pending_job():
    store = get_default_store()
    job = submit(store, source="C:/definitely/missing.mp4", model="tiny")
    assert job.id
    assert job.state in (JobState.PENDING, JobState.RUNNING, JobState.ERROR)


def test_transcript_for_empty_job_is_none():
    from textflowkit.core.runner import transcript_for

    store = get_default_store()
    job = store.create("x")
    assert transcript_for(job) is None


# --- MCP adapter ----------------------------------------------------------

def test_mcp_server_constructs_with_tools():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    assert mcp.name == "textflowkit"
    tools = mcp._tool_manager._tools
    for expected in (
        "list_sources",
        "transcribe_media",
        "get_job_status",
        "get_transcript",
        "export_transcript",
        "list_jobs",
    ):
        assert expected in tools, expected


def test_mcp_annotations_mark_read_only_tools():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    tools = mcp._tool_manager._tools
    for name in ("list_sources", "get_job_status", "get_transcript", "list_jobs"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("transcribe_media", "export_transcript"):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.open_world_hint is True


def test_mcp_list_sources_reports_capabilities():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import list_sources

    result = list_sources()
    assert "youtube" in result["platforms"]
    assert "srt" in result["formats"]
    assert result["input_kinds"] == ["local", "direct"]


def test_mcp_transcribe_rejects_bad_format():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("x", formats="docx")
    assert "error" in out
    assert "docx" in out["error"]


def test_mcp_status_unknown_job():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_job_status

    assert "error" in get_job_status("nope")


def test_mcp_transcript_requires_finished_job():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_transcript, transcribe_media

    started = transcribe_media("C:/nope.mp4", model="tiny")
    out = get_transcript(started["job_id"])
    # Either it is still running, or it already failed - never a transcript.
    assert "error" in out


def test_mcp_list_jobs_validates_state():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import list_jobs

    out = list_jobs(state="bogus")
    assert "error" in out
    assert "available_states" in out


# --- HTTP adapter ---------------------------------------------------------

def test_http_routes_registered():
    pytest.importorskip("fastapi")
    from textflowkit.adapters.http_server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for expected in ("/health", "/sources", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/transcript"):
        assert expected in paths, expected


def test_http_health_and_sources():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    c = TestClient(app)
    assert c.get("/health").json()["status"] == "ok"
    s = c.get("/sources").json()
    assert "youtube" in s["platforms"]


def test_http_404_for_unknown_job():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    assert TestClient(app).get("/jobs/nope").status_code == 404


def test_http_422_for_bad_format():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    r = TestClient(app).post("/jobs", json={"source": "x", "formats": ["docx"]})
    assert r.status_code == 422


def test_http_transcript_conflict_before_done():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    store = get_default_store()
    job = store.create("x")  # stays PENDING
    r = TestClient(app).get(f"/jobs/{job.id}/transcript")
    assert r.status_code == 409
