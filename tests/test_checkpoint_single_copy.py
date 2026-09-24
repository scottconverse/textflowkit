"""A finished job must store its transcript exactly once (U49 / E4).

Every checkpoint written from the `transcribe` stage onward carries a full copy
of the transcript, which is the bulk of the payload - and it is written into the
job's own `checkpoint` column. When the job reaches DONE the runner stores the
same transcript again in the `transcript` column, so a finished job's row holds
two copies of every word, doubling what a durable store keeps on disk for the
deliverable it already has.

The fix stores the transcript once, in the job field, and leaves the DONE
checkpoint as resume metadata only (source, model, language, engine, device,
options, finished stages, media/audio paths, local identity). A reader rebuilds
the full record from both halves in memory. The tests below pin the storage
shape itself - they read the raw SQLite columns, not only the object the store
returns - and then pin every consumer the change could break:

- DONE no-op resume and reuse still find the job after a reopen,
- a requested output whose file is gone is still rendered from the stored copy,
- source/options/identity matching is unchanged,
- a pre-change row that already holds both copies still loads,
- an ERROR/CANCELLED checkpoint still keeps its own transcript, and is never
  hydrated from anywhere else,
- a corrupt half fails closed instead of resuming on a partial record.

Everything here runs against a fake engine and a fake decode step over a temp
tree: no model, no ffmpeg, no network.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from textflowkit.core import pipeline
from textflowkit.core.checkpoint import (
    find_resumable_checkpoint,
    load_checkpoint,
    local_source_identity,
    prepare_resume,
)
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request

# A distinctive body so "is this copy present in that column" is a byte
# question, not a structural guess.
MARKER = "duplicated-transcript-body-marker"


class _MarkerEngine:
    """Deterministic engine that counts runs. No model, no network."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio, *, language=None) -> Transcript:
        self.calls += 1
        return Transcript(
            source=str(audio),
            language=language or "en",
            segments=[Segment(0.0, 1.0, f"hello {MARKER}")],
        )


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake engine, and a fake decode step: no ffmpeg, no model."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    engine = _MarkerEngine()

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
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return output_dir, engine


def _request(source: Path, output_dir: Path | None) -> SubmissionRequest:
    return SubmissionRequest(
        source=str(source),
        formats=["json", "srt"],
        output_dir=str(output_dir) if output_dir is not None else None,
        model="tiny",
        device="cpu",
    )


