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

    out = transcribe_media("x", formats="xyzzy")
    assert "error" in out
    assert "xyzzy" in out["error"]


def test_mcp_queue_full_is_explicit_and_retryable(monkeypatch):
    pytest.importorskip("mcp")
    from textflowkit.adapters import mcp_server
    from textflowkit.core.executor import QueueFullError

    def full(*args, **kwargs):
        raise QueueFullError("job queue is full")

    monkeypatch.setattr(mcp_server, "submit_request", full)
    out = mcp_server.transcribe_media("x")
    assert out == {"error": "job queue is full", "retryable": True}


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

    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/health").json()["status"] == "ok"
    s = c.get("/sources").json()
    assert "youtube" in s["platforms"]


def test_http_404_for_unknown_job():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    assert TestClient(app, base_url="http://127.0.0.1").get("/jobs/nope").status_code == 404


def test_http_status_omits_complete_transcript():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    store = get_default_store()
    job = store.create("x")
    store.update(
        job.id, state=JobState.DONE,
        transcript={"segments": [{"text": "private"}]},
        checkpoint={"transcript": {"segments": [{"text": "private"}]}},
    )
    body = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}").json()
    assert body["state"] == "done"
    assert "transcript" not in body
    assert "checkpoint" not in body
    assert "private" not in str(body)


@pytest.mark.parametrize("limit", [-1, 1001])
def test_list_limits_are_rejected_by_both_adapters(limit):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app
    from textflowkit.adapters.mcp_server import list_jobs

    assert TestClient(app, base_url="http://127.0.0.1").get("/jobs", params={"limit": limit}).status_code == 422
    assert "error" in list_jobs(limit=limit)


def test_list_zero_limit_is_empty_on_both_adapters():
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app
    from textflowkit.adapters.mcp_server import list_jobs

    assert TestClient(app, base_url="http://127.0.0.1").get("/jobs", params={"limit": 0}).json()["jobs"] == []
    assert list_jobs(limit=0)["jobs"] == []


def test_http_422_for_bad_format():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    r = TestClient(app, base_url="http://127.0.0.1").post("/jobs", json={"source": "x", "formats": ["xyzzy"]})
    assert r.status_code == 422


def test_http_queue_full_returns_429(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server
    from textflowkit.core.executor import QueueFullError

    def full(*args, **kwargs):
        raise QueueFullError("job queue is full")

    monkeypatch.setattr(http_server, "submit_request", full)
    response = TestClient(http_server.app, base_url="http://127.0.0.1").post("/jobs", json={"source": "x"})
    assert response.status_code == 429
    assert "queue is full" in response.json()["detail"]


def test_http_transcript_conflict_before_done():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    store = get_default_store()
    job = store.create("x")  # stays PENDING
    r = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}/transcript")
    assert r.status_code == 409


# --- cancellation surface -------------------------------------------------

def test_mcp_exposes_cancel_job():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    assert "cancel_job" in mcp._tool_manager._tools


def test_mcp_cancel_annotations_are_mutating_not_open_world():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    ann = mcp._tool_manager._tools["cancel_job"].annotations
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.open_world_hint is False   # local state only, no network/disk


def test_mcp_cancel_unknown_job():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import cancel_job

    out = cancel_job("nope")
    assert "error" in out


def test_mcp_cancel_terminal_job_reports_reason():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import cancel_job

    store = get_default_store()
    job = store.create("x")
    store.update(job.id, state=JobState.DONE)

    out = cancel_job(job.id)
    assert out["cancelled"] is False
    assert out["state"] == "done"
    assert "already done" in out["reason"]


def test_mcp_cancel_queued_job_succeeds():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import cancel_job

    store = get_default_store()
    job = store.create("x")  # PENDING, no executor token
    out = cancel_job(job.id)
    assert out["cancelled"] is True
    assert out["state"] == "cancelled"
    assert store.get(job.id).state is JobState.CANCELLED


def test_http_has_cancel_route():
    pytest.importorskip("fastapi")
    from textflowkit.adapters.http_server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/jobs/{job_id}/cancel" in paths


def test_http_cancel_unknown_job_404():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    assert TestClient(app, base_url="http://127.0.0.1").post("/jobs/nope/cancel").status_code == 404


def test_http_cancel_terminal_job():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    store = get_default_store()
    job = store.create("x")
    store.update(job.id, state=JobState.ERROR)

    r = TestClient(app, base_url="http://127.0.0.1").post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    assert r.json()["state"] == "error"


def test_http_cancel_pending_job():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    store = get_default_store()
    job = store.create("x")

    r = TestClient(app, base_url="http://127.0.0.1").post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert r.json()["state"] == "cancelled"


# --- retrieval surface (paging / search) ----------------------------------

def _seed_done_job(store, n=10):
    from textflowkit.core.model import Segment, Transcript

    job = store.create("src")
    tr = Transcript(
        source="src",
        language="en",
        segments=[Segment(i, i + 0.5, f"word{i} text") for i in range(n)],
    )
    store.update(job.id, state=JobState.DONE, transcript=tr.to_dict(), outputs=[])
    return job


def test_mcp_get_transcript_pages():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_transcript

    store = get_default_store()
    job = _seed_done_job(store, 10)

    out = get_transcript(job.id, fmt="json", limit=4)
    assert out["returned"] == 4
    assert out["total_segments"] == 10
    assert out["has_more"] is True

    out2 = get_transcript(job.id, fmt="json", offset=8, limit=4)
    assert out2["returned"] == 2
    assert out2["has_more"] is False


