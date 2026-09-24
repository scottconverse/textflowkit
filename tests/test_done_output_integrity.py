"""A completed job's explicit resume must verify the outputs it reuses (U34).

U5 taught ``ensure_outputs`` to reuse a recorded output instead of re-publishing
it, but the decision was made on the file suffix alone: any file in the output
directory whose extension matched - whether or not it was named after this job's
stem, and whatever it contained - was returned as the requested output. A
completed job's explicit resume could therefore hand back a modified or foreign
file as its transcript, and could not tell the difference.

These tests drive the real submission -> runner -> pipeline path with a fake
engine and fake decode step (no model, no ffmpeg, no network) plus the helper
directly, so both the resume contract and the reuse rule are pinned.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_partial_output_resume import _CountingEngine
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import CheckpointError
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import atomic_write_bytes, ensure_outputs, render_bytes

FOREIGN = b"foreign transcript bytes\n"


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake engine, and a fake decode step: no ffmpeg, no model.

    The same shape as ``test_partial_output_resume``'s fixture, which owns the
    partial-publication resume case; kept local so each module can be read on
    its own terms.
    """
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


def _transcript() -> Transcript:
    return Transcript(
        source="x", language="en", segments=[Segment(0.0, 1.0, "hello world")]
    )


def _finish_job(
    store: MemoryJobStore,
    out_dir: Path,
    source: Path,
    *,
    formats: tuple[str, ...] = ("txt",),
):
    """Run a whole job to DONE through the shared submission path."""
    request = SubmissionRequest(
        source=str(source), formats=list(formats), output_dir=str(out_dir),
        model="tiny", device="cpu",
    )
    job = submit_request(store, request, background=False)
    assert store.get(job.id).state is JobState.DONE, store.get(job.id).error
    return job


def _resume(store: MemoryJobStore, job_id: str):
    """Explicit resume of the completed job, through the shared entry point."""
    return resume_job(store, job_id, background=False)


@pytest.fixture
def done_job(tmp_path, fake_pipeline):
    """A DONE job with one published ``txt`` output, plus its raw evidence."""
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job = _finish_job(store, out_dir, source)
    (output,) = job.outputs
    return store, engine, Path(output), list(job.outputs), job.id


# --- completed-job explicit resume -----------------------------------------


def test_done_resume_reuses_an_unchanged_recorded_output(done_job):
    """The unchanged case still resumes without re-transcribing or rewriting."""
    store, engine, output, recorded, job_id = done_job
    before = output.read_bytes()
    before_mtime = output.stat().st_mtime_ns

    resumed = _resume(store, job_id)

    assert resumed.state is JobState.DONE, resumed.error
    assert list(resumed.outputs) == recorded
    assert engine.calls == 1, "a resume must not transcribe again"
    assert output.read_bytes() == before
    assert output.stat().st_mtime_ns == before_mtime, (
        "an already-published output was rewritten instead of reused"
    )


def test_done_resume_refuses_a_modified_recorded_output(done_job):
    """A tampered output is not returned as success and is left untouched."""
    store, engine, output, recorded, job_id = done_job
    output.write_bytes(FOREIGN)

    with pytest.raises(CheckpointError) as exc:
        _resume(store, job_id)

    assert "no longer matches" in str(exc.value), exc.value
    assert output.read_bytes() == FOREIGN, "the foreign bytes were clobbered"
    assert engine.calls == 1
    current = store.get(job_id)
    assert current.state is JobState.DONE, "the completed job must not be reopened"
    assert list(current.outputs) == recorded


def test_done_resume_refuses_a_directory_at_the_recorded_name(done_job):
    """Only a regular file can be this job's output, at any name."""
    store, engine, output, recorded, job_id = done_job
    output.unlink()
    output.mkdir()
    (output / "note.txt").write_bytes(FOREIGN)

    with pytest.raises(CheckpointError):
        _resume(store, job_id)

    assert output.is_dir() and (output / "note.txt").read_bytes() == FOREIGN
    assert engine.calls == 1
    assert list(store.get(job_id).outputs) == recorded


