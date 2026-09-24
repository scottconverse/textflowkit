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

# Developer HTTP refuses a peer it cannot judge, and `TestClient`'s default peer
# is the non-address `testclient`; the developer-mode test below declares the
# loopback peer a real local caller has. No socket is opened.
LOCAL_PEER = ("127.0.0.1", 50000)


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
    # Clear the sweep guard too, so a test never inherits another test's bound.
    monkeypatch.setattr(http_server, "_next_expiry", float("inf"), raising=False)
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


def _seed_buckets(prefix: str, count: int, started: float, counter: int = 1) -> None:
    """Fill the live rate table with synthetic buckets, bypassing HTTP."""
    for index in range(count):
        http_server._RATE_BUCKETS[f"{prefix}{index}"] = (started, counter)
    # Writing the table directly bypasses the sweep guard, so rearm it here.
    http_server._next_expiry = min(
        getattr(http_server, "_next_expiry", float("inf")),
        started + http_server.RATE_BUCKET_TTL_SECONDS,
    )


@pytest.fixture
def rate_table(monkeypatch):
    """An isolated rate table whose sweep guard starts unset."""
    http_server._RATE_BUCKETS.clear()
    monkeypatch.setattr(http_server, "_next_expiry", float("inf"), raising=False)
    yield http_server._RATE_BUCKETS
    http_server._RATE_BUCKETS.clear()


def test_production_rate_capacity_preserves_active_counters(production, monkeypatch):
    """A 10,001st peer must not erase counters that are still counting down."""
    monkeypatch.setenv("TEXTFLOWKIT_RATE_PER_MINUTE", "1")
    capacity = http_server.RATE_BUCKET_CAPACITY
    now = time.monotonic()
    # One live peer already over its limit, and a table filled to capacity.
    http_server._RATE_BUCKETS["testclient"] = (now, 1)
    _seed_buckets("10.0.", capacity - 1, now)
    assert len(http_server._RATE_BUCKETS) == capacity
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    strangers = TestClient(http_server.app, client=("10.9.9.9", 4000))

    # Capacity is fail-closed for an untracked peer: it is refused, not evicted.
    assert strangers.get("/health", headers=headers).status_code == 429
    # And the tracked peer's counter survived, so its limit still holds.
    assert TestClient(http_server.app).get("/health", headers=headers).status_code == 429
    assert len(http_server._RATE_BUCKETS) == capacity
    assert http_server._RATE_BUCKETS["testclient"] == (now, 1)


def test_production_rate_buckets_expire_and_are_reclaimed(production, monkeypatch):
    """Aged-out buckets are garbage: reclaimed by age, not by wiping the table."""
    monkeypatch.setenv("TEXTFLOWKIT_RATE_PER_MINUTE", "1")
    capacity = http_server.RATE_BUCKET_CAPACITY
    stale = time.monotonic() - http_server.RATE_BUCKET_TTL_SECONDS - 1
    _seed_buckets("10.0.", capacity, stale)
    assert len(http_server._RATE_BUCKETS) == capacity
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    fresh = TestClient(http_server.app, client=("10.9.9.9", 4000))

    assert fresh.get("/health", headers=headers).status_code == 200
    assert set(http_server._RATE_BUCKETS) == {"10.9.9.9"}
    # The admitted peer's window is real, so its next request is over the limit.
    assert fresh.get("/health", headers=headers).status_code == 429


def test_rate_refusal_keeps_live_peers_when_table_is_full(rate_table):
    capacity = http_server.RATE_BUCKET_CAPACITY
    _seed_buckets("10.0.", capacity, 1000.0)
    # A brand-new address is refused while every tracked bucket is still live.
    assert http_server._rate_refusal("203.0.113.7", 1000.0, 1) == "rate capacity reached"
    # A tracked peer keeps its own per-minute behaviour, unerased by the above.
    assert http_server._rate_refusal("10.0.0", 1000.0, 1) == "rate limit exceeded"
    assert http_server._rate_refusal("10.0.0", 1000.0, 2) is None
    assert len(http_server._RATE_BUCKETS) == capacity
    assert http_server._RATE_BUCKETS["10.0.0"] == (1000.0, 2)


def test_rate_refusal_bounds_table_under_address_flood(rate_table):
    """Distinct live addresses cannot grow the table past capacity."""
    capacity = http_server.RATE_BUCKET_CAPACITY
    _seed_buckets("10.0.", capacity, 1000.0)
    for index in range(50):
        assert http_server._rate_refusal(f"203.0.113.{index}", 1000.0, 1) == (
            "rate capacity reached"
        )
    assert len(http_server._RATE_BUCKETS) == capacity


def test_rate_refusal_reclaims_expired_buckets_by_age(rate_table):
    capacity = http_server.RATE_BUCKET_CAPACITY
    ttl = http_server.RATE_BUCKET_TTL_SECONDS
    _seed_buckets("10.0.", capacity, 1000.0)
    later = 1000.0 + ttl
    for index in range(25):
        assert http_server._rate_refusal(f"203.0.113.{index}", later, 1) is None
    assert len(http_server._RATE_BUCKETS) == 25
    assert not [peer for peer in http_server._RATE_BUCKETS if peer.startswith("10.0.")]


