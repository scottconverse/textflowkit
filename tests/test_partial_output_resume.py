"""Same-job resume after a partial multi-format publication (U5, sixdesk ENG-01).

A job that publishes one output format and then fails on a later one is left in
ERROR with its first format already on disk. Retrying that same job re-renders
every requested format and publishes them sequentially, so the retry hits the
no-clobber rule on the file *its own earlier attempt* wrote and fails again. The
job can never be completed.

The fix is narrow: publishing may adopt an existing file only when this run is a
resume of the job that owns the stem and the file's bytes are exactly what this
run would write. A differing file still fails closed, and a fresh run keeps the
strict no-clobber rule even when the bytes match.

These tests drive the real submission -> runner -> pipeline path over a temp tree
with a fake engine and fake decode step, so no model, network, or ffmpeg is
involved. The publish fault is injected once, at the write boundary, which is
where the sixdesk fault landed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import textflowkit.render as render_mod
from textflowkit.core import pipeline
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import atomic_write_bytes, write_all


class _CountingEngine:
    """A deterministic engine that records how many times it was asked to work."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio, *, language=None) -> Transcript:
        self.calls += 1
        return Transcript(
            source=str(audio),
            language=language or "en",
            segments=[Segment(0.0, 1.0, "hello world")],
        )


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake engine, and a fake decode step: no ffmpeg, no model."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    engine = _CountingEngine()

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


def _fail_publish_once(monkeypatch, suffix: str) -> None:
    """Fail the first publish attempt for `suffix`, then publish normally.

    The wrapper delegates to the real function, so the resumed attempt still
    exercises the real publication path.
    """
    real = render_mod.atomic_write_bytes
    armed = {"pending": True}

    def flaky(path, data, **kwargs):
        if armed["pending"] and Path(path).suffix == suffix:
            armed["pending"] = False
            raise OSError(f"injected publish failure for {suffix}")
        return real(path, data, **kwargs)

    monkeypatch.setattr(render_mod, "atomic_write_bytes", flaky)


def _request(source: Path, output_dir: Path) -> SubmissionRequest:
    return SubmissionRequest(
        source=str(source),
        formats=["txt", "srt"],
        output_dir=str(output_dir),
        model="tiny",
        device="cpu",
    )


def test_same_job_resume_finishes_formats_after_partial_publication(
    tmp_path, monkeypatch, fake_pipeline
):
    """The core defect: the retry must complete the job it left half-published."""
    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    request = _request(source, output_dir)
    job = submit_request(store, request, background=False)

    assert store.get(job.id).state is JobState.ERROR
    assert engine.calls == 1

    txt_path = output_dir / f"clip-{job.id}.txt"
    srt_path = output_dir / f"clip-{job.id}.srt"
    assert txt_path.is_file(), "the first format should have published"
    assert not srt_path.exists(), "the second format is the one that failed"
    published = txt_path.read_bytes()
    published_mtime = txt_path.stat().st_mtime_ns

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert resumed.id == job.id
    assert engine.calls == 1, "a resume must not transcribe again"
    assert txt_path.is_file() and srt_path.is_file()
    assert {Path(p).parent for p in resumed.outputs} == {output_dir.resolve()}
    assert {Path(p).stem for p in resumed.outputs} == {f"clip-{job.id}"}
    assert txt_path.read_bytes() == published, "the reused format changed bytes"
    assert txt_path.stat().st_mtime_ns == published_mtime, (
        "the already-published format was rewritten instead of reused"
    )
    assert "hello world" in srt_path.read_text(encoding="utf-8")


def test_resume_refuses_a_differing_file_at_the_missing_format(
    tmp_path, monkeypatch, fake_pipeline
):
    """An unrelated file where the missing format belongs must not be clobbered."""
    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(store, _request(source, output_dir), background=False)
    assert store.get(job.id).state is JobState.ERROR

    txt_path = output_dir / f"clip-{job.id}.txt"
    published = txt_path.read_bytes()
    srt_path = output_dir / f"clip-{job.id}.srt"
    srt_path.write_text("owner data", encoding="utf-8")

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.ERROR, "a differing file must fail closed"
    assert srt_path.read_text(encoding="utf-8") == "owner data"
    assert txt_path.read_bytes() == published
    assert engine.calls == 1