def _raw_columns(path: Path, job_id: str) -> tuple[str | None, str | None]:
    """Read the two JSON columns straight out of SQLite, without the store.

    The store hands back parsed objects; the defect is about bytes on disk, so
    the proof has to read the file the way an older build would.
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT transcript, checkpoint FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "job row disappeared"
    return row[0], row[1]


def _copies_in_sqlite(path: Path, job_id: str) -> int:
    """How many of the two stored columns carry the transcript body."""
    return sum(1 for column in _raw_columns(path, job_id) if column and MARKER in column)


def _copies_in_memory(job) -> int:
    """How many of the two in-memory fields carry the transcript body."""
    return sum(
        1
        for value in (job.transcript, job.checkpoint)
        if value is not None and MARKER in json.dumps(value)
    )


# --- the storage shape ------------------------------------------------------


def test_done_job_stores_the_transcript_once_in_memory(tmp_path, fake_pipeline):
    """The DONE transition must not leave a second copy of every word."""
    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()

    job = submit_request(store, _request(source, output_dir), background=False)
    done = store.get(job.id)

    assert done.state is JobState.DONE
    assert engine.calls == 1
    # The deliverable is intact: the transcript field holds the real thing.
    assert done.transcript is not None
    assert MARKER in json.dumps(done.transcript)
    # The checkpoint keeps the resume metadata and no transcript body.
    assert done.checkpoint is not None
    assert done.checkpoint.get("transcript") is None
    assert MARKER not in json.dumps(done.checkpoint)
    # Metadata that matching and local identity depend on is still there.
    assert done.checkpoint["source"] == str(source)
    assert done.checkpoint["finished_stages"][-1] == "render"
    assert done.checkpoint["local_identity"]["sha256"]


def test_done_job_stores_the_transcript_once_in_sqlite(tmp_path, fake_pipeline):
    """The same rule, proven on the stored columns rather than the object."""
    output_dir, _engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    db = tmp_path / "jobs.db"
    store = SqliteJobStore(db)

    job = submit_request(store, _request(source, output_dir), background=False)

    transcript_column, checkpoint_column = _raw_columns(db, job.id)
    assert transcript_column is not None and MARKER in transcript_column
    assert checkpoint_column is not None
    assert "transcript" not in json.loads(checkpoint_column)
    assert _copies_in_sqlite(db, job.id) == 1
    store.close()


def test_sqlite_reopen_reuses_and_rerenders_from_the_single_copy(
    tmp_path, fake_pipeline
):
    """Reopen the database: find, no-op resume, and a missing output all work.

    The output directory is emptied between runs, so the recorded outputs are
    gone from disk and can only be rebuilt from the transcript the row still
    holds. The engine must not run again, and identity matching must be
    unaffected by the checkpoint having no transcript of its own.
    """
    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    db = tmp_path / "jobs.db"
    request = _request(source, output_dir)
    identity = local_source_identity(str(source), input_root=tmp_path)

    store = SqliteJobStore(db)
    job = submit_request(store, request, background=False)
    assert job.state is JobState.DONE
    assert engine.calls == 1
    published = {Path(p).suffix: Path(p).read_bytes() for p in job.outputs}
    assert set(published) == {".json", ".srt"}
    store.close()

    reopened = SqliteJobStore(db)
    try:
        assert _copies_in_sqlite(db, job.id) == 1

        found = find_resumable_checkpoint(
            reopened,
            source=str(source),
            model=request.model,
            language=request.language,
            engine=request.engine,
            device=request.device,
            options=request.options(),
        )
        assert found is not None, "a DONE job must still be findable after the change"
        found_job, record = found
        assert found_job.id == job.id
        assert record.source == str(source)
        assert record.options == request.options()
        assert record.local_identity == identity
        assert record.transcript is not None
        assert MARKER in json.dumps(record.transcript)

        # Hydration is read-only: the stored row is byte-for-byte unchanged.
        before = _raw_columns(db, job.id)
        assert load_checkpoint(reopened.get(job.id)) is not None
        assert _raw_columns(db, job.id) == before

        # DONE no-op resume: same job, no engine, no second transcript copy.
        for path in Path(output_dir).iterdir():
            path.unlink()
        resumed = submit_request(reopened, request, background=False, resume=True)
        assert resumed.id == job.id
        assert resumed.state is JobState.DONE
        assert engine.calls == 1, "a DONE resume must not transcribe again"
        assert _copies_in_sqlite(db, job.id) == 1

        # The requested outputs were re-rendered from the stored transcript.
        for suffix, data in published.items():
            path = Path(output_dir) / f"{Path(job.outputs[0]).stem}{suffix}"
            assert path.is_file(), f"{suffix} was not rendered on resume"
            assert path.read_bytes() == data
        assert {Path(p).suffix for p in resumed.outputs} == set(published)
    finally:
        reopened.close()


# --- old rows, other states, and corrupt halves -----------------------------


def test_pre_change_done_row_with_both_copies_still_loads(tmp_path):
    """A row written before this change keeps working, duplicate and all."""
    db = tmp_path / "jobs.db"
    store = SqliteJobStore(db)
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    request = _request(source, None)
    job = store.create(request.source, request=request.to_dict())

    transcript = Transcript(
        source=str(source), language="en", segments=[Segment(0.0, 1.0, MARKER)]
    )
    legacy_checkpoint = {
        "version": 2,
        "source": str(source),
        "model": request.model,
        "language": request.language,
        "engine": request.engine,
        "device": request.device,
        "options": request.options(),
        "finished_stages": ["source", "fetch", "extract", "transcribe", "render"],
        "transcript": transcript.to_dict(),
        "media_path": None,
        "audio_path": None,
        "local_identity": local_source_identity(str(source), input_root=tmp_path),
    }
    store.update(
        job.id,
        state=JobState.DONE,
        progress="complete",
        transcript=transcript.to_dict(),
        checkpoint=legacy_checkpoint,
    )
    assert _copies_in_sqlite(db, job.id) == 2, "the old shape is two copies"

    record = load_checkpoint(store.get(job.id))
    assert record is not None
    assert record.transcript is not None
    assert record.transcript["segments"][0]["text"] == MARKER

    reused = submit_request(store, request, background=False, resume=True)
    assert reused.id == job.id
    store.close()


def test_error_and_cancelled_checkpoints_keep_their_own_transcript(tmp_path):
    """A failed or cancelled job's checkpoint is the only copy: do not touch it."""
    store = MemoryJobStore()
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    metadata = {
        "version": 2,
        "source": str(source),
        "model": "tiny",
        "language": None,
        "engine": "whisper",
        "device": "cpu",
        "options": {"formats": ["json"], "diarize": False},
        "finished_stages": ["source", "fetch", "extract", "transcribe"],
        "media_path": None,
        "audio_path": None,
        "local_identity": local_source_identity(str(source), input_root=tmp_path),
    }
    body = Transcript(
        source=str(source), language="en", segments=[Segment(0.0, 1.0, MARKER)]
    ).to_dict()

    for state in (JobState.ERROR, JobState.CANCELLED):
        job = store.create(str(source))
        store.update(job.id, state=state, checkpoint={**metadata, "transcript": body})
        record = load_checkpoint(store.get(job.id))
        assert record is not None, state
        assert record.transcript is not None, state
        prepared = prepare_resume(store, store.get(job.id), record)
        assert prepared is not None, state
        assert prepared[0].state is JobState.PENDING

    # A metadata-only checkpoint on a non-DONE job is not a DONE checkpoint:
    # hydrating it from the job transcript would invent finished work for a run
    # that never got that far. It stays absent, as it does today.
    for state in (JobState.ERROR, JobState.CANCELLED, JobState.RUNNING):
        job = store.create(str(source))
        store.update(
            job.id,
            state=state,
            transcript=body,
            checkpoint={**metadata, "transcript": None},
        )
        assert load_checkpoint(store.get(job.id)) is None, state


