"""The opt-in `faster-whisper` engine must be reachable through every adapter.

U42 wired the engine into the CLI and the Python API. The adapters are a separate
contract: a caller who reaches the same core through MCP or HTTP must be able to
name the engine, must still get `whisper` when they do not, must get the choice
recorded on the durable request so a resume keeps it, and must be refused *before*
a job row or an acquisition exists when the name is unknown or the optional
package is absent.

Everything here is deterministic. The `faster_whisper` package is faked with a
module object in `sys.modules` (no download, no network), and a missing extra is
simulated by putting `None` there, which makes `import faster_whisper` raise
ImportError regardless of the environment.

Submissions are run inline: `submission.get_default_executor` is patched to None
so `submit_request` takes its synchronous route, and `submission.run_job` is
replaced by a recorder that finishes the job without acquiring media, decoding
audio, or loading a model. The adapter request-building and the durable job row
are the real ones.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from textflowkit.core.jobs import JobState, get_default_store

# Developer HTTP refuses a peer it cannot judge, and `TestClient`'s default peer
# is the non-address `testclient`. Every HTTP test here means "a local caller".
LOCAL_PEER = ("127.0.0.1", 50000)

MISSING_HINT = "textflowkit[faster-whisper]"


@pytest.fixture(autouse=True)
def clean_store():
    get_default_store().clear()
    yield
    get_default_store().clear()


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """The optional package appears importable without being installed."""
    monkeypatch.setitem(sys.modules, "faster_whisper", ModuleType("faster_whisper"))


@pytest.fixture
def missing_faster_whisper(monkeypatch):
    """`import faster_whisper` raises ImportError even if the package is present."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


@pytest.fixture
def inline_submission(monkeypatch):
    """Run submissions inline and record the request dict each job carried."""
    from textflowkit.core import submission
    from textflowkit.core.model import Transcript

    ran: list[dict] = []

    def record(job, store, **kwargs):
        ran.append(dict(job.request or {}))
        store.update(
            job.id, state=JobState.DONE, progress="complete",
            transcript=Transcript(source=job.source, segments=[]).to_dict(),
        )

    monkeypatch.setattr(submission, "run_job", record)
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)
    return ran


@pytest.fixture
def http_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    return TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)


# --------------------------------------------------------------------------
# Published tool / request contracts
# --------------------------------------------------------------------------


def test_mcp_tool_schemas_publish_an_engine_parameter():
    """A harness reads `tools/list`, not this repository, so the schema is the doc."""
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    tools = mcp._tool_manager._tools
    for name in ("transcribe_media", "submit_batch_media"):
        properties = tools[name].parameters["properties"]
        assert "engine" in properties, f"{name} schema omits engine"
        assert properties["engine"]["default"] == "whisper", name


def test_http_request_schema_publishes_engine_with_a_whisper_default():
    pytest.importorskip("fastapi")
    from textflowkit.adapters.http_server import TranscribeRequest, app

    assert TranscribeRequest.model_fields["engine"].default == "whisper"
    schema = app.openapi()["components"]["schemas"]["TranscribeRequest"]["properties"]
    assert schema["engine"]["default"] == "whisper"


# --------------------------------------------------------------------------
# MCP: selection, default, and refusal
# --------------------------------------------------------------------------


