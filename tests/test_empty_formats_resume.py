"""An empty format list must mean one thing everywhere (U51).

`SubmissionRequest` accepts `formats=[]` (reachable from the MCP tool's
`formats=""` argument) and the pipeline has always read that as its normal
default of JSON/SRT/TXT at run time. The durable request kept the empty list,
though, so the checkpoint written under the default could never match the
request that produced it - resuming the identical request rejected its own
checkpoint. These tests pin the durable request, its `options()`, and the
checkpoint to one another, on both stores and with and without an output
directory. No model, no ffmpeg, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textflowkit.core import pipeline
from textflowkit.core.checkpoint import load_checkpoint
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request

# The pipeline's existing runtime default, spelled out here so the test states
# the meaning rather than importing the implementation it checks.
DEFAULT_FORMATS = ["json", "srt", "txt"]


class _Engine:
    """Deterministic engine: no model, no network."""

    def transcribe(self, audio, *, language=None) -> Transcript:
        return Transcript(
            source=str(audio),
            language=language or "en",
            segments=[Segment(0.0, 1.0, "empty formats")],
        )


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake engine, and a fake decode step: no ffmpeg, no model."""

    def fetch(ref, *, work_dir, **kwargs):
        media = Path(work_dir) / "media.wav"
        media.write_bytes(Path(ref.location).read_bytes())
        return media

    def extract(media, *, work_dir, check_cancel=None):
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(media.read_bytes())
        return audio

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: _Engine())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))


@pytest.mark.parametrize("durable", [False, True], ids=["memory", "sqlite"])
@pytest.mark.parametrize("with_output_dir", [False, True], ids=["no-output-dir", "output-dir"])
def test_empty_formats_request_resumes_its_own_checkpoint(
    tmp_path, fake_pipeline, durable, with_output_dir
):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = SqliteJobStore(tmp_path / "jobs.db") if durable else MemoryJobStore()
    try:
        request = SubmissionRequest(
            source=str(source),
            formats=[],
            model="tiny",
            device="cpu",
            output_dir=str(output_dir) if with_output_dir else None,
        )
        job = submit_request(store, request, background=False)
        assert job.state is JobState.DONE

        # The defect: resume of the identical request rejected the checkpoint
        # this very job wrote.
        resumed = resume_job(store, job.id, background=False)
        assert resumed.id == job.id
        assert resumed.state is JobState.DONE

        # One canonical meaning: what the durable request records is exactly
        # what the run did and what the checkpoint was matched against.
        stored = store.get(job.id)
        assert stored.request["formats"] == DEFAULT_FORMATS
        assert request.options()["formats"] == DEFAULT_FORMATS
        assert load_checkpoint(stored).options == request.options()
    finally:
        store.close()
