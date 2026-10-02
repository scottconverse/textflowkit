"""AL-002: resume setup failures must not destroy the previous transcript.

Audit finding AL-002 — the pipeline published a ``_checkpoint("source")`` before
it hydrated the previous transcript, so the snapshot it wrote carried the local
``transcript`` (still ``None``) while still listing ``transcribe`` as finished.
A failure in the setup window (a ``work_dir`` that is a file, a failing
``mkdtemp``) therefore replaced a durable completed transcript with
``transcript: null``, and the next retry had to recompute all the expensive work.

The fix: validate and hydrate reusable work *before* publishing the source
checkpoint, and publish only a coherent snapshot. A setup failure then leaves
the previous durable snapshot intact.

These tests write through a **real SQLite checkpoint sink** — the same
``write_checkpoint`` path the runner uses — so reopening the database proves
what actually landed, not what a spy remembered. No model, network, or ffmpeg is
reached: the failures are induced in setup, and the healthy retry is asserted to
make zero expensive calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textflowkit.core.checkpoint import (
    CheckpointRecord,
    load_checkpoint,
    local_source_identity,
    write_checkpoint,
)
from textflowkit.core.jobs import JobState
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import transcribe
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job

TRANSCRIPT_TEXT = "already recognized speech"


def _record(source: Path, request: SubmissionRequest, **kwargs) -> CheckpointRecord:
    return CheckpointRecord(
        source=str(source),
        model=request.model,
        options=request.options(),
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=Transcript(
            source=str(source), segments=[Segment(0.0, 1.0, TRANSCRIPT_TEXT)],
        ).to_dict(),
        local_identity=local_source_identity(str(source)),
        **kwargs,
    )


def _seeded_error_job(tmp_path: Path, *, work_dir: Path | None = None):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(
        source=str(source), formats=["json"],
        work_dir=str(work_dir) if work_dir is not None else None,
    )
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict(),
        error="prior rendering error",
    )
    return store, job, request, source


def _assert_durable_snapshot_intact(db: Path, job_id: str) -> None:
    """Reopen the database and assert the reusable transcript + markers survive."""
    reopened = SqliteJobStore(db)
    try:
        after = load_checkpoint(reopened.get(job_id))
        assert after is not None, "the usable checkpoint was destroyed by setup failure"
        assert after.transcript is not None, "transcript replaced with null"
        assert after.transcript["segments"][0]["text"] == TRANSCRIPT_TEXT
        assert "transcribe" in after.finished_stages
    finally:
        reopened.close()


def test_work_dir_that_is_a_file_keeps_the_previous_checkpoint(tmp_path):
    """A failing ``mkdir`` through the real sink must not replace the transcript.

    The pipeline's public callback is wired to a real SQLite ``write_checkpoint``
    sink for the resumed job, so any snapshot the run publishes is durable and
    observable after reopening. A ``work_dir`` that is a regular file makes the
    directory setup raise before any usable work is represented.
    """
    blocked = tmp_path / "work"
    blocked.write_text("not a directory")
    store, job, _request, source = _seeded_error_job(tmp_path, work_dir=blocked)
    db = tmp_path / "jobs.db"

    before = load_checkpoint(store.get(job.id))
    assert before is not None and before.transcript is not None

    with pytest.raises(OSError):
        transcribe(
            str(source), formats=["json"], model="small", work_dir=str(blocked),
            resume_checkpoint=before.to_dict(),
            on_checkpoint=lambda rec: write_checkpoint(store, job.id, rec),
        )

    store.close()
    _assert_durable_snapshot_intact(db, job.id)


def test_mkdtemp_failure_through_real_sink_keeps_the_checkpoint(tmp_path, monkeypatch):
    """A failing ``mkdtemp`` (directory exists) must not replace the transcript.

    Models a real Windows ``FileExistsError`` from a scratch path that cannot be
    created, driven through the pipeline's public callback backed by the SQLite
    sink.
    """
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(source=str(source), formats=["json"])
    db = tmp_path / "jobs.db"
    store = SqliteJobStore(db)
    job = store.create(str(source), request=request.to_dict())
    store.update(job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict())
    original = load_checkpoint(store.get(job.id)).to_dict()

    def _boom(**kwargs):
        raise FileExistsError("scratch directory could not be created")

    monkeypatch.setattr("textflowkit.core.pipeline.tempfile.mkdtemp", _boom)
    with pytest.raises(FileExistsError):
        transcribe(
            str(source), formats=["json"], resume_checkpoint=original,
            on_checkpoint=lambda rec: write_checkpoint(store, job.id, rec),
        )

    store.close()
    _assert_durable_snapshot_intact(db, job.id)


def test_source_callback_persists_then_raises_exact_error(tmp_path):
    """Persist the replacement through the real sink, then raise the exact error.

    The audit window is the callback that writes the *source* snapshot. Here the
    replacement is written durably (as the runner does) and then the injected
    ``RuntimeError`` is raised, so the reopened database shows what a real
    interrupt at that instant would have left behind: a coherent snapshot that
    still carries the previous transcript and markers — never a complete-but-empty
    one.
    """
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(source=str(source), formats=["json"])
    db = tmp_path / "jobs.db"
    store = SqliteJobStore(db)
    job = store.create(str(source), request=request.to_dict())
    store.update(job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict())
    original = load_checkpoint(store.get(job.id)).to_dict()

    published: list[dict] = []

    def _persist_then_raise(record: dict) -> None:
        # Same durable path the runner wires up, then the exact injected fault.
        write_checkpoint(store, job.id, record)
        published.append(record)
        raise RuntimeError("interrupted in the source-checkpoint window")

    with pytest.raises(RuntimeError, match="interrupted in the source-checkpoint window"):
        transcribe(
            str(source), formats=["json"], resume_checkpoint=original,
            on_checkpoint=_persist_then_raise,
        )

    assert published, "no checkpoint was published before the injected failure"
    first = published[0]
    assert "source" in first["finished_stages"]
    assert first["transcript"] is not None, (
        "source snapshot dropped the previous transcript while claiming transcribe"
    )
    assert first["transcript"]["segments"][0]["text"] == TRANSCRIPT_TEXT

    # The durable row, reopened, carries the same coherent snapshot.
    store.close()
    _assert_durable_snapshot_intact(db, job.id)


def test_resume_setup_failure_through_submission_keeps_the_checkpoint(tmp_path):
    """The audit's exact path: ``resume_job`` with an uncreatable ``work_dir``."""
    blocked = tmp_path / "work"
    blocked.write_text("not a directory")
    store, job, _request, _source = _seeded_error_job(tmp_path, work_dir=blocked)

    before = load_checkpoint(store.get(job.id))
    assert before is not None and before.transcript is not None

    result = resume_job(store, job.id, background=False)
    assert result.state is JobState.ERROR

    raw = store.get(job.id)
    after = load_checkpoint(raw)
    assert after is not None, "checkpoint unusable after a setup failure"
    assert after.transcript is not None
    assert after.transcript["segments"][0]["text"] == TRANSCRIPT_TEXT
    assert raw.checkpoint["finished_stages"] == ["source", "fetch", "extract", "transcribe"]
    store.close()