def test_mcp_transcribe_media_selects_faster_whisper(fake_faster_whisper, inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav", engine="faster-whisper")

    assert "error" not in out, out
    assert get_default_store().get(out["job_id"]).request["engine"] == "faster-whisper"


def test_mcp_transcribe_media_defaults_to_whisper(inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav")

    assert "error" not in out, out
    assert get_default_store().get(out["job_id"]).request["engine"] == "whisper"


def test_mcp_submit_batch_media_selects_faster_whisper(fake_faster_whisper, inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "two.wav"], engine="faster-whisper")

    assert out["count"] == 2, out
    assert [job["engine"] for job in inline_submission] == ["faster-whisper"] * 2


def test_mcp_submit_batch_media_defaults_to_whisper(inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    assert submit_batch_media(["one.wav"])["count"] == 1
    assert [job["engine"] for job in inline_submission] == ["whisper"]


def test_mcp_rejects_an_unknown_engine_before_creating_a_job(inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav", engine="gpt-9-whisper")

    assert "unknown engine" in out["error"], out
    assert get_default_store().list() == []
    assert inline_submission == []


def test_mcp_rejects_a_missing_extra_before_creating_a_job(
    missing_faster_whisper, inline_submission
):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav", engine="faster-whisper")

    assert MISSING_HINT in out["error"], out
    assert "pip install" in out["error"]
    assert get_default_store().list() == []


def test_mcp_batch_rejects_an_unknown_engine_without_queuing_anything(inline_submission):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "two.wav"], engine="gpt-9-whisper")

    assert "unknown engine" in out["error"], out
    assert get_default_store().list() == []
    assert inline_submission == []


def test_mcp_batch_rejects_a_missing_extra_without_queuing_anything(
    missing_faster_whisper, inline_submission
):
    """A missing extra is a whole-request condition: no half-queued batch."""
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "two.wav"], engine="faster-whisper")

    assert MISSING_HINT in out["error"], out
    assert get_default_store().list() == []
    assert inline_submission == []


# --------------------------------------------------------------------------
# HTTP single
# --------------------------------------------------------------------------


def test_http_single_selects_faster_whisper(fake_faster_whisper, inline_submission, http_client):
    response = http_client.post(
        "/jobs", json={"source": "media.wav", "engine": "faster-whisper"}
    )

    assert response.status_code == 202, response.text
    assert get_default_store().get(response.json()["id"]).request["engine"] == "faster-whisper"


def test_http_single_defaults_to_whisper(inline_submission, http_client):
    response = http_client.post("/jobs", json={"source": "media.wav"})

    assert response.status_code == 202, response.text
    assert get_default_store().get(response.json()["id"]).request["engine"] == "whisper"


def test_http_single_unknown_engine_is_422_before_a_job_row(inline_submission, http_client):
    response = http_client.post(
        "/jobs", json={"source": "media.wav", "engine": "gpt-9-whisper"}
    )

    assert response.status_code == 422, response.text
    assert "unknown engine" in response.json()["detail"]
    assert get_default_store().list() == []
    assert inline_submission == []


def test_http_single_missing_extra_is_422_before_a_job_row(
    missing_faster_whisper, inline_submission, http_client
):
    response = http_client.post(
        "/jobs", json={"source": "media.wav", "engine": "faster-whisper"}
    )

    assert response.status_code == 422, response.text
    assert MISSING_HINT in response.json()["detail"]
    assert get_default_store().list() == []
    assert inline_submission == []


# --------------------------------------------------------------------------
# HTTP batch
# --------------------------------------------------------------------------


def test_http_batch_selects_faster_whisper(fake_faster_whisper, inline_submission, http_client):
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "engine": "faster-whisper"},
        {"source": "two.wav", "engine": "faster-whisper"},
    ]})

    assert response.status_code == 202, response.text
    assert [job["engine"] for job in inline_submission] == ["faster-whisper"] * 2


def test_http_batch_defaults_to_whisper(inline_submission, http_client):
    response = http_client.post("/jobs/batch", json={"jobs": [{"source": "one.wav"}]})

    assert response.status_code == 202, response.text
    assert [job["engine"] for job in inline_submission] == ["whisper"]


def test_http_batch_unknown_engine_is_422_with_no_partial_queue(
    inline_submission, http_client
):
    """One unusable engine name rejects the request, not half of it."""
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "engine": "gpt-9-whisper"},
        {"source": "two.wav"},
    ]})

    assert response.status_code == 422, response.text
    assert "unknown engine" in response.json()["detail"]
    assert get_default_store().list() == []
    assert inline_submission == []


