"""Whistle is the default engine across the shared core, CLI, Python, MCP, HTTP.

These are the W2 regression pins: the *default* for a fresh single and batch
submission is Whistle on every surface, an explicit ``whisper``/``faster-whisper``
(and an explicit Whisper model) is still honoured, a *persisted* request that
omitted the engine decodes as the legacy ``whisper``/``small`` (never migrated to
Whistle), and an option Whistle cannot honour is refused before a job row or an
acquisition exists.

Everything is deterministic and offline: submissions run inline against a stub
``run_job`` so no media is fetched and no model is loaded, and the ``whisper``
package is only ever referenced through fakes installed in ``sys.modules``.
"""

from __future__ import annotations

import sys

import pytest

from textflowkit.core.checkpoint import (
    CHECKPOINT_VERSION,
    CheckpointError,
    CheckpointRecord,
    metadata_only_checkpoint,
    parse_checkpoint,
)
from textflowkit.core.engine import (
    DEFAULT_ENGINE,
    ENGINE_CHOICES,
    engine_default_model,
    get_engine,
    validate_engine,
)
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.sources.detect import SourceRef

LOCAL_PEER = ("127.0.0.1", 50000)


# --- the default itself ----------------------------------------------------


def test_the_default_engine_is_whistle():
    assert DEFAULT_ENGINE == "whistle"
    assert ENGINE_CHOICES[0] == "whistle"
    assert validate_engine("default") == "whistle"


def test_the_default_model_is_resolved_per_engine():
    assert engine_default_model("whistle") == "whistle"
    assert engine_default_model("whisper") == "small"
    assert engine_default_model("faster-whisper") == "small"


def test_get_engine_default_builds_whistle_without_importing_torch_or_whisper():
    """The default engine must not drag a torch/whisper import in."""
    before = set(sys.modules)
    engine = get_engine()
    new = set(sys.modules) - before
    assert engine.name == "whistle"
    assert not any(m == "torch" or m.startswith("torch.") for m in new)
    assert "whisper" not in new


# --- the shared request defaults -------------------------------------------


def test_a_fresh_request_defaults_to_whistle():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(source="https://example.invalid/a.mp3")
    assert request.engine == "whistle"
    assert request.model == "whistle"


def test_an_explicit_whisper_request_keeps_whisper_and_its_model():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(
        source="https://example.invalid/a.mp3", engine="whisper", model="tiny"
    )
    assert request.engine == "whisper"
    assert request.model == "tiny"


def test_an_explicit_whisper_request_without_a_model_defaults_to_small():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(source="https://example.invalid/a.mp3", engine="whisper")
    assert request.engine == "whisper"
    assert request.model == "small"


# --- durable decoding of a legacy persisted request ------------------------


def test_a_persisted_request_that_omitted_the_engine_decodes_as_legacy_whisper():
    """Requests saved before Whistle was the default must not migrate on decode.

    Their work ran on openai-whisper; decoding them as Whistle would silently
    change the engine a resume asks for and break the checkpoint match that
    keeps their completed work reusable.
    """
    from textflowkit.core.submission import SubmissionRequest

    # A legacy row: no `engine` key at all (the older dataclass wrote none).
    legacy = {"source": "https://example.invalid/a.mp3", "model": "small"}
    request = SubmissionRequest.from_dict(legacy)
    assert request.engine == "whisper"
    assert request.model == "small"


def test_a_legacy_request_that_omitted_both_decodes_to_whisper_and_small():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest.from_dict({"source": "https://example.invalid/a.mp3"})
    assert request.engine == "whisper"
    assert request.model == "small"


def test_a_persisted_whistle_request_decodes_unchanged():
    from textflowkit.core.submission import SubmissionRequest

    data = SubmissionRequest(
        source="https://example.invalid/a.mp3"
    ).to_dict()
    assert data["engine"] == "whistle"
    decoded = SubmissionRequest.from_dict(data)
    assert decoded.engine == "whistle"
    assert decoded.model == "whistle"


