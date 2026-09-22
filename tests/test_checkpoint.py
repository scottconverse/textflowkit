"""Resumable checkpoint persistence and matching."""

from __future__ import annotations

import json
import sqlite3

from textflowkit.core.checkpoint import (
    CheckpointRecord,
    find_resumable_checkpoint,
    load_checkpoint,
    matches,
    parse_checkpoint,
    write_checkpoint,
)
from textflowkit.core.jobs import MemoryJobStore
from textflowkit.core.sqlite_store import SqliteJobStore


def _record(source: str = "https://example.com/v", **overrides) -> CheckpointRecord:
    data = {
        "source": source,
        "model": "small",
        "language": "en",
        "device": "cpu",
        "options": {"formats": ["json", "srt"], "diarize": False},
        "finished_stages": ["source", "fetch", "extract", "transcribe"],
        "transcript": {
            "source": source,
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
            "metadata": {},
        },
        "media_path": None,
        "audio_path": None,
    }
    data.update(overrides)
    return CheckpointRecord(**data)


def test_checkpoint_round_trips_through_memory_store():
    store = MemoryJobStore()
    job = store.create("https://example.com/v")
    payload = _record().to_dict()

    assert write_checkpoint(store, job.id, payload) is not None
    got = load_checkpoint(store.get(job.id))

    assert got is not None
    assert got.source == "https://example.com/v"
    assert got.model == "small"
    assert got.language == "en"
    assert got.transcript is not None
    assert got.finished_stages[-1] == "transcribe"


def test_checkpoint_round_trips_through_sqlite_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    s1 = SqliteJobStore(path)
    job = s1.create("https://example.com/v")
    write_checkpoint(s1, job.id, _record())
    s1.close()

    s2 = SqliteJobStore(path)
    got = load_checkpoint(s2.get(job.id))
    assert got is not None
    assert got.transcript is not None
    assert got.options["formats"] == ["json", "srt"]
    s2.close()