def test_done_checkpoint_with_a_corrupt_transcript_fails_closed(tmp_path):
    """Half a record must not be served as a resume."""
    store = MemoryJobStore()
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    metadata = {
        "version": 2,
        "source": str(source),
        "model": "tiny",
        "language": None,
        "engine": "whisper",
        "device": "cpu",
        "options": {
            "formats": ["json"],
            "diarize": False,
            "diarizer_backend": "pyannote",
            "translate_to": None,
            "translator_backend": "ollama",
        },
        "finished_stages": ["source", "fetch", "extract", "transcribe"],
        "media_path": None,
        "audio_path": None,
        "local_identity": local_source_identity(str(source), input_root=tmp_path),
    }
    job = store.create(str(source))
    store.update(
        job.id,
        state=JobState.DONE,
        transcript={"source": str(source), "segments": [{"nope": 1}]},
        checkpoint={**metadata, "transcript": None},
    )
    assert load_checkpoint(store.get(job.id)) is None

    # Metadata corruption fails closed the same way, from either half.
    store.update(job.id, checkpoint={"source": str(source), "transcript": None})
    assert load_checkpoint(store.get(job.id)) is None

    store.update(
        job.id,
        transcript=Transcript(
            source=str(source), language="en", segments=[]
        ).to_dict(),
        checkpoint={**metadata, "finished_stages": ["source"], "transcript": None},
    )
    assert load_checkpoint(store.get(job.id)) is None

    # A local DONE job with no usable checkpoint refuses to resume rather than
    # silently re-transcribing over a row it cannot read.
    request = SubmissionRequest(
        source=str(source), formats=["json"], model="tiny", device="cpu"
    )
    with pytest.raises(ValueError, match="no reusable checkpoint"):
        submit_request(
            store, request, background=False, resume_job_id=job.id
        )


