"""Submission-time refusal of unusable models, outputs, and inputs.

The shared contract is the only place every surface passes through before a job
record exists or a queue slot is taken, so a request that cannot succeed must be
refused there rather than at the point of use. These tests pin the three
refusals the directive names:

- a ``model`` that is not a name the selected engine publishes (paths included),
- an explicit ``output_dir`` outside ``TEXTFLOWKIT_OUTPUT_ROOT``,
- a local ``source`` that does not exist.

Each is checked through the real HTTP and MCP adapters, because the *surface*
behaviour (422, ``{"error": ...}``, no job) is the contract, not the exception
type. Validation must stay cheap: no model weights are loaded and no directory
is created.

Nothing here reaches a network, a worker, or a real pipeline:

- a URL source is only ever the *source* of a request that is refused during
  construction, so no adapter ever gets the chance to fetch it;
- the positive cases are either construction-only or submitted with
  ``background=False`` against a stubbed ``run_job``;
- and, because an invalid model is still accepted by the product code today, the
  HTTP and MCP negative tests also replace the adapter's ``submit_request`` and
  ``submit_batch`` bindings with a function that raises immediately. If the new
  validation fails to reject the request, the test fails with a clear assertion
  instead of queueing a real job that would fetch a URL on the network.
"""

from __future__ import annotations

import pytest

from textflowkit.adapters import http_server, mcp_server
from textflowkit.core import submission
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.submission import SubmissionRequest, submit_request

# See tests/test_submission.py: TestClient's default peer is not an address the
# developer HTTP surface will judge, so the calls below declare loopback.
LOCAL_PEER = ("127.0.0.1", 50000)
HTTP = "http://127.0.0.1"


@pytest.fixture
def store(monkeypatch):
    """A shared in-memory store wired into both adapters.

    ``get_default_store`` is process-wide, so patching it here is what makes "no
    job was created" an observation about the surface under test rather than
    about whatever store the process happened to have.
    """
    jobs = MemoryJobStore()
    monkeypatch.setattr(http_server, "get_default_store", lambda: jobs)
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: jobs)
    return jobs


@pytest.fixture
def no_work(monkeypatch):
    """Stub the one place a submission could start doing work.

    ``run_job`` is the core's entry into acquisition and inference; replacing it
    means a test that expects *acceptance* still never contacts a network or a
    model. Refusals never get this far, so this is a net for the positive cases
    and for anything that slips past them.
    """
    ran: list[str] = []

    def record(job, jobs, **kwargs):
        ran.append(job.source)
        jobs.update(
            job.id, state=JobState.DONE, progress="complete",
            transcript=Transcript(source=job.source, segments=[Segment(0, 1, "stub")]).to_dict(),
        )

    monkeypatch.setattr(submission, "run_job", record)
    return ran


@pytest.fixture
def no_reach(monkeypatch):
    """Make reaching the adapters' submit bindings a loud, instant failure.

    The model check is the thing under test here and the product does not have it
    yet, so without this an HTTP/MCP negative case would queue a real job whose
    worker fetches a URL. Raising ``AssertionError`` from the *adapter* binding is
    what keeps the test a fast, deterministic failure rather than a network hang,
    while still letting the surface's own 422/error mapping be asserted.
    """
    def explode(*args, **kwargs):
        raise AssertionError("validation failed to reject request")

    monkeypatch.setattr(http_server, "submit_request", explode)
    monkeypatch.setattr(http_server, "submit_batch", explode)
    monkeypatch.setattr(mcp_server, "submit_request", explode)


def _post(source: str, **body) -> object:
    return _client().post("/jobs", json={"source": source, "formats": ["txt"], **body})


def _client():
    from fastapi.testclient import TestClient

    return TestClient(http_server.app, base_url=HTTP, client=LOCAL_PEER)


# --- the positive case the refusals must not narrow -------------------------

def test_real_names_and_local_files_are_still_accepted(tmp_path, no_work):
    """Every shape that is genuinely usable must survive the new checks."""
    media = tmp_path / "clip.wav"
    media.write_bytes(b"not really audio, but present")
    accepted = [
        # Default engine, model names openai-whisper itself publishes.
        {"source": str(media), "model": "tiny"},
        {"source": str(media), "model": "small.en"},
        # Explicit engine name and its alias.
        {"source": str(media), "model": "base", "engine": "whisper"},
        {"source": str(media), "model": "base", "engine": "openai-whisper"},
        {"source": str(media), "model": "small"},
    ]
    store = MemoryJobStore()
    for body in accepted:
        job = submit_request(store, SubmissionRequest(**body), background=False)
        assert job.state is JobState.DONE
    assert len(store.list()) == len(accepted)


def test_a_url_is_not_stat_ed_as_a_local_file():
    """A URL names bytes elsewhere; it is never a local path to check.

    The request is built but never submitted, so nothing is fetched.
    """
    request = SubmissionRequest(source="https://example.invalid/never-fetched.mp3")
    assert request.source.startswith("https://")


# --- model validation -------------------------------------------------------

def test_a_readable_local_model_path_is_rejected(tmp_path):
    """A path to a checkpoint is not a model this contract will run.

    It is readable and plausibly valid to a human, which is exactly why the
    refusal has to be explicit: the directive requires model *paths* rejected.
    """
    weights = tmp_path / "my-finetune.pt"
    weights.write_bytes(b"synthetic weights")
    media = tmp_path / "clip.wav"
    media.write_bytes(b"present")
    with pytest.raises(ValueError, match="model"):
        SubmissionRequest(source=str(media), model=str(weights))