def test_sqlite_migrates_an_old_jobs_table(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            progress TEXT NOT NULL DEFAULT '',
            error TEXT,
            transcript TEXT,
            outputs TEXT NOT NULL DEFAULT '[]',
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO jobs (id, source, state, created_at, updated_at, progress,"
        " error, transcript, outputs, cancel_requested)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "src", "pending", 1.0, 1.0, "", None, None, "[]", 0),
    )
    conn.commit()
    conn.close()

    store = SqliteJobStore(path)
    assert store.get("old") is not None
    write_checkpoint(store, "old", _record("src"))
    assert load_checkpoint(store.get("old")) is not None
    store.close()


def test_corrupt_checkpoint_is_treated_as_absent():
    assert parse_checkpoint("not json") is None
    assert parse_checkpoint({"version": 999}) is None
    assert parse_checkpoint({"source": "x", "model": "small", "transcript": "bad"}) is None

    store = MemoryJobStore()
    job = store.create("x")
    store.update(job.id, checkpoint={"source": "x", "model": "small"})
    assert load_checkpoint(store.get(job.id)) is None


def test_matches_rejects_different_source_model_language_device_or_options():
    record = _record()
    base = {
        "source": "https://example.com/v",
        "model": "small",
        "language": "en",
        "device": "cpu",
        "options": {"formats": ["json", "srt"], "diarize": False},
    }
    assert matches(record, **base) is True
    assert matches(record, **{**base, "source": "other"}) is False
    assert matches(record, **{**base, "model": "large"}) is False
    assert matches(record, **{**base, "language": "fr"}) is False
    assert matches(record, **{**base, "device": "cuda"}) is False
    assert matches(record, **{**base, "options": {"formats": ["json"], "diarize": False}}) is False


def test_find_resumable_checkpoint_returns_newest_matching_and_ignores_corrupt():
    store = MemoryJobStore()
    older = store.create("https://example.com/v")
    write_checkpoint(store, older.id, _record())
    corrupt = store.create("https://example.com/v")
    store.update(corrupt.id, checkpoint={"source": "https://example.com/v", "model": "small"})
    newest = store.create("https://example.com/v")
    newest_record = _record(
        finished_stages=["source", "fetch", "extract", "transcribe", "render"]
    )
    write_checkpoint(store, newest.id, newest_record)

    found = find_resumable_checkpoint(
        store,
        source="https://example.com/v",
        model="small",
        language="en",
        device="cpu",
        options={"formats": ["json", "srt"], "diarize": False},
    )

    assert found is not None
    job, record = found
    assert job.id == newest.id
    assert record.finished_stages[-1] == "render"


def test_find_resumable_checkpoint_is_a_clean_no_when_options_differ():
    store = MemoryJobStore()
    job = store.create("https://example.com/v")
    write_checkpoint(store, job.id, _record())

    assert (
        find_resumable_checkpoint(
            store,
            source="https://example.com/v",
            model="small",
            language="en",
            device="cpu",
            options={"formats": ["json"], "diarize": False},
        )
        is None
    )


def test_checkpoint_dict_is_json_serialisable_and_shape_is_stable():
    payload = _record().to_dict()
    assert json.loads(json.dumps(payload)) == payload
    for key in ("source", "model", "language", "finished_stages", "transcript", "options"):
        assert key in payload


# --- the two bugs found by actually running resume --------------------------
# Both of these passed the first round of tests and still made --resume silently
# re-transcribe everything. The tests exist so the next regression is caught.


def test_resume_does_not_require_scratch_media(tmp_path, monkeypatch):
    """A checkpoint whose audio lived in scratch must still resume.

    Scratch is deleted at the end of every run, so the original code - which
    required the recorded audio path to exist - could never resume anything.
    """
    from textflowkit.core import pipeline
    from textflowkit.core.model import Segment, Transcript

    media = tmp_path / "clip.wav"
    media.write_bytes(b"RIFF....WAVEfmt ")

    tr = Transcript(source=str(media), language="en",
                    segments=[Segment(0.0, 1.0, "hello")])
    checkpoint = {
        "version": 1,
        "source": str(media),
        "model": "tiny",
        "language": None,
        "engine": "whisper",
        "device": None,
        "options": {"formats": ["json"], "diarize": False,
                    "diarizer_backend": "pyannote",
                    "translate_to": None, "translator_backend": "ollama"},
        "finished_stages": ["source", "fetch", "extract", "transcribe"],
        "transcript": tr.to_dict(),
        # deliberately a path that does not exist, like a deleted scratch dir
        "media_path": str(tmp_path / "gone" / "clip.wav"),
        "audio_path": str(tmp_path / "gone" / "clip.wav"),
    }

    # The engine must not be reached: a resume that transcribes is a failure.
    def explode(*a, **k):
        raise AssertionError("engine ran despite a valid checkpoint")

    monkeypatch.setattr(pipeline, "get_engine", explode)

    # Output confinement is enforced from the environment root, so widen it to
    # include this test's temp directory for the duration of the call.
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    result = pipeline.transcribe(
        str(media), model="tiny", formats=["json"],
        output_dir=tmp_path, resume_checkpoint=checkpoint,
        input_root=tmp_path,
    )
    assert result.transcript.segments[0].text == "hello"


def test_cli_resume_actually_reuses_a_checkpoint(tmp_path, monkeypatch, capsys):
    """Run the real CLI with --resume and prove it does not re-transcribe.

    The first version of this test called find_resumable_checkpoint directly,
    which passed even when the CLI searched at the wrong moment. This drives
    cli.main so the ordering bug it guards against can actually fail the test.
    """
    from textflowkit import cli as cli_mod
    from textflowkit.core.checkpoint import CheckpointRecord
    from textflowkit.core.jobs import JobState, MemoryJobStore
    from textflowkit.core.model import Segment, Transcript

    media = tmp_path / "clip.wav"
    media.write_bytes(b"RIFF....WAVEfmt ")

    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(tmp_path))

    store = MemoryJobStore()
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)

    tr = Transcript(source=str(media), language="en",
                    segments=[Segment(0.0, 1.0, "seeded")])
    prior = store.create(str(media))
    store.update(
        prior.id,
        state=JobState.DONE,
        transcript=tr.to_dict(),
        checkpoint=CheckpointRecord(
            source=str(media), model="tiny",
            options={"formats": ["json"], "diarize": False,
                     "diarizer_backend": "pyannote",
                     "translate_to": None, "translator_backend": "ollama"},
            finished_stages=["source", "fetch", "extract", "transcribe"],
            transcript=tr.to_dict(),
        ).to_dict(),
    )

    # Any attempt to transcribe means resume did not take effect.
    from textflowkit.core import pipeline as pipeline_mod

    def explode(*a, **k):
        raise AssertionError("the CLI re-transcribed instead of resuming")

    monkeypatch.setattr(pipeline_mod, "get_engine", explode)

    rc = cli_mod.main([
        "transcribe", str(media),
        "--model", "tiny",
        "--formats", "json",
        "--output-dir", str(tmp_path),
        "--resume",
        "--quiet",
    ])

    assert rc == 0, f"cli.main failed with {rc}"
    out = (tmp_path / "clip.json")
    assert out.exists(), "resume produced no output file"
    import json
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["segments"][0]["text"] == "seeded"


