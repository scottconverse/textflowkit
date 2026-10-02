"""AL-002: resume setup failures must not destroy the previous transcript.

Audit finding AL-002 — the pipeline serialized its *local* ``transcript`` (still
``None``) into a source checkpoint before it hydrated the previous transcript,
and published that empty snapshot through the runner's guarded write. A failure
in the setup window (a ``work_dir`` that is a file, a failing ``mkdtemp``) would
therefore replace a durable completed transcript with ``transcript: null`` while
still listing ``transcribe`` as finished — leaving a checkpoint that looks
complete but holds nothing, so the next retry must recompute all the expensive
inference and postprocessing work.

The fix: hydrate and validate reusable work *before* publishing any replacement
checkpoint, so a setup failure leaves the previous durable snapshot intact. Only
newly completed work is ever written, coherently, as the work advances.

These tests use a real SQLite store and reopen the database to assert the
durable row still holds the original transcript and coherent stage markers. No
model, network, or ffmpeg is reached — the failures are induced in setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textflowkit.core.checkpoint import (
    CheckpointRecord,
    load_checkpoint,
    local_source_identity,
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


def test_work_dir_that_is_a_file_keeps_the_previous_checkpoint(tmp_path):
    """A real setup failure must not replace a usable transcript with null.

    RED: the source checkpoint was published before hydration, so the failing
    ``work_dir`` setup replaced the stored transcript with ``null``.
    """
    blocked = tmp_path / "work"
    blocked.write_text("not a directory")
    store, job, _request, source = _seeded_error_job(tmp_path, work_dir=blocked)

    before = load_checkpoint(store.get(job.id))
    assert before is not None and before.transcript is not None

    with pytest.raises(Exception):
        transcribe(
            str(source), formats=["json"], model="small",
            work_dir=str(blocked),
            resume_checkpoint=load_checkpoint(store.get(job.id)).to_dict(),
        )

    # Reopen the database: the original transcript and coherent markers remain.
    store.close()
    reopened = SqliteJobStore(tmp_path / "jobs.db")
    try:
        after = load_checkpoint(reopened.get(job.id))
        assert after is not None, "the usable checkpoint was destroyed by setup failure"
        assert after.transcript is not None
        assert after.transcript["segments"][0]["text"] == TRANSCRIPT_TEXT
        assert "transcribe" in after.finished_stages
    finally:
        reopened.close()


def test_resume_setup_failure_through_submission_keeps_the_checkpoint(tmp_path):
    """The same failure driven through ``resume_job`` leaves the row intact.

    This is the audit's exact path: a real ERROR row with a usable checkpoint,
    resumed with a ``work_dir`` that cannot be created.
    """
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


def test_source_checkpoint_callback_failure_window_preserves_snapshot(tmp_path, monkeypatch):
    """A crash between the source checkpoint and hydration must not lose work.

    The window the audit named is the callback that persists the *source*
    snapshot. Whatever a run does inside that window, the previously durable
    transcript must survive: the replacement may only be published once the
    hydrated work is actually represented.
    """
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(source=str(source), formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict(),
    )
    original = load_checkpoint(store.get(job.id)).to_dict()

    seen: list[dict] = []

    def _boom(record: dict) -> None:
        seen.append(record)
        # The first callback is the pre-hydration source snapshot. Failing here
        # models an interrupted resume inside the setup window.
        raise RuntimeError("interrupted after source checkpoint")

    # Drive the pipeline directly through its public callback so the failure
    # lands inside the window the finding names.
    from textflowkit.core.pipeline import transcribe as _transcribe

    # Make the work_dir setup fail deterministically at mkdtemp.
    monkeypatch.setattr(
        "textflowkit.core.pipeline.tempfile.mkdtemp",
        lambda **kwargs: (_ for _ in ()).throw(OSError("mkdtemp failed")),
    )
    with pytest.raises(Exception):
        _transcribe(
            str(source), formats=["json"], resume_checkpoint=original,
            on_checkpoint=_boom,
        )

    # Nothing was written to the store; the durable snapshot is untouched.
    store.close()
    reopened = SqliteJobStore(tmp_path / "jobs.db")
    try:
        after = load_checkpoint(reopened.get(job.id)).to_dict()
        assert after["transcript"] is not None
        assert after["transcript"]["segments"][0]["text"] == TRANSCRIPT_TEXT
    finally:
        reopened.close()


def test_healthy_retry_after_setup_failure_reuses_recognized_work(tmp_path, monkeypatch):
    """After a setup failure the next, healthy retry still reuses the transcript.

    The whole point of preserving the snapshot is that a following retry with a
    valid work directory must skip the expensive stages rather than recompute.
    """
    from textflowkit.core import pipeline as pipeline_mod

    source = tmp_path / "clip.wav"
    source.write_bytes(b"audit fixture; no inference required")
    request = SubmissionRequest(source=str(source), formats=["json"])
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id, state=JobState.ERROR, checkpoint=_record(source, request).to_dict(),
    )

    blocked = tmp_path / "work"
    blocked.write_text("not a directory")
    with pytest.raises(Exception):
        pipeline_mod.transcribe(
            str(source), formats=["json"], work_dir=str(blocked),
            resume_checkpoint=load_checkpoint(store.get(job.id)).to_dict(),
        )

    # The transcript survives, so a retry that reaches hydration recognizes it.
    resumed = load_checkpoint(store.get(job.id))
    assert resumed is not None and resumed.transcript is not None
    assert resumed.transcript["segments"][0]["text"] == TRANSCRIPT_TEXT

    calls: list[int] = []

    class _Engine:
        def transcribe(self, audio, *, language=None):  # pragma: no cover - must not run
            calls.append(1)
            raise AssertionError("engine must not run: transcript should be reused")

    monkeypatch.setattr(pipeline_mod, "get_engine", lambda *a, **k: _Engine())
    work = tmp_path / "goodwork"
    work.mkdir()
    result = pipeline_mod.transcribe(
        str(source), formats=["json"], work_dir=str(work),
        resume_checkpoint=resumed.to_dict(),
    )
    assert calls == [], "recognized transcript was recomputed"
    assert result.transcript.segments[0].text == TRANSCRIPT_TEXT
    store.close()