def test_rate_refusal_restarts_expired_peer_window_in_place(rate_table):
    ttl = http_server.RATE_BUCKET_TTL_SECONDS
    http_server._RATE_BUCKETS["203.0.113.9"] = (1000.0, 1)
    # The old window has closed, so the peer gets a fresh one and a fresh count.
    assert http_server._rate_refusal("203.0.113.9", 1000.0 + ttl, 1) is None
    assert http_server._RATE_BUCKETS["203.0.113.9"] == (1000.0 + ttl, 1)
    assert http_server._rate_refusal("203.0.113.9", 1000.0 + ttl, 1) == "rate limit exceeded"


def test_rate_refusal_admits_new_peer_once_oldest_bucket_expires(rate_table, monkeypatch):
    """A sweep may be deferred only while nothing has actually expired."""
    capacity = http_server.RATE_BUCKET_CAPACITY
    ttl = http_server.RATE_BUCKET_TTL_SECONDS
    # Ten thousand live buckets at t=1000, except one that expires at t=1001.
    _seed_buckets("10.0.", capacity - 1, 1000.0)
    _seed_buckets("10.0.oldest.", 1, 1000.0 - (ttl - 1))
    assert len(http_server._RATE_BUCKETS) == capacity
    sweeps: list[float] = []
    real_sweep = http_server._sweep_expired_buckets

    def counted(now: float) -> None:
        sweeps.append(now)
        real_sweep(now)

    monkeypatch.setattr(http_server, "_sweep_expired_buckets", counted)
    # At t=1000 that bucket is still inside its window, so refusing is correct
    # and a scan would be wasted.
    assert http_server._rate_refusal("203.0.113.1", 1000.0, 1) == "rate capacity reached"
    assert sweeps == []
    # At t=1001 it has expired, and a deferred sweep must not keep refusing.
    assert http_server._rate_refusal("203.0.113.1", 1000.0 + 1.0, 1) is None
    assert sweeps == [1000.0 + 1.0]
    assert "10.0.oldest.0" not in http_server._RATE_BUCKETS
    assert len(http_server._RATE_BUCKETS) == capacity


def test_rate_refusal_does_not_sweep_a_full_live_table(rate_table, monkeypatch):
    """A full table of live peers must not cost a scan per request."""
    capacity = http_server.RATE_BUCKET_CAPACITY
    _seed_buckets("10.0.", capacity, 1000.0)
    sweeps: list[float] = []
    real_sweep = http_server._sweep_expired_buckets

    def counted(now: float) -> None:
        sweeps.append(now)
        real_sweep(now)

    monkeypatch.setattr(http_server, "_sweep_expired_buckets", counted)
    for index in range(50):
        assert http_server._rate_refusal(f"203.0.113.{index}", 1000.0, 1) == (
            "rate capacity reached"
        )
    assert sweeps == []
    assert len(http_server._RATE_BUCKETS) == capacity


def test_rate_refusal_relearns_guard_after_direct_seeding(rate_table):
    """A table written behind the guard's back is relearned, not trusted."""
    capacity = http_server.RATE_BUCKET_CAPACITY
    for index in range(capacity):
        http_server._RATE_BUCKETS[f"10.0.{index}"] = (1000.0, 1)
    http_server._next_expiry = float("inf")  # guard not maintained by the writer
    # The bound is unknown, so the expired buckets are reclaimed rather than
    # skipped on the strength of a stale "nothing can have expired" claim.
    assert http_server._rate_refusal("203.0.113.1", 2000.0, 1) is None
    assert set(http_server._RATE_BUCKETS) == {"203.0.113.1"}


def test_rate_refusal_rearms_after_a_window_restart(rate_table):
    """A peer restarting its window must not blind the next reclamation."""
    capacity = http_server.RATE_BUCKET_CAPACITY
    ttl = http_server.RATE_BUCKET_TTL_SECONDS
    _seed_buckets("10.0.", capacity - 1, 2000.0)
    _seed_buckets("10.0.oldest.", 1, 1000.0)
    # At t=1100 the oldest bucket has expired and the tracked peer restarts.
    assert http_server._rate_refusal("10.0.oldest.0", 1000.0 + 100.0, 1) is None
    assert http_server._RATE_BUCKETS["10.0.oldest.0"] == (1000.0 + 100.0, 1)
    # A new peer is still refused: the remaining buckets are live until t=2060.
    assert http_server._rate_refusal("203.0.113.1", 1000.0 + 100.0, 1) == (
        "rate capacity reached"
    )
    assert len(http_server._RATE_BUCKETS) == capacity
    # And reclamation still happens exactly when those buckets expire.
    assert http_server._rate_refusal("203.0.113.1", 2000.0 + ttl, 1) is None


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
    client = TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER)
    assert client.get("/health").status_code == 200


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