def test_mcp_get_transcript_time_range():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_transcript

    job = _seed_done_job(get_default_store(), 10)
    out = get_transcript(job.id, fmt="txt", start=2.0, end=4.0)
    assert out["returned"] == 3
    assert "word2" in out["content"]
    assert "word5" not in out["content"]


def test_mcp_get_transcript_rejects_bad_offset():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_transcript

    job = _seed_done_job(get_default_store(), 5)
    assert "error" in get_transcript(job.id, offset=-1)


def test_mcp_get_transcript_reports_next_page():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import get_transcript

    job = _seed_done_job(get_default_store(), 10)
    out = get_transcript(job.id, fmt="json", limit=3)
    assert "offset=3" in out["next"]


def test_http_and_mcp_word_timings_are_opt_in_and_stored_data_is_unchanged():
    import json

    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app
    from textflowkit.adapters.mcp_server import get_transcript
    from textflowkit.core.model import Segment, Transcript, WordTiming

    store = get_default_store()
    job = store.create("source.wav")
    transcript = Transcript(source="source.wav", segments=[Segment(
        0, 1, "source speech", translated_text="translated speech",
        words=[WordTiming(0, 0.5, "source")],
    )])
    store.update(job.id, state=JobState.DONE, transcript=transcript.to_dict())
    client = TestClient(app, base_url="http://127.0.0.1")

    http_default = client.get(f"/jobs/{job.id}/transcript").json()
    http_words = client.get(f"/jobs/{job.id}/transcript", params={"include_words": "true"}).json()
    assert "words" not in http_default["transcript"]["segments"][0]
    assert http_words["transcript"]["segments"][0]["words"][0]["text"] == "source"

    mcp_default = json.loads(get_transcript(job.id, fmt="json")["content"])
    mcp_words = json.loads(get_transcript(job.id, fmt="json", include_words=True)["content"])
    assert "words" not in mcp_default["segments"][0]
    assert mcp_words["segments"][0]["words"][0]["text"] == "source"
    assert mcp_words["segments"][0]["translated_text"] == "translated speech"
    assert store.get(job.id).transcript["segments"][0]["words"][0]["text"] == "source"


def test_http_and_mcp_reject_unavailable_pdf_before_creating_job(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app
    from textflowkit.adapters.mcp_server import transcribe_media
    from textflowkit.core import submission

    def unavailable(formats):
        raise ValueError("PDF export requires the 'export' extra")

    monkeypatch.setattr(submission, "validate_export_requirements", unavailable)
    store = get_default_store()
    http = TestClient(app, base_url="http://127.0.0.1").post("/jobs", json={
        "source": "media.wav", "formats": ["pdf"], "output_dir": str(tmp_path),
    })
    mcp = transcribe_media("media.wav", formats="pdf", output_dir=str(tmp_path))
    assert http.status_code == 422
    assert "export" in http.json()["detail"]
    assert "export" in mcp["error"]
    assert store.list() == []


def test_mcp_search_finds_and_reports_context():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import search_transcript

    job = _seed_done_job(get_default_store(), 10)
    out = search_transcript(job.id, "word5", limit=5, context=1)
    assert out["match_count"] == 1
    m = out["matches"][0]
    assert m["index"] == 5
    assert len(m["context_before"]) == 1
    assert len(m["context_after"]) == 1


def test_mcp_search_empty_query_is_an_error():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import search_transcript

    job = _seed_done_job(get_default_store(), 5)
    assert "error" in search_transcript(job.id, "")


def test_mcp_search_requires_finished_job():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import search_transcript

    store = get_default_store()
    job = store.create("pending")
    assert "error" in search_transcript(job.id, "x")


def test_http_transcript_paging_and_metadata():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    job = _seed_done_job(get_default_store(), 10)
    r = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}/transcript", params={"format": "json", "limit": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["returned"] == 3
    assert body["total_segments"] == 10
    assert body["has_more"] is True


def test_http_transcript_time_range_renders_srt():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    job = _seed_done_job(get_default_store(), 10)
    r = TestClient(app, base_url="http://127.0.0.1").get(
        f"/jobs/{job.id}/transcript", params={"format": "srt", "start": 1.0, "end": 3.0}
    )
    assert r.status_code == 200
    assert r.text.startswith("1")
    assert "word1" in r.text


def test_http_transcript_bad_offset_422():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    job = _seed_done_job(get_default_store(), 5)
    r = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}/transcript", params={"offset": -1})
    assert r.status_code == 422


def test_http_search_endpoint():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    job = _seed_done_job(get_default_store(), 10)
    r = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}/search", params={"q": "word7"})
    assert r.status_code == 200
    assert r.json()["match_count"] == 1


def test_http_search_empty_query_422():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    job = _seed_done_job(get_default_store(), 5)
    r = TestClient(app, base_url="http://127.0.0.1").get(f"/jobs/{job.id}/search", params={"q": ""})
    assert r.status_code == 422


def test_http_export_honours_requested_formats(tmp_path, monkeypatch):
    """Regression: a bare list[str] on a POST is a BODY field in FastAPI, so the
    formats argument was silently ignored and defaults were always written."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app
    from textflowkit.core.paths import ENV_OUTPUT_ROOT

    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(tmp_path))
    job = _seed_done_job(get_default_store(), 3)

    r = TestClient(app, base_url="http://127.0.0.1").post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "txt"], "output_dir": "out"},
    )
    assert r.status_code == 200
    names = sorted(p.split("\\")[-1].split("/")[-1] for p in r.json()["written"])
    assert len(names) == 2
    assert any(n.endswith(".srt") for n in names)
    assert any(n.endswith(".txt") for n in names)
    assert not any(n.endswith(".json") for n in names), "defaults were used instead"