def test_healthy_retry_after_setup_failure_makes_zero_expensive_calls(tmp_path, monkeypatch):
    """After a setup failure a healthy retry reuses work with no expensive calls.

    The point of preserving the snapshot is that the following retry must skip
    transcription, acquisition, and decoding. Spies on the engine and the fetch /
    extract stages must record **zero** calls.
    """
    from textflowkit.core import pipeline as pipeline_mod

    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(source=str(source), formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(str(source), request=request.to_dict())
    store.update(job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict())

    blocked = tmp_path / "work"
    blocked.write_text("not a directory")
    with pytest.raises(OSError):
        pipeline_mod.transcribe(
            str(source), formats=["json"], work_dir=str(blocked),
            resume_checkpoint=load_checkpoint(store.get(job.id)).to_dict(),
            on_checkpoint=lambda rec: write_checkpoint(store, job.id, rec),
        )

    resumed = load_checkpoint(store.get(job.id))
    assert resumed is not None and resumed.transcript is not None

    calls: dict[str, int] = {"engine": 0, "fetch": 0, "extract": 0}

    class _Engine:
        def transcribe(self, audio, *, language=None):  # pragma: no cover - must not run
            calls["engine"] += 1
            raise AssertionError("engine must not run: transcript should be reused")

    def _no_fetch(*a, **k):  # pragma: no cover - must not run
        calls["fetch"] += 1
        raise AssertionError("fetch must not run for a completed transcript")

    def _no_extract(*a, **k):  # pragma: no cover - must not run
        calls["extract"] += 1
        raise AssertionError("extract must not run for a completed transcript")

    monkeypatch.setattr(pipeline_mod, "get_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(pipeline_mod, "fetch_media", _no_fetch)
    monkeypatch.setattr(pipeline_mod, "stage_confined_local_media", _no_fetch)
    monkeypatch.setattr(pipeline_mod, "extract_audio", _no_extract)

    work = tmp_path / "goodwork"
    work.mkdir()
    result = pipeline_mod.transcribe(
        str(source), formats=["json"], work_dir=str(work),
        resume_checkpoint=resumed.to_dict(),
        on_checkpoint=lambda rec: write_checkpoint(store, job.id, rec),
    )
    assert calls == {"engine": 0, "fetch": 0, "extract": 0}, calls
    assert result.transcript.segments[0].text == TRANSCRIPT_TEXT
    store.close()