def test_a_persisted_request_naming_the_default_alias_decodes_as_legacy_whisper():
    """A saved ``engine='default'`` meant openai-whisper when it was written.

    Before Whistle existed, ``default`` resolved to the Whisper engine. That
    alias now resolves to Whistle, so decoding a saved record by *today's*
    meaning would silently change the engine a resume asks for - and, because
    the record also carries Whisper's ``small`` model, would be refused against
    Whistle's single-model list. Durable decoding must follow what the record
    meant: legacy Whisper, ``small``.
    """
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest.from_dict(
        {"source": "https://example.invalid/a.mp3", "engine": "default", "model": "small"}
    )
    assert request.engine == "whisper"
    assert request.model == "small"


def test_a_persisted_default_alias_without_a_model_decodes_as_legacy_whisper_small():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest.from_dict(
        {"source": "https://example.invalid/a.mp3", "engine": "default"}
    )
    assert request.engine == "whisper"
    assert request.model == "small"


def test_a_persisted_openai_whisper_alias_canonicalises_to_whisper():
    """``openai-whisper`` is the Whisper engine under its other name."""
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest.from_dict(
        {"source": "https://example.invalid/a.mp3", "engine": "openai-whisper"}
    )
    assert request.engine == "whisper"
    assert request.model == "small"


def test_a_fresh_request_naming_the_default_alias_persists_canonical_whistle():
    """A *fresh* ``engine='default'`` means the current default, which is Whistle.

    It is also canonicalised on construction, so a durable request never stores
    the alias ``default``: the engine that ran is the engine the record names.
    """
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(source="https://example.invalid/a.mp3", engine="default")
    assert request.engine == "whistle"
    assert request.model == "whistle"
    assert request.to_dict()["engine"] == "whistle"


def test_a_fresh_request_naming_the_openai_whisper_alias_persists_canonical_whisper():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(source="https://example.invalid/a.mp3", engine="openai-whisper")
    assert request.engine == "whisper"
    assert request.model == "small"
    assert request.to_dict()["engine"] == "whisper"


# --- options the default engine cannot honour ------------------------------


@pytest.mark.parametrize("device", ["cuda", "cuda:0", "mps"])
def test_a_non_cpu_device_is_refused_for_the_default_engine(device):
    from textflowkit.core.submission import SubmissionRequest

    with pytest.raises(ValueError, match="Whistle runs on CPU only"):
        SubmissionRequest(source="https://example.invalid/a.mp3", device=device)


def test_an_unsupported_language_is_refused_for_the_default_engine():
    from textflowkit.core.submission import SubmissionRequest

    with pytest.raises(ValueError, match="does not support language"):
        SubmissionRequest(source="https://example.invalid/a.mp3", language="zz")


def test_a_cpu_device_is_accepted_for_the_default_engine():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(source="https://example.invalid/a.mp3", device="cpu")
    assert request.device == "cpu"


def test_a_gpu_device_is_still_accepted_when_whisper_is_named():
    from textflowkit.core.submission import SubmissionRequest

    request = SubmissionRequest(
        source="https://example.invalid/a.mp3", engine="whisper", device="cuda"
    )
    assert request.device == "cuda"


# --- the surfaces: CLI, Python, MCP, HTTP ----------------------------------


@pytest.fixture
def store(monkeypatch):
    jobs = MemoryJobStore()
    from textflowkit.adapters import http_server, mcp_server

    monkeypatch.setattr(http_server, "get_default_store", lambda: jobs)
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: jobs)
    return jobs


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
def local_media(monkeypatch, tmp_path):
    for name in ("media.wav", "one.wav", "two.wav"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.chdir(tmp_path)


def test_python_pipeline_defaults_to_whistle(monkeypatch):
    """A direct Python caller that names no engine gets Whistle, and the model
    it hands the engine is Whistle's own default - not `small`."""
    from textflowkit.core import pipeline

    seen = {}

    def fake_get_engine(name, **kwargs):
        seen["engine"] = name
        seen["model"] = kwargs.get("model")
        raise AssertionError("stop after the engine is resolved")

    # Acquisition must succeed with fakes so execution reaches the engine call.
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: "media.bin")
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: "audio.wav")
    monkeypatch.setattr(pipeline, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        pipeline,
        "resolve_source",
        lambda source: SourceRef(kind="url", location=source, platform="direct"),
    )

    with pytest.raises(AssertionError, match="stop after"):
        pipeline.transcribe("https://example.invalid/a.mp3")
    assert seen["engine"] == "whistle"
    assert seen["model"] == "whistle"


