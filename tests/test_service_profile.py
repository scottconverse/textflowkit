"""Production HTTP is fail-closed; localhost developer mode remains simple."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from textflowkit.adapters import http_server
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import JobState, get_default_store, reset_default_store
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.service import (
    ServiceConfigurationError,
    enforce_media_limits,
    validate_production_config,
)
from textflowkit.render import atomic_write_bytes


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
    http_server._RATE_BUCKETS.clear()
    yield
    reset_default_executor()
    reset_default_store()
    http_server._RATE_BUCKETS.clear()


def test_production_rejects_missing_security_configuration(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.delenv("TEXTFLOWKIT_API_TOKEN", raising=False)
    with pytest.raises(ServiceConfigurationError, match="TEXTFLOWKIT_API_TOKEN"):
        validate_production_config()


def test_production_requires_token_and_durable_roots(production):
    validate_production_config()
    client = TestClient(http_server.app)
    assert client.get("/health").status_code == 401
    response = client.get(
        "/health", headers={"Authorization": "Bearer a-long-test-token-12345"}
    )
    assert response.status_code == 200


def test_production_rate_and_request_size_limits(production, monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_RATE_PER_MINUTE", "1")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_REQUEST_BYTES", "16")
    client = TestClient(http_server.app)
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    oversized = client.post("/jobs", json={"source": "x" * 100}, headers=headers)
    assert oversized.status_code == 413
    assert client.get("/health", headers=headers).status_code == 200
    assert client.get("/health", headers=headers).status_code == 429


def test_production_streaming_body_is_rejected_before_full_buffer(production, monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_MAX_REQUEST_BYTES", "16")
    chunks_read = 0

    async def receive():
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"x" * 10, "more_body": True}

    async def unexpected_call_next(request):
        raise AssertionError("oversized request reached FastAPI")

    request = http_server.Request(
        {"type": "http", "method": "POST", "path": "/jobs",
         "headers": [(b"authorization", b"Bearer a-long-test-token-12345")],
         "client": ("127.0.0.1", 12345)},
        receive,
    )
    response = asyncio.run(http_server.production_guard(request, unexpected_call_next))
    assert response.status_code == 413
    assert chunks_read == 2


def test_production_transcript_defaults_to_bounded_page(production):
    store = get_default_store()
    transcript = Transcript(source="x", segments=[Segment(i, i + 1, "word") for i in range(150)])
    job = store.create("x")
    store.update(job.id, state=JobState.DONE, transcript=transcript.to_dict())
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    client = TestClient(http_server.app)
    body = client.get(f"/jobs/{job.id}/transcript", headers=headers).json()
    assert body["returned"] == 100
    assert body["has_more"] is True
    assert client.get(
        f"/jobs/{job.id}/transcript", params={"limit": 501}, headers=headers
    ).status_code == 422


def test_production_inline_transcript_obeys_output_cap(production, monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_MAX_OUTPUT_BYTES", "100")
    store = get_default_store()
    transcript = Transcript(source="x", segments=[Segment(0, 1, "long text " * 100)])
    job = store.create("x")
    store.update(job.id, state=JobState.DONE, transcript=transcript.to_dict())
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    client = TestClient(http_server.app)
    assert client.get(f"/jobs/{job.id}/transcript", headers=headers).status_code == 413
    assert client.get(
        f"/jobs/{job.id}/transcript", params={"format": "txt"}, headers=headers
    ).status_code == 413
    assert client.get(
        f"/jobs/{job.id}/search", params={"q": "long"}, headers=headers
    ).status_code == 413


def test_developer_mode_needs_no_token(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    assert TestClient(http_server.app).get("/health").status_code == 200


def test_misspelled_production_profile_fails_closed(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "produciton")
    assert TestClient(http_server.app).get("/health").status_code == 503


def test_production_media_duration_limit(monkeypatch, tmp_path):
    from textflowkit.core import service
    from textflowkit.sources import acquire

    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_DURATION_SECONDS", "1")
    monkeypatch.setattr(acquire, "require_tool", lambda name: "ffprobe")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="2.0", returncode=0,
    ))
    media = tmp_path / "media.mp4"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")
    with pytest.raises(ServiceConfigurationError, match="duration exceeds"):
        enforce_media_limits(media, audio)


def test_http_and_mcp_jobs_reject_known_duration_before_decode(
    production, monkeypatch,
):
    from pathlib import Path

    from textflowkit.adapters import mcp_server
    from textflowkit.core import pipeline
    from textflowkit.sources.acquire import require_tool

    root = Path(os.environ["TEXTFLOWKIT_INPUT_ROOT"])
    media = root / "overlong.mp3"
    ffmpeg = require_tool("ffmpeg")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:a", "mp3", str(media)],
                   capture_output=True, check=True, timeout=30)
    monkeypatch.setenv("TEXTFLOWKIT_MAX_DURATION_SECONDS", "1")
    extract_calls = []
    actual_extract = pipeline.extract_audio

    def counted_extract(*args, **kwargs):
        extract_calls.append(1)
        return actual_extract(*args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_audio", counted_extract)
    http = TestClient(http_server.app).post(
        "/jobs", json={"source": str(media), "formats": ["json"]},
        headers={"Authorization": "Bearer a-long-test-token-12345"},
    )
    assert http.status_code == 202
    mcp = mcp_server.transcribe_media(str(media), formats="json")
    assert "job_id" in mcp
    store = get_default_store()
    ids = [http.json()["id"], mcp["job_id"]]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        jobs = [store.get(job_id) for job_id in ids]
        if all(job is not None and job.state is JobState.ERROR for job in jobs):
            break
        time.sleep(0.02)
    assert all(store.get(job_id).state is JobState.ERROR for job_id in ids)
    assert all("duration exceeds" in store.get(job_id).error for job_id in ids)
    assert extract_calls == []


def test_production_output_size_limit_precedes_write(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_OUTPUT_BYTES", "3")
    path = tmp_path / "out.txt"
    with pytest.raises(ServiceConfigurationError, match="output exceeds"):
        atomic_write_bytes(path, b"four")
    assert not path.exists()