@pytest.mark.parametrize("bad", ["smal", "gpt-4", "does-not-exist.pt"])
def test_http_refuses_unknown_model_without_creating_a_job(store, no_reach, bad):
    response = _post("https://example.invalid/a.mp3", model=bad)
    assert response.status_code == 422
    assert bad in response.json()["detail"]
    assert store.list() == []


def test_http_refuses_unknown_model_while_the_engine_extra_is_absent(store, no_reach):
    """The engine name is checked on every box, installed extra or not.

    faster-whisper is an optional extra that is not installed here, so a typo in
    the engine's own model names must still be refused as a request error (422)
    rather than surfacing later as a missing-package failure.
    """
    response = _post(
        "https://example.invalid/a.mp3", model="not-a-ct2-model", engine="faster-whisper",
    )
    assert response.status_code == 422
    assert store.list() == []


def test_http_refuses_a_local_model_path_without_creating_a_job(store, no_reach, tmp_path):
    weights = tmp_path / "my-finetune.pt"
    weights.write_bytes(b"synthetic weights")
    response = _post("https://example.invalid/a.mp3", model=str(weights))
    assert response.status_code == 422
    assert store.list() == []


def test_http_refuses_an_out_of_root_output_dir_without_creating_a_job(store, no_reach, tmp_path, monkeypatch):
    inside = tmp_path / "root"
    inside.mkdir()
    outside = tmp_path / "outside"
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(inside))
    response = _post("https://example.invalid/a.mp3", output_dir=str(outside))
    assert response.status_code == 422
    assert store.list() == []
    assert not outside.exists()


def test_http_refuses_a_missing_local_source_without_creating_a_job(store, no_reach, tmp_path):
    response = _post(str(tmp_path / "absent.mp4"))
    assert response.status_code == 422
    assert store.list() == []


def test_http_refuses_a_missing_local_source_when_output_is_inside_a_confined_root(
    store, no_reach, tmp_path, monkeypatch,
):
    """With a configured output root the refusal must still be a 422.

    ``resolve_input_path`` reports a missing file in a way the surfaces must
    recognise as a request error rather than an internal one. A confined
    deployment that only catches ``ValueError`` would turn a missing input into a
    500, so this pins the boundary that matters operationally: the output root is
    set, the source does not exist, and the surface must still say 422 with no job.
    """
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    response = _post(str(tmp_path / "absent.mp4"))
    assert response.status_code == 422
    assert store.list() == []


def test_mcp_refuses_unknown_model(store, no_reach):
    result = mcp_server.transcribe_media("https://example.invalid/a.mp3", model="smal")
    assert "error" in result
    assert store.list() == []


def test_mcp_refuses_a_local_model_path(store, no_reach, tmp_path):
    weights = tmp_path / "my-finetune.pt"
    weights.write_bytes(b"synthetic weights")
    result = mcp_server.transcribe_media("https://example.invalid/a.mp3", model=str(weights))
    assert "error" in result
    assert store.list() == []


def test_mcp_refuses_missing_local_source(store, no_reach, tmp_path):
    result = mcp_server.transcribe_media(str(tmp_path / "absent.mp4"))
    assert "error" in result
    assert store.list() == []


# --- model validation must not load weights ---------------------------------

def test_model_validation_loads_no_weights(monkeypatch, tmp_path):
    """Checking a name must not cost a model load.

    ``whisper.load_model`` is replaced with a bomb rather than a counter: a
    validation that took the cheap route by loading the model and reading its
    errors would be caught here instead of quietly passing.
    """
    whisper = pytest.importorskip("whisper")

    def explode(*args, **kwargs):
        raise AssertionError("validation must not load model weights")

    monkeypatch.setattr(whisper, "load_model", explode)
    media = tmp_path / "clip.wav"
    media.write_bytes(b"present")
    assert SubmissionRequest(source=str(media), model="tiny").model == "tiny"


# --- output_dir confinement -------------------------------------------------

def test_output_dir_outside_the_root_is_refused_without_creating_it(tmp_path, monkeypatch):
    inside = tmp_path / "root"
    inside.mkdir()
    outside = tmp_path / "outside"
    media = tmp_path / "clip.wav"
    media.write_bytes(b"present")
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(inside))
    with pytest.raises(ValueError, match="outside the allowed root"):
        SubmissionRequest(source=str(media), output_dir=str(outside))
    assert not outside.exists()


def test_output_dir_inside_the_root_is_accepted(tmp_path, monkeypatch):
    inside = tmp_path / "root"
    inside.mkdir()
    media = tmp_path / "clip.wav"
    media.write_bytes(b"present")
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(inside))
    nested = inside / "nested" / "deep"
    request = SubmissionRequest(source=str(media), output_dir=str(nested))
    assert request.output_dir == str(nested)
    # Resolving must not create anything: the pipeline owns directory creation.
    assert not nested.exists()


# --- local source existence -------------------------------------------------

def test_missing_local_source_is_refused(tmp_path):
    with pytest.raises(ValueError):
        SubmissionRequest(source=str(tmp_path / "absent.mp4"))


def test_existing_local_source_is_accepted(tmp_path):
    media = tmp_path / "present.wav"
    media.write_bytes(b"source bytes")
    request = SubmissionRequest(source=str(media))
    assert request.source == str(media)


# --- the CLI surface --------------------------------------------------------

def test_cli_reports_a_missing_source_and_creates_no_job(monkeypatch, tmp_path, capsys):
    from textflowkit import cli

    jobs = MemoryJobStore()
    monkeypatch.setattr(cli, "get_default_store", lambda: jobs)
    assert cli.main(["transcribe", str(tmp_path / "absent.mp4"), "--quiet"]) != 0
    assert jobs.list() == []
    capsys.readouterr()