def test_python_pipeline_refuses_an_unsupported_language_before_acquisition(monkeypatch):
    """The same cheap refusal the adapters do, for a direct Python caller.

    Whistle cannot honour `zz`; that is knowable from the arguments, so it must
    fail as a PipelineError with no fetch, no decode, no model load."""
    from textflowkit.core import pipeline

    def explode(*a, **k):
        raise AssertionError("acquisition ran before the option was refused")

    monkeypatch.setattr(pipeline, "require_tool", explode)
    monkeypatch.setattr(pipeline, "fetch_media", explode)
    monkeypatch.setattr(pipeline, "resolve_source", explode)

    with pytest.raises(pipeline.PipelineError, match="does not support language"):
        pipeline.transcribe("https://example.invalid/a.mp3", language="zz")


def test_python_pipeline_refuses_a_non_cpu_device_before_acquisition(monkeypatch):
    from textflowkit.core import pipeline

    def explode(*a, **k):
        raise AssertionError("acquisition ran before the option was refused")

    monkeypatch.setattr(pipeline, "require_tool", explode)
    monkeypatch.setattr(pipeline, "fetch_media", explode)
    monkeypatch.setattr(pipeline, "resolve_source", explode)

    with pytest.raises(pipeline.PipelineError, match="CPU only"):
        pipeline.transcribe("https://example.invalid/a.mp3", device="cuda")


def test_mcp_single_defaults_to_whistle(store, inline_submission, local_media):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav")
    assert "error" not in out, out
    request = store.get(out["job_id"]).request
    assert request["engine"] == "whistle"
    assert request["model"] == "whistle"


def test_mcp_batch_defaults_to_whistle(store, inline_submission, local_media):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "two.wav"])
    assert out["count"] == 2, out
    assert [job["engine"] for job in inline_submission] == ["whistle", "whistle"]


def test_http_single_defaults_to_whistle(store, inline_submission, local_media):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    client = TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)
    response = client.post("/jobs", json={"source": "media.wav"})
    assert response.status_code == 202, response.text
    request = store.get(response.json()["id"]).request
    assert request["engine"] == "whistle"
    assert request["model"] == "whistle"


def test_http_batch_defaults_to_whistle(store, inline_submission, local_media):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    client = TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)
    response = client.post("/jobs/batch", json={"jobs": [{"source": "one.wav"}, {"source": "two.wav"}]})
    assert response.status_code == 202, response.text
    assert [job["engine"] for job in inline_submission] == ["whistle", "whistle"]


def test_mcp_single_rejects_a_whistle_incompatible_language_before_a_job(store, inline_submission, local_media):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import transcribe_media

    out = transcribe_media("media.wav", language="zz")
    assert "error" in out, out
    assert "does not support language" in out["error"], out
    assert store.list() == []
    assert inline_submission == []


def test_http_single_rejects_a_non_cpu_device_for_whistle_before_a_job(
    store, inline_submission, local_media
):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    client = TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)
    response = client.post("/jobs", json={"source": "media.wav", "device": "cuda"})
    assert response.status_code == 422, response.text
    assert "CPU only" in response.json()["detail"]
    assert store.list() == []
    assert inline_submission == []


def test_cli_transcribe_defaults_to_whistle(monkeypatch, tmp_path):
    from textflowkit import cli

    media = tmp_path / "media.wav"
    media.write_bytes(b"x")
    jobs = MemoryJobStore()
    monkeypatch.setattr(cli, "get_default_store", lambda: jobs)

    from textflowkit.core import submission

    monkeypatch.setattr(submission, "run_job", lambda job, store, **kwargs: store.update(
        job.id, state=JobState.DONE, progress="complete",
        transcript={"source": job.source, "segments": []},
    ))
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)

    rc = cli.main(["transcribe", str(media), "--stdout", "--quiet"])
    assert rc == 0
    jobs_list = jobs.list()
    assert len(jobs_list) == 1
    assert jobs_list[0].request["engine"] == "whistle"
    assert jobs_list[0].request["model"] == "whistle"


