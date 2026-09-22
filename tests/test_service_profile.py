"""Production HTTP is fail-closed; localhost developer mode remains simple."""

from __future__ import annotations

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


def test_production_output_size_limit_precedes_write(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_OUTPUT_BYTES", "3")
    path = tmp_path / "out.txt"
    with pytest.raises(ServiceConfigurationError, match="output exceeds"):
        atomic_write_bytes(path, b"four")
    assert not path.exists()