def test_cli_resume_refuses_a_modified_output_with_one_clean_error(
    tmp_path, monkeypatch, fake_pipeline, capsys
):
    """The user-visible surface: a failed resume and an untouched file.

    A bare `FileExistsError` escaping the submission path would traceback here,
    so this also pins the refusal to the clean one-line error a resume is
    supposed to produce.
    """
    from textflowkit import cli
    from textflowkit.core.sqlite_store import SqliteJobStore

    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = SqliteJobStore(tmp_path / "jobs.db")
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(cli, "get_default_store", lambda: store)
    args = [
        "transcribe", str(source), "--formats", "txt", "--output-dir", str(out_dir),
        "--model", "tiny", "--device", "cpu", "--quiet",
    ]
    try:
        assert cli.main(args) == 0, capsys.readouterr().err
        (job,) = store.list(limit=10)
        output = Path(job.outputs[0])
        output.write_bytes(FOREIGN)
        capsys.readouterr()

        assert cli.main([*args, "--resume"]) == 1
        stderr = capsys.readouterr().err

        assert "error:" in stderr and "no longer matches" in stderr, stderr
        assert output.read_bytes() == FOREIGN
        assert store.get(job.id).state is JobState.DONE
        assert engine.calls == 1
    finally:
        store.close()


def test_done_resume_does_not_adopt_a_wrong_name_recorded_output(
    tmp_path, fake_pipeline
):
    """A same-suffix file that is not this job's output is never handed back."""
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job = _finish_job(store, out_dir, source)
    recorded = Path(job.outputs[0])
    expected_name = recorded.name
    expected_bytes = recorded.read_bytes()
    recorded.unlink()
    foreign = out_dir / "unrelated.txt"
    foreign.write_bytes(FOREIGN)
    # The record now names the foreign file for the same format, the way a
    # stale or edited record would.
    store.update(job.id, outputs=[str(recorded), str(foreign)])

    resumed = _resume(store, job.id)

    assert resumed.state is JobState.DONE, resumed.error
    assert [Path(p).name for p in resumed.outputs] == [expected_name]
    assert (out_dir / expected_name).read_bytes() == expected_bytes
    assert foreign.read_bytes() == FOREIGN, "a foreign file must not be touched"
    assert engine.calls == 1


def test_done_resume_rebuilds_a_missing_recorded_output(done_job):
    """A deleted output is re-rendered from the stored transcript, not re-run."""
    store, engine, output, _recorded, job_id = done_job
    expected = output.read_bytes()
    output.unlink()

    resumed = _resume(store, job_id)

    assert resumed.state is JobState.DONE, resumed.error
    assert output.read_bytes() == expected
    assert [Path(p) for p in resumed.outputs] == [output]
    assert engine.calls == 1, (
        "the rebuilt output must come from the stored transcript, not from new "
        "acquisition or inference"
    )