def test_http_batch_missing_extra_is_422_with_no_partial_queue(
    missing_faster_whisper, inline_submission, http_client
):
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "engine": "faster-whisper"},
        {"source": "two.wav"},
    ]})

    assert response.status_code == 422, response.text
    assert MISSING_HINT in response.json()["detail"]
    assert get_default_store().list() == []
    assert inline_submission == []


# --------------------------------------------------------------------------
# The choice is part of the saved request, so it survives a resume
# --------------------------------------------------------------------------


def test_http_engine_choice_survives_the_stored_request_and_a_resume(
    fake_faster_whisper, inline_submission, http_client
):
    created = http_client.post(
        "/jobs", json={"source": "https://example.invalid/media", "engine": "faster-whisper"}
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert get_default_store().get(job_id).request["engine"] == "faster-whisper"

    resumed = http_client.post(f"/jobs/{job_id}/resume")

    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["id"] == job_id, "resume did not match the saved engine"
    assert get_default_store().get(job_id).request["engine"] == "faster-whisper"


def test_mcp_engine_choice_survives_the_stored_request_and_a_resume(
    fake_faster_whisper, inline_submission
):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import resume_job, transcribe_media

    started = transcribe_media("https://example.invalid/media", engine="faster-whisper")
    assert "error" not in started, started
    job_id = started["job_id"]
    assert get_default_store().get(job_id).request["engine"] == "faster-whisper"

    resumed = resume_job(job_id)

    assert "error" not in resumed, resumed
    assert resumed["job_id"] == job_id, "resume did not match the saved engine"
    assert get_default_store().get(job_id).request["engine"] == "faster-whisper"


def test_resume_reuses_a_completed_transcript_after_the_extra_is_removed(
    monkeypatch, inline_submission, http_client
):
    """Reuse is decided before the engine is needed, so losing the extra is safe.

    The availability check sits on the paths that create or queue work, not at
    request construction. A finished job is answered from its stored transcript
    without touching an engine, so uninstalling the optional package must not
    turn an otherwise valid resume into a refusal.
    """
    monkeypatch.setitem(sys.modules, "faster_whisper", ModuleType("faster_whisper"))
    created = http_client.post(
        "/jobs", json={"source": "https://example.invalid/media", "engine": "faster-whisper"}
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]

    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # the extra is gone
    resumed = http_client.post(f"/jobs/{job_id}/resume")

    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["id"] == job_id
    assert get_default_store().get(job_id).request["engine"] == "faster-whisper"


# --------------------------------------------------------------------------
# Direct Python API preflights before acquisition
# --------------------------------------------------------------------------


def _explode_acquisition(monkeypatch):
    from textflowkit.core import pipeline

    def explode(*args, **kwargs):
        raise AssertionError("acquisition or inference ran before the engine check")

    for name in (
        "fetch_media",
        "stage_confined_local_media",
        "extract_audio",
        "require_tool",
        "get_engine",
    ):
        monkeypatch.setattr(pipeline, name, explode)


def test_direct_python_transcribe_rejects_an_unknown_engine_before_fetching(
    monkeypatch, tmp_path
):
    from textflowkit.core import pipeline

    _explode_acquisition(monkeypatch)
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"x")

    with pytest.raises(pipeline.PipelineError, match="unknown engine"):
        pipeline.transcribe(str(source), engine="gpt-9-whisper")


def test_direct_python_transcribe_rejects_a_missing_extra_before_fetching(
    monkeypatch, missing_faster_whisper, tmp_path
):
    from textflowkit.core import pipeline

    _explode_acquisition(monkeypatch)
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"x")

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.transcribe(str(source), engine="faster-whisper")

    assert MISSING_HINT in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


def test_direct_python_transcribe_without_an_engine_still_reports_the_source(
    tmp_path,
):
    """The preflight must not shadow the pipeline's normal first failure."""
    from textflowkit.core import pipeline

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.transcribe(str(tmp_path / "absent.wav"))

    assert "unknown engine" not in str(excinfo.value)
    assert "absent.wav" in str(excinfo.value)