# --- explicit resume transitions and the real runner path -------------------


def _seed_checkpoint(store, source, *, model="small", language="en", device="cpu",
                     stages=None, transcript=None, state=None):
    """Create a job carrying a valid checkpoint, optionally terminal."""
    from textflowkit.core.model import Segment, Transcript

    tr = transcript or Transcript(
        source=source,
        language=language,
        segments=[Segment(0.0, 1.0, "seeded")],
    )
    job = store.create(source)
    store.update(
        job.id,
        state=state or "pending",
        checkpoint=_record(
            source=source,
            model=model,
            language=language,
            device=device,
            finished_stages=stages or ["source", "fetch", "extract", "transcribe"],
            transcript=tr.to_dict(),
        ).to_dict(),
    )
    return store.get(job.id)


def test_prepare_resume_reopens_error_job_and_preserves_checkpoint():
    from textflowkit.core.checkpoint import prepare_resume
    from textflowkit.core.jobs import JobState

    store = MemoryJobStore()
    job = _seed_checkpoint(store, "https://example.com/v")
    store.update(
        job.id,
        state=JobState.ERROR,
        error="boom",
        cancel_requested=True,
        progress="failed",
    )
    checkpoint = load_checkpoint(store.get(job.id))
    assert checkpoint is not None

    prepared = prepare_resume(store, job, checkpoint)

    assert prepared is not None
    reopened, payload = prepared
    assert reopened.state is JobState.PENDING
    assert reopened.error is None
    assert reopened.cancel_requested is False
    assert reopened.progress == "resuming"
    assert payload["transcript"] is not None
    assert load_checkpoint(store.get(job.id)).finished_stages == checkpoint.finished_stages


def test_prepare_resume_reopens_cancelled_job():
    from textflowkit.core.checkpoint import prepare_resume
    from textflowkit.core.jobs import JobState

    store = MemoryJobStore()
    job = _seed_checkpoint(store, "https://example.com/v")
    store.update(job.id, state=JobState.CANCELLED, error=None, cancel_requested=True)

    prepared = prepare_resume(store, job, load_checkpoint(store.get(job.id)))

    assert prepared is not None
    reopened, _ = prepared
    assert reopened.state is JobState.PENDING
    assert reopened.cancel_requested is False


def test_prepare_resume_refuses_done_job():
    from textflowkit.core.checkpoint import prepare_resume
    from textflowkit.core.jobs import JobState

    store = MemoryJobStore()
    job = _seed_checkpoint(store, "https://example.com/v")
    store.update(job.id, state=JobState.DONE)

    assert prepare_resume(store, job, load_checkpoint(store.get(job.id))) is None
    assert store.get(job.id).state is JobState.DONE


def test_run_job_resumes_error_job_and_passes_checkpoint(monkeypatch):
    """The real runner must receive the checkpoint and finish DONE."""
    from textflowkit.core import runner
    from textflowkit.core.checkpoint import prepare_resume
    from textflowkit.core.jobs import JobState
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import TranscribeResult

    store = MemoryJobStore()
    job = _seed_checkpoint(store, "https://example.com/v")
    store.update(job.id, state=JobState.ERROR, error="interrupted")
    prepared = prepare_resume(store, job, load_checkpoint(store.get(job.id)))
    assert prepared is not None
    reopened, payload = prepared

    seen = {}

    def fake_transcribe(source, *, resume_checkpoint=None, on_checkpoint=None, **kwargs):
        seen["resume_checkpoint"] = resume_checkpoint
        if on_checkpoint is not None:
            on_checkpoint({"source": source, "model": "small", "transcript": {}})
        return TranscribeResult(
            transcript=Transcript(source=source, language="en", segments=[]),
            outputs=[],
        )

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)
    runner.run_job(reopened, store, source="https://example.com/v", resume_checkpoint=payload)

    assert seen["resume_checkpoint"] == payload
    assert store.get(job.id).state is JobState.DONE