def test_done_resume_of_a_pdf_keeps_its_own_output_and_refuses_a_truncated_one(
    tmp_path, fake_pipeline
):
    """A PDF is reused by name, location and length, not by content.

    reportlab stamps a random document identifier into every PDF it writes
    (measured: two renders of one transcript differ in exactly those 64 bytes),
    so a byte comparison would refuse a PDF this tool had itself just
    published. The length is stable for identical content, so a truncated or
    replaced file is still refused - a same-length edit is not, which is the
    limit of this check.
    """
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job = _finish_job(store, out_dir, source, formats=("pdf",))
    output = Path(job.outputs[0])
    size = output.stat().st_size

    resumed = _resume(store, job.id)

    assert resumed.state is JobState.DONE, resumed.error
    assert [Path(p) for p in resumed.outputs] == [output]
    assert output.stat().st_size == size, "the PDF was re-rendered in place"
    assert engine.calls == 1

    output.write_bytes(output.read_bytes()[: size // 2])
    truncated = output.read_bytes()

    with pytest.raises(CheckpointError):
        _resume(store, job.id)

    assert output.read_bytes() == truncated


def test_resume_of_an_empty_format_list_never_returns_recorded_outputs(
    tmp_path, fake_pipeline
):
    """``formats=[]`` (CLI ``--formats ""``) cannot reach the rendering-skip branch.

    The pipeline reads an empty list as the standard three formats, so such a
    job publishes and records all three - while the checkpoint it stores holds
    that defaulted list, not the empty one the request keeps. A resume
    therefore fails the checkpoint match before the DONE branch is reached, and
    the recorded paths are never handed back unverified. This is what makes
    ``reusable_done_result``'s ``formats``-empty branch unreachable from the
    real surfaces; if a later change makes the two lists agree, this test must
    be revisited before that branch can be trusted.
    """
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job = _finish_job(store, out_dir, source, formats=())

    assert sorted(Path(p).suffix for p in job.outputs) == [".json", ".srt", ".txt"]
    tampered = next(Path(p) for p in job.outputs if p.endswith(".txt"))
    tampered.write_bytes(FOREIGN)

    with pytest.raises(ValueError, match="does not match the saved checkpoint"):
        _resume(store, job.id)

    assert tampered.read_bytes() == FOREIGN
    assert engine.calls == 1


# --- the reuse rule itself --------------------------------------------------


def test_helper_does_not_adopt_a_wrong_name_recorded_output(tmp_path):
    """A same-suffix neighbour is not the requested output, even with no clash."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    foreign = out_dir / "unrelated.txt"
    foreign.write_bytes(FOREIGN)

    written = ensure_outputs(
        transcript, formats=["txt"], output_dir=out_dir, stem="talk",
        existing=[str(foreign)],
    )

    assert [Path(p).name for p in written] == ["talk.txt"]
    assert (out_dir / "talk.txt").read_bytes() == render_bytes(transcript, "txt")
    assert foreign.read_bytes() == FOREIGN


def test_helper_fails_closed_on_a_differing_expected_output(tmp_path):
    """The expected name holding other bytes is never clobbered or adopted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    expected = out_dir / "talk.txt"
    expected.write_bytes(FOREIGN)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["txt"], output_dir=out_dir, stem="talk",
            existing=[str(expected)],
        )

    assert expected.read_bytes() == FOREIGN
    assert not list(out_dir.glob("*.tmp"))


def test_helper_fails_closed_on_an_unrecorded_identical_output(tmp_path):
    """Nothing recorded at the expected name: the strict no-clobber rule holds."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    expected = out_dir / "talk.txt"
    expected.write_bytes(render_bytes(transcript, "txt"))

    with pytest.raises(FileExistsError):
        ensure_outputs(transcript, formats=["txt"], output_dir=out_dir, stem="talk")

    assert not list(out_dir.glob("*.tmp"))


def _make_file_symlink(link: Path, target: Path) -> None:
    """Create a file symlink, or skip where the host refuses one.

    Windows needs Developer Mode or elevation for file symlinks (measured:
    ``WinError 1314``); directory junctions do not, but they are directories, so
    they cannot stand in for a symlinked *file* here.
    """
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"cannot create a file symlink here: {exc}")


def test_helper_does_not_adopt_a_recorded_symlink_pointing_outside(tmp_path, monkeypatch):
    """A link whose target holds the right bytes is still not this job's file."""
    root = tmp_path / "root"
    out_dir = root / "out"
    out_dir.mkdir(parents=True)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(root))
    transcript = _transcript()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(render_bytes(transcript, "txt"))
    link = out_dir / "talk.txt"
    _make_file_symlink(link, outside)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["txt"], output_dir=out_dir, stem="talk",
            existing=[str(link)],
        )

    assert link.is_symlink(), "the link was replaced"
    assert outside.read_bytes() == render_bytes(transcript, "txt")
    assert not list(out_dir.glob("*.tmp"))


def test_same_job_adoption_refuses_a_symlink_even_when_bytes_match(tmp_path):
    """The write_all/retry adoption path must not adopt a link either."""
    data = b"hello world\n"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(data)
    link = tmp_path / "talk.txt"
    _make_file_symlink(link, outside)

    with pytest.raises(FileExistsError):
        atomic_write_bytes(link, data, reuse_identical=True)

    assert link.is_symlink()
    assert outside.read_bytes() == data
    assert not list(tmp_path.glob("*.tmp"))
