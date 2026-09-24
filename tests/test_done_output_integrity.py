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

A PDF is the one format whose rendering is not byte-reproducible: reportlab
draws a random document id and the render time into every file, so the
comparison blanks exactly those three fields (and nothing else) and fails closed
on any other shape.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.test_partial_output_resume import _CountingEngine
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import CheckpointError, transcript_for_job
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import atomic_write_bytes, ensure_outputs, render_bytes

FOREIGN = b"foreign transcript bytes\n"

# reportlab's render-random metadata, as measured on this host: one tight
# document-id block in the classic trailer, `/ID \n[<32 hex><32 hex>]`, and the
# two render-time values written by its one date formatter.
_PDF_ID_BLOCK = re.compile(rb"/ID\s*\[<([0-9A-Fa-f]{32})><([0-9A-Fa-f]{32})>\]")
_PDF_DATE_VALUE = re.compile(rb"/(CreationDate|ModDate)(\s*)\(D:(\d{14}[+-]\d{2}'\d{2}')\)")


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


# --- PDF: only the random document id may differ ----------------------------


def _pdf_ids(data: bytes) -> list[tuple[bytes, bytes]]:
    """The ``/ID`` digest pairs in a rendered PDF, in file order."""
    return [(m.group(1), m.group(2)) for m in _PDF_ID_BLOCK.finditer(data)]


def _render_with_another_id(transcript: Transcript, data: bytes) -> bytes:
    """A second render of one transcript whose document id differs from `data`.

    reportlab draws the id at random, so two renders of the same transcript
    differ there and nowhere else. A host that drew the same id twice could not
    pose the case at all, so that is skipped rather than asserted on.
    """
    for _ in range(8):
        other = render_bytes(transcript, "pdf")
        if _pdf_ids(other) != _pdf_ids(data):
            return other
    pytest.skip("reportlab produced the same document id for two renders")


def _done_pdf_job(tmp_path, fake_pipeline):
    """A DONE job whose single published output is a PDF, plus its evidence."""
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    job = _finish_job(store, out_dir, source, formats=("pdf",))
    output = Path(job.outputs[0])
    return store, engine, output, list(job.outputs), job.id


def test_done_resume_of_a_pdf_accepts_a_render_with_a_different_document_id(
    tmp_path, fake_pipeline
):
    """The random document id must not make a legitimate PDF resume fail."""
    store, engine, output, _recorded, job_id = _done_pdf_job(tmp_path, fake_pipeline)
    published = output.read_bytes()
    stored = transcript_for_job(store.get(job_id))
    assert stored is not None
    fresh = _render_with_another_id(stored, published)

    assert len(fresh) == len(published)
    resumed = _resume(store, job_id)

    assert resumed.state is JobState.DONE, resumed.error
    assert [Path(p) for p in resumed.outputs] == [output]
    assert output.read_bytes() == published, "the published PDF was rewritten"
    assert engine.calls == 1


def test_done_resume_refuses_a_same_length_pdf_tamper(tmp_path, fake_pipeline):
    """A same-length edit outside the document id is refused, file untouched."""
    store, engine, output, _recorded, job_id = _done_pdf_job(tmp_path, fake_pipeline)
    published = output.read_bytes()
    assert published.count(b"/Title (Transcript)") == 1
    tampered = published.replace(b"/Title (Transcript)", b"/Title (Transcrapt)")
    assert len(tampered) == len(published)
    assert _pdf_ids(tampered) == _pdf_ids(published), "the document id was touched"
    output.write_bytes(tampered)

    with pytest.raises(CheckpointError) as exc:
        _resume(store, job_id)

    assert "no longer matches" in str(exc.value), exc.value
    assert output.read_bytes() == tampered, "the tampered bytes were clobbered"
    assert store.get(job_id).state is JobState.DONE
    assert engine.calls == 1