def test_cli_transcribe_honours_explicit_whisper_and_model(monkeypatch, tmp_path):
    from textflowkit import cli

    media = tmp_path / "media.wav"
    media.write_bytes(b"x")
    jobs = MemoryJobStore()
    monkeypatch.setattr(cli, "get_default_store", lambda: jobs)

    from textflowkit.core import submission

    monkeypatch.setattr(submission, "run_job", lambda job, store, **kwargs: store.update(
        job.id, state=JobState.DONE, progress="complete",
        transcript={"source": job.source, "segments": []},
    ))
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)

    rc = cli.main(["transcribe", str(media), "--engine", "whisper", "--model", "tiny",
                   "--stdout", "--quiet"])
    assert rc == 0
    request = jobs.list()[0].request
    assert request["engine"] == "whisper"
    assert request["model"] == "tiny"


def test_mcp_schema_defaults_to_whistle():
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    tools = mcp._tool_manager._tools
    for name in ("transcribe_media", "submit_batch_media"):
        assert tools[name].parameters["properties"]["engine"]["default"] == "whistle", name


def test_http_schema_defaults_to_whistle():
    pytest.importorskip("fastapi")
    from textflowkit.adapters.http_server import TranscribeRequest

    assert TranscribeRequest.model_fields["engine"].default == "whistle"
    assert TranscribeRequest.model_fields["model"].default is None


# --- dependency hygiene: the default pulls no torch ------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11")
def test_openai_whisper_is_not_a_base_dependency():
    from pathlib import Path

    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]

    assert not any("openai-whisper" in req for req in project["dependencies"])
    assert any(
        req.startswith("openai-whisper")
        for req in project["optional-dependencies"]["whisper"]
    )
    # `all` must include the whisper extra so the engine is installable there.
    assert any(
        "whisper" in req for req in project["optional-dependencies"]["all"]
    )


def test_importing_the_core_pulls_no_torch_or_whisper():
    """A fresh install of the default surface must not need torch at import."""
    import importlib

    import textflowkit  # noqa: F401 - importing the package is the point

    before = set(sys.modules)
    importlib.import_module("textflowkit.core.pipeline")
    importlib.import_module("textflowkit.core.submission")
    new = set(sys.modules) - before
    assert not any(m == "torch" or m.startswith("torch.") for m in new)
    assert "whisper" not in new


# --- v3: the durable partial (unfinished) Whistle record --------------------
#
# v3 adds ``engine_progress`` beside ``transcript``. A *partial* is durable
# per-block Whistle work with no finished transcript; a *complete* record is the
# existing transcript-carrying one. These pins hold the boundary between them,
# including the Whistle-only gate on the partial form.


def _partial_body(*, completed: int = 1, segments=None, **overrides) -> dict:
    body = {
        "schema": "textflowkit.whistle.progress/1",
        "policy": "core=26;context<=2;max=30",
        "wav_identity": "aa" * 32,
        "duration": 66.0,
        "model_sha256": "bb" * 32,
        "binary_sha256": "cc" * 32,
        "language": None,
        "completed_core_index": completed,
        "segments": segments if segments is not None else [
            {"start": 1.0, "end": 1.5, "text": "hello",
             "words": [{"start": 1.0, "end": 1.5, "text": "hello"}]},
        ],
        "total_cores": 3,
    }
    body.update(overrides)
    return body


def _record(*, engine: str = "whistle", version: int = CHECKPOINT_VERSION,
            transcript=None, engine_progress=None, stages=None,
            local_identity=None) -> dict:
    return {
        "version": version,
        "source": "clip.wav",
        "model": "whistle",
        "engine": engine,
        "language": None,
        "options": {},
        "finished_stages": stages if stages is not None else ["source", "fetch", "extract"],
        "transcript": transcript,
        "engine_progress": engine_progress,
        "media_path": None,
        "audio_path": None,
        "local_identity": local_identity,
    }


_TRANSCRIPT = {
    "source": "clip.wav", "language": "en", "engine": "whistle",
    "segments": [{"start": 0.0, "end": 1.0, "text": "hi",
                  "words": [{"start": 0.0, "end": 1.0, "text": "hi"}]}],
    "duration": 66.0, "metadata": {},
}


@pytest.mark.parametrize("version", [1, 2])
def test_a_legacy_or_v2_complete_record_still_parses(version):
    record = CheckpointRecord.from_dict(_record(
        version=version, transcript=_TRANSCRIPT,
        stages=["source", "fetch", "extract", "transcribe"]))
    assert record.version == version
    assert record.transcript is not None
    assert record.engine_progress is None