def test_error_checkpoint_still_refuses_a_tightened_input_root(tmp_path):
    """The up-front input-root refusal must survive the storage change.

    A failed job's checkpoint is the only copy of its work and is never demoted,
    so resume still reads it and refuses a source that now falls outside the
    configured root - before the row is reopened, so a refusal cannot leave a
    PENDING job with nothing queued to run it.
    """
    wide = tmp_path / "wide"
    narrow = tmp_path / "narrow"
    wide.mkdir()
    narrow.mkdir()
    media = wide / "old.wav"
    media.write_bytes(b"fake media")

    store = MemoryJobStore()
    request = SubmissionRequest(
        source=str(media), formats=["json"], model="tiny", device="cpu",
        input_root=str(wide),
    )
    job = store.create(str(media), request=request.to_dict())
    store.update(
        job.id,
        state=JobState.ERROR,
        error="interrupted",
        progress="failed",
        checkpoint={
            "version": 2,
            "source": str(media),
            "model": "tiny",
            "language": None,
            "engine": "whisper",
            "device": "cpu",
            "options": request.options(),
            "finished_stages": ["source", "fetch", "extract", "transcribe"],
            "transcript": Transcript(
                source=str(media), language="en", segments=[Segment(0.0, 1.0, MARKER)]
            ).to_dict(),
            "media_path": None,
            "audio_path": None,
            "local_identity": local_source_identity(str(media), input_root=wide),
        },
    )

    with pytest.raises(ValueError, match="outside the allowed input root"):
        resume_job(store, job.id, background=False, input_root=str(narrow))

    assert store.get(job.id).state is JobState.ERROR, "a refusal must not reopen the row"


def test_cli_completion_leaves_one_copy_on_a_pre_change_row(tmp_path, monkeypatch):
    """The CLI finalises a job with a DONE write of its own.

    A pre-change row is seeded in the old two-copy shape - what an existing
    database holds - and driven through the real CLI resume path, which reuses
    the stored transcript and re-renders a requested output. The row must end
    holding its words once, and the engine must never be reached.
    """
    from textflowkit import cli as cli_mod

    media = tmp_path / "clip.wav"
    media.write_bytes(b"RIFF....WAVEfmt ")

    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(tmp_path))
    store = MemoryJobStore()
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)

    transcript = Transcript(
        source=str(media), language="en", segments=[Segment(0.0, 1.0, MARKER)]
    )
    request = SubmissionRequest(source=str(media), formats=["json"], model="tiny")
    prior = store.create(str(media), request=request.to_dict())
    store.update(
        prior.id,
        state=JobState.DONE,
        transcript=transcript.to_dict(),
        checkpoint={
            "version": 2,
            "source": str(media),
            "model": "tiny",
            "language": None,
            "engine": "whisper",
            "device": None,
            "options": request.options(),
            "finished_stages": ["source", "fetch", "extract", "transcribe", "render"],
            "transcript": transcript.to_dict(),
            "media_path": None,
            "audio_path": None,
            "local_identity": local_source_identity(str(media), input_root=tmp_path),
        },
    )
    assert _copies_in_memory(store.get(prior.id)) == 2

    def explode(*a, **k):
        raise AssertionError("the CLI transcribed instead of reusing the checkpoint")

    monkeypatch.setattr(pipeline, "get_engine", explode)

    rc = cli_mod.main([
        "transcribe", str(media),
        "--model", "tiny",
        "--formats", "json",
        "--output-dir", str(tmp_path),
        "--resume",
        "--quiet",
    ])

    assert rc == 0
    done = store.get(prior.id)
    assert done.state is JobState.DONE
    assert done.id == prior.id, "the CLI must reuse the completed job"
    assert MARKER in json.dumps(done.transcript)
    assert _copies_in_memory(done) == 1
    assert (tmp_path / f"clip-{prior.id}.json").is_file()