def test_helper_reuses_a_pdf_that_differs_only_in_the_document_id(tmp_path):
    """Two legitimate renders of one transcript are one file to this rule.

    The file on disk is a real render whose document id differs from the one
    `ensure_outputs` renders for itself: adopting it (rather than failing on
    it) is only possible because the comparison is content-aware.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    prior = render_bytes(transcript, "pdf")
    other = _render_with_another_id(transcript, prior)
    path = out_dir / "talk.pdf"
    path.write_bytes(other)

    written = ensure_outputs(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        existing=[str(path)],
    )

    assert [Path(p) for p in written] == [path]
    assert path.read_bytes() == other, "an adopted file was rewritten"


def _with_render_time(data: bytes, value: bytes) -> bytes:
    """`data` with both render-time values replaced by `value` (same length)."""
    matches = list(_PDF_DATE_VALUE.finditer(data))
    assert len(matches) == 2, matches
    assert len(value) == matches[0].end(3) - matches[0].start(3), "wrong length"
    out = data
    for match in reversed(matches):
        out = out[: match.start(3)] + value + out[match.end(3) :]
    return out


def test_helper_reuses_a_pdf_rendered_at_another_time(tmp_path):
    """A PDF published earlier carries another render time and is still its own.

    The recorded file here is a real render whose two timestamps were rewritten
    to a different, properly shaped render time - which is exactly what a second
    render produces (measured: a render two seconds later differs in those two
    values and nowhere else). A comparison that blanked only the document id
    would refuse the file this tool published itself.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    # The value inside the `D:` prefix, as reportlab's formatter writes it.
    earlier = _with_render_time(published, b"20010101000000+00'00'")
    assert len(earlier) == len(published) and earlier != published
    path = out_dir / "talk.pdf"
    path.write_bytes(earlier)

    written = ensure_outputs(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        existing=[str(path)],
    )

    assert [Path(p) for p in written] == [path]
    assert path.read_bytes() == earlier, "an adopted file was rewritten"


def _pdf_render_time_variants(data: bytes) -> dict[str, bytes]:
    """PDFs whose render-time metadata must never be adopted, by variant name."""
    matches = list(_PDF_DATE_VALUE.finditer(data))
    assert len(matches) == 2, matches
    return {
        "render time without the expected shape": _with_render_time(data, b"X" * 21),
        "render time field repeated": data.replace(
            b"/CreationDate", b"/CreationDate /CreationDate", 1
        ),
        "render time field removed": data.replace(matches[0].group(0), b"", 1),
    }


_PDF_RENDER_TIME_VARIANT_NAMES = (
    "render time without the expected shape",
    "render time field repeated",
    "render time field removed",
)


@pytest.mark.parametrize("variant", _PDF_RENDER_TIME_VARIANT_NAMES)
def test_helper_fails_closed_on_pdf_render_time_that_is_not_the_expected_shape(
    tmp_path, variant
):
    """Unknown or repeated render-time metadata is refused, not accepted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    recorded = render_bytes(transcript, "pdf")
    tampered = _pdf_render_time_variants(recorded)[variant]
    path = out_dir / "talk.pdf"
    path.write_bytes(tampered)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == tampered, "the recorded file was modified"
    assert not list(out_dir.glob("*.tmp"))


def _pdf_variants(data: bytes) -> dict[str, bytes]:
    """Same-or-changed PDFs that must never be adopted, by variant name."""
    match = _PDF_ID_BLOCK.search(data)
    assert match is not None, "the render has no document-id block to vary"
    block = match.group(0)
    first = match.group(1)
    return {
        "two document-id blocks": data.replace(block, block + block, 1),
        "spaced document-id pair": data[: match.end(1)] + b" " + data[match.start(2) :],
        "no document-id block": data[: match.start()] + data[match.end() :],
        "one digest is not hex": data.replace(first, b"z" * 31 + first[-1:], 1),
        "truncated file": data[: len(data) // 2],
        "last byte changed": data[:-1] + bytes([data[-1] ^ 0x01]),
    }


_PDF_VARIANT_NAMES = (
    "two document-id blocks",
    "spaced document-id pair",
    "no document-id block",
    "one digest is not hex",
    "truncated file",
    "last byte changed",
)


@pytest.mark.parametrize("variant", _PDF_VARIANT_NAMES)
def test_helper_fails_closed_on_a_pdf_tamper_or_an_uncomparable_document_id(
    tmp_path, variant
):
    """Unknown or ambiguous document-id shapes are refused, not accepted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    recorded = render_bytes(transcript, "pdf")
    tampered = _pdf_variants(recorded)[variant]
    path = out_dir / "talk.pdf"
    path.write_bytes(tampered)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == tampered, "the recorded file was modified"
    assert not list(out_dir.glob("*.tmp"))


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