def test_resume_refuses_a_replaced_earlier_format(tmp_path, monkeypatch, fake_pipeline):
    """A published path whose bytes no longer match is not adopted as ours."""
    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".srt")

    job = submit_request(store, _request(source, output_dir), background=False)
    assert store.get(job.id).state is JobState.ERROR

    txt_path = output_dir / f"clip-{job.id}.txt"
    txt_path.write_text("owner data", encoding="utf-8")
    srt_path = output_dir / f"clip-{job.id}.srt"

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.ERROR, "a differing file must fail closed"
    assert txt_path.read_text(encoding="utf-8") == "owner data"
    assert not srt_path.exists(), "nothing may be published after a refusal"
    assert engine.calls == 1


def test_cli_resume_completes_a_partially_published_job(
    tmp_path, monkeypatch, fake_pipeline, capsys
):
    """The same repair on the real CLI surface, through a durable store."""
    from textflowkit import cli
    from textflowkit.core.sqlite_store import SqliteJobStore

    output_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = SqliteJobStore(tmp_path / "jobs.db")
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(cli, "get_default_store", lambda: store)
    _fail_publish_once(monkeypatch, ".srt")

    args = [
        "transcribe",
        str(source),
        "--formats",
        "txt,srt",
        "--output-dir",
        str(output_dir),
        "--model",
        "tiny",
        "--device",
        "cpu",
        "--quiet",
    ]
    try:
        assert cli.main(args) == 1, "the injected publish failure must fail the run"
        capsys.readouterr()
        (job,) = store.list(limit=10)
        assert job.state is JobState.ERROR
        assert engine.calls == 1

        txt_path = output_dir / f"clip-{job.id}.txt"
        srt_path = output_dir / f"clip-{job.id}.srt"
        published = txt_path.read_bytes()
        assert txt_path.is_file() and not srt_path.exists()

        assert cli.main([*args, "--resume"]) == 0, capsys.readouterr().err
        resumed = store.get(job.id)

        assert resumed.state is JobState.DONE, resumed.error
        assert engine.calls == 1, "the CLI resume must not transcribe again"
        assert txt_path.read_bytes() == published
        assert srt_path.is_file()
        assert "hello world" in srt_path.read_text(encoding="utf-8")
    finally:
        store.close()


def test_fresh_run_still_refuses_an_identical_existing_file(tmp_path):
    """No-clobber is unchanged for a fresh attempt, even when bytes match."""
    tr = Transcript(source="x", segments=[Segment(0.0, 1.0, "hello")])
    staged = tmp_path / "staged"
    staged.mkdir()
    (written,) = write_all(tr, formats=["txt"], output_dir=staged, stem="talk")

    target = tmp_path / "out"
    target.mkdir()
    existing = target / "talk.txt"
    existing.write_bytes(written.read_bytes())

    with pytest.raises(FileExistsError):
        write_all(tr, formats=["txt"], output_dir=target, stem="talk")

    assert existing.read_bytes() == written.read_bytes()
    assert not list(target.glob("*.tmp"))


def test_reuse_identical_needs_the_exact_path_and_bytes(tmp_path):
    """The opt-in idempotent publish adopts only a byte-identical file."""
    path = tmp_path / "talk.txt"
    atomic_write_bytes(path, b"one")
    before = path.stat().st_mtime_ns

    atomic_write_bytes(path, b"one", reuse_identical=True)
    assert path.read_bytes() == b"one"
    assert path.stat().st_mtime_ns == before, "an identical file was rewritten"

    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, b"two", reuse_identical=True)
    assert path.read_bytes() == b"one"
    assert not list(tmp_path.glob("*.tmp"))