def test_a_v2_record_with_a_valid_fingerprint_is_not_refused_for_being_v2():
    """The fingerprint arrived in v2; bumping to v3 must not refuse a v2 resume."""
    identity = {"path": "clip.wav", "size": 10, "sha256": "d" * 64}
    record = CheckpointRecord.from_dict(_record(
        version=2, transcript=_TRANSCRIPT,
        stages=["source", "fetch", "extract", "transcribe"],
        local_identity=identity))
    assert record.version == 2
    assert record.local_identity == identity


def test_a_valid_partial_v3_record_parses_and_carries_no_transcript():
    record = CheckpointRecord.from_dict(_record(engine_progress=_partial_body()))
    assert record.version == CHECKPOINT_VERSION
    assert record.transcript is None
    assert record.engine_progress is not None
    assert record.engine_progress["completed_core_index"] == 1


def test_a_partial_record_must_name_the_whistle_engine():
    """The partial body is Whistle's own; a foreign engine name is corruption."""
    with pytest.raises(CheckpointError, match="Whistle-only"):
        CheckpointRecord.from_dict(_record(engine="whisper",
                                           engine_progress=_partial_body()))


def test_a_partial_cannot_be_hydrated_as_a_complete_transcript():
    record = parse_checkpoint(_record(engine_progress=_partial_body()))
    assert record is not None
    assert record.transcript is None
    assert "transcribe" not in record.finished_stages


def test_a_partial_that_claims_transcription_finished_is_refused():
    with pytest.raises(CheckpointError, match="lists transcription as finished"):
        CheckpointRecord.from_dict(_record(
            engine_progress=_partial_body(),
            stages=["source", "fetch", "extract", "transcribe"]))


def test_a_partial_under_an_older_version_is_refused():
    with pytest.raises(CheckpointError, match="cannot carry partial engine progress"):
        CheckpointRecord.from_dict(_record(version=2, engine_progress=_partial_body()))


def test_a_record_with_both_a_transcript_and_a_partial_is_refused():
    with pytest.raises(CheckpointError, match="both a transcript and partial"):
        CheckpointRecord.from_dict(_record(
            transcript=_TRANSCRIPT, engine_progress=_partial_body(),
            stages=["source", "fetch", "extract", "transcribe"]))


@pytest.mark.parametrize("mutate, needle", [
    (lambda b: b.pop("segments"), "segment list"),
    (lambda b: b.__setitem__("segments", "nope"), "segment list"),
    (lambda b: b.__setitem__("duration", 0.0), "duration"),
    (lambda b: b.__setitem__("duration", -1.0), "duration"),
    (lambda b: b.__setitem__("duration", "soon"), "duration"),
    (lambda b: b.__setitem__("language", ["en"]), "language"),
    (lambda b: b.__setitem__("completed_core_index", -1), "completed"),
    (lambda b: b.__setitem__("schema", "other/9"), "schema"),
    (lambda b: b.pop("policy"), "window policy"),
    (lambda b: b.__setitem__("wav_identity", "short"), "audio identity"),
])
def test_a_malformed_partial_body_is_refused(mutate, needle):
    body = _partial_body()
    mutate(body)
    with pytest.raises(CheckpointError, match=needle):
        CheckpointRecord.from_dict(_record(engine_progress=body))


def test_a_partial_with_a_missing_transcript_and_missing_progress_is_refused():
    with pytest.raises(CheckpointError, match="missing a transcript"):
        CheckpointRecord.from_dict(_record())


def test_metadata_only_checkpoint_drops_both_bodies():
    """A DONE row stores its words once: both the transcript and any partial go."""
    stored = _record(transcript=_TRANSCRIPT, stages=["source", "transcribe"],
                     engine_progress=None)
    reduced = metadata_only_checkpoint(stored)
    assert reduced is not None
    assert "transcript" not in reduced
    assert "engine_progress" not in reduced
    assert reduced["source"] == "clip.wav"


def test_metadata_only_checkpoint_returns_none_when_no_body_present():
    assert metadata_only_checkpoint(_record(transcript=None, engine_progress=None)) is None