def test_run_job_resumes_cancelled_job(monkeypatch):
    from textflowkit.core import runner
    from textflowkit.core.checkpoint import prepare_resume
    from textflowkit.core.jobs import JobState
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import TranscribeResult

    store = MemoryJobStore()
    job = _seed_checkpoint(store, "https://example.com/v")
    store.update(job.id, state=JobState.CANCELLED, cancel_requested=True)
    prepared = prepare_resume(store, job, load_checkpoint(store.get(job.id)))
    assert prepared is not None
    reopened, payload = prepared

    monkeypatch.setattr(
        runner,
        "transcribe",
        lambda source, **kwargs: TranscribeResult(
            transcript=Transcript(source=source, language="en", segments=[]),
            outputs=[],
        ),
    )
    runner.run_job(reopened, store, source="https://example.com/v", resume_checkpoint=payload)

    assert store.get(job.id).state is JobState.DONE


# --- pipeline stage reuse and fallback ------------------------------------


def test_pipeline_resume_skips_acquisition_extraction_and_engine(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.model import Segment, Transcript

    source = "https://example.com/v"
    transcript = Transcript(source="old", language="en", segments=[Segment(0.0, 1.0, "kept")])
    checkpoint = _record(source=source, transcript=transcript.to_dict())

    calls = []

    class Ref:
        platform = "youtube"
        kind = "url"
        location = source

    class Engine:
        def transcribe(self, audio, language=None):  # pragma: no cover - must not run
            calls.append("engine")
            raise AssertionError("engine ran")

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: Ref())
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: calls.append("require_tool"))
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: calls.append("fetch"))
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: calls.append("extract"))
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))

    result = pipeline.transcribe(
        source,
        model="small",
        language="en",
        device="cpu",
        formats=["json"],
        output_dir=tmp_path,
        resume_checkpoint=checkpoint.to_dict(),
    )

    assert result.transcript.segments[0].text == "kept"
    assert result.transcript.platform == "youtube"
    assert calls == []


def test_pipeline_resume_falls_back_when_transcribe_stage_is_missing(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.model import Segment, Transcript

    source = "https://example.com/v"
    media = tmp_path / "media.bin"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")
    transcript = Transcript(source=source, language="en", segments=[Segment(0.0, 1.0, "new")])
    checkpoint = _record(
        source=source,
        finished_stages=["source", "fetch", "extract"],
        transcript=transcript.to_dict(),
    )

    calls = []

    class Ref:
        platform = "youtube"
        kind = "url"
        location = source

    class Engine:
        def transcribe(self, audio_path, language=None):
            calls.append("engine")
            return transcript

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: Ref())
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: calls.append("require_tool"))
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: audio)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))

    result = pipeline.transcribe(
        source,
        model="small",
        language="en",
        device="cpu",
        formats=["json"],
        output_dir=tmp_path,
        resume_checkpoint=checkpoint.to_dict(),
    )

    assert result.transcript is transcript
    assert "engine" in calls
    assert "fetch" not in calls  # fetch stub is not recorded, but engine proves fallback


def test_pipeline_checkpoints_follow_stage_order(monkeypatch, tmp_path):
    from textflowkit.core import pipeline
    from textflowkit.core.model import Segment, Transcript

    source = "https://example.com/v"
    media = tmp_path / "media.bin"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")
    transcript = Transcript(source=source, language="en", segments=[Segment(0.0, 1.0, "new")])
    stages = []

    class Ref:
        platform = "youtube"
        kind = "url"
        location = source

    class Engine:
        def transcribe(self, audio_path, language=None):
            return transcript

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: Ref())
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_media", lambda *a, **k: media)
    monkeypatch.setattr(pipeline, "extract_audio", lambda *a, **k: audio)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))

    pipeline.transcribe(
        source,
        model="small",
        language="en",
        formats=["json"],
        output_dir=tmp_path,
        on_checkpoint=lambda record: stages.append(record["finished_stages"][-1]),
    )

    assert stages == ["source", "fetch", "extract", "transcribe", "postprocess", "render"]
