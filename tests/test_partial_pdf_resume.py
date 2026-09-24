"""Same-job resume of a partially published **PDF** (U35, follows U5/U34).

``write_all(..., reuse_published=True)`` is the ERROR/CANCELLED retry path: a
job whose earlier attempt published some of its formats and then failed is
retried, and the formats already on disk are adopted rather than re-published
(U5). That adoption compared bytes exactly. A PDF is the one format whose
rendering is not byte-reproducible - reportlab draws a random document id and
the render time into every file - so a PDF job could never finish its own
partial publication: the retry hit the no-clobber rule on the file its own
earlier attempt wrote, failed closed, and stayed in ERROR forever.

These tests drive the real submission -> runner -> pipeline resume path over a
temp tree with a fake engine and fake decode step (no model, network or ffmpeg),
plus the helper directly. The publish fault is injected once, at the write
boundary, which is where the reported fault lands.

The PDF comparison itself is U34's, and its full fail-closed shape coverage is
pinned in ``test_done_output_integrity``; the helpers are imported from there
rather than re-derived, and this module pins the cases that are specific to the
partial-publication route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_done_output_integrity import (
    _make_file_symlink,
    _pdf_render_time_variants,
    _pdf_variants,
    _render_with_another_id,
    _transcript,
    _with_render_time,
)
from tests.test_partial_output_resume import _CountingEngine, _fail_publish_once
from textflowkit.core import pipeline
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import atomic_write_bytes, render_bytes, write_all


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    """A temp tree, a fake engine, and a fake decode step: no ffmpeg, no model.

    The same shape as ``test_partial_output_resume``'s fixture, which owns the
    partial-publication resume case. Spelled out here rather than imported so a
    reader of this module sees what the route under test is built from; the
    helper that injects the publish fault is shared, not copied.
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

# Shapes `_normalize_pdf_metadata` must refuse, sampled from the two variant
# families U34 owns. The comparator is the same one this route now calls, and
# every shape in both families is pinned there; a representative subset is
# pinned here so the route itself is covered without duplicating that module.
_UNKNOWN_METADATA_VARIANTS = (
    "no document-id block",
    "two document-id blocks",
    "one digest is not hex",
    "render time without the expected shape",
)


def _partially_published_pdf_job(tmp_path, fake_pipeline, monkeypatch):
    """A job in ERROR with its PDF published and its ``txt`` not, plus evidence.

    ``formats=["pdf", "txt"]`` publishes in that order, so failing the second
    publish leaves exactly the reported state: the PDF this job wrote is on
    disk, the later format is missing.
    """
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".txt")

    request = SubmissionRequest(
        source=str(source),
        formats=["pdf", "txt"],
        output_dir=str(out_dir),
        model="tiny",
        device="cpu",
    )
    job = submit_request(store, request, background=False)

    assert store.get(job.id).state is JobState.ERROR, store.get(job.id).error
    assert engine.calls == 1
    pdf_path = out_dir / f"clip-{job.id}.pdf"
    txt_path = out_dir / f"clip-{job.id}.txt"
    assert pdf_path.is_file(), "the first format should have published"
    assert not txt_path.exists(), "the second format is the one that failed"
    return store, engine, pdf_path, txt_path, job.id


def _refused(store, job_id, pdf_path, expected_bytes, txt_path, engine):
    """The resume failed closed: nothing published, nothing clobbered."""
    resumed = resume_job(store, job_id, background=False)

    assert resumed.state is JobState.ERROR, "a differing file must fail closed"
    assert pdf_path.read_bytes() == expected_bytes, "the published PDF was clobbered"
    assert not txt_path.exists(), "nothing may be published after a refusal"
    assert engine.calls == 1, "a refused resume must not transcribe again"


# --- the reported defect: this job's own PDF cannot be adopted ---------------


def test_partial_pdf_resume_adopts_its_own_earlier_pdf(
    tmp_path, monkeypatch, fake_pipeline
):
    """The core defect: the retry must finish the job it left half-published.

    The recorded PDF is a real render of the same transcript whose document id
    and render time were drawn again, so it is not byte-equal to the fresh
    render - only its render-metadata fields differ.
    """
    store, engine, pdf_path, txt_path, job_id = _partially_published_pdf_job(
        tmp_path, fake_pipeline, monkeypatch
    )
    published = pdf_path.read_bytes()
    published_mtime = pdf_path.stat().st_mtime_ns

    resumed = resume_job(store, job_id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert resumed.id == job_id
    assert engine.calls == 1, "a resume must not transcribe again"
    assert {Path(p).stem for p in resumed.outputs} == {f"clip-{job_id}"}
    assert pdf_path.read_bytes() == published, "the reused PDF changed bytes"
    assert pdf_path.stat().st_mtime_ns == published_mtime, (
        "the already-published PDF was rewritten instead of reused"
    )
    assert txt_path.is_file(), "the missing format must be finished"
    assert "hello world" in txt_path.read_text(encoding="utf-8")


def test_write_all_reuses_its_own_pdf_rendered_at_another_time(tmp_path):
    """A PDF published earlier in the day is still this job's own file.

    A real resume renders long after the file was published, so the recorded PDF
    carries a different render time. A comparison that blanked only the document
    id would refuse the file this tool published itself.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    (written,) = write_all(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )
    earlier = _with_render_time(published, b"20010101000000+00'00'")
    assert len(earlier) == len(published) and earlier != published
    written.write_bytes(earlier)
    mtime = written.stat().st_mtime_ns

    (again,) = write_all(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )

    assert again == written
    assert written.read_bytes() == earlier, "an adopted file was rewritten"
    assert written.stat().st_mtime_ns == mtime


def test_write_all_reuses_its_own_pdf_with_another_document_id(tmp_path):
    """Two legitimate renders of one transcript are one file to this rule."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    (written,) = write_all(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )
    other = _render_with_another_id(transcript, published)
    written.write_bytes(other)
    mtime = written.stat().st_mtime_ns

    (again,) = write_all(
        transcript, formats=["pdf"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )

    assert again == written
    assert written.read_bytes() == other, "an adopted file was rewritten"
    assert written.stat().st_mtime_ns == mtime


# --- what must still fail closed on the same route --------------------------


def test_partial_pdf_resume_refuses_a_same_size_pdf_tamper(
    tmp_path, monkeypatch, fake_pipeline
):
    """A same-length edit outside the document id is refused, file untouched."""
    store, engine, pdf_path, txt_path, job_id = _partially_published_pdf_job(
        tmp_path, fake_pipeline, monkeypatch
    )
    published = pdf_path.read_bytes()
    assert published.count(b"/Title (Transcript)") == 1
    tampered = published.replace(b"/Title (Transcript)", b"/Title (Transcrapt)")
    assert len(tampered) == len(published)
    pdf_path.write_bytes(tampered)

    _refused(store, job_id, pdf_path, tampered, txt_path, engine)


@pytest.mark.parametrize("variant", _UNKNOWN_METADATA_VARIANTS)
def test_partial_pdf_resume_refuses_uncomparable_render_metadata(
    tmp_path, monkeypatch, fake_pipeline, variant
):
    """Metadata absent, repeated or mis-shaped is refused, not accepted."""
    store, engine, pdf_path, txt_path, job_id = _partially_published_pdf_job(
        tmp_path, fake_pipeline, monkeypatch
    )
    published = pdf_path.read_bytes()
    tampered = {
        **_pdf_variants(published),
        **_pdf_render_time_variants(published),
    }[variant]
    pdf_path.write_bytes(tampered)

    _refused(store, job_id, pdf_path, tampered, txt_path, engine)


def test_partial_pdf_resume_refuses_a_symlink_at_the_recorded_name(
    tmp_path, monkeypatch, fake_pipeline
):
    """A link whose target holds the right bytes is still not this job's file."""
    store, engine, pdf_path, txt_path, job_id = _partially_published_pdf_job(
        tmp_path, fake_pipeline, monkeypatch
    )
    published = pdf_path.read_bytes()
    pdf_path.unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(published)
    _make_file_symlink(pdf_path, outside)

    _refused(store, job_id, pdf_path, published, txt_path, engine)

    assert pdf_path.is_symlink(), "the link was replaced"
    assert outside.read_bytes() == published
    assert not list(txt_path.parent.glob("*.tmp"))


# --- the boundaries of the widening -----------------------------------------


def test_fresh_write_all_never_adopts_an_existing_pdf(tmp_path):
    """A fresh run keeps the strict no-clobber rule, metadata aside."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    other = _render_with_another_id(transcript, published)
    expected = out_dir / "talk.pdf"
    expected.write_bytes(other)

    with pytest.raises(FileExistsError):
        write_all(transcript, formats=["pdf"], output_dir=out_dir, stem="talk")

    assert expected.read_bytes() == other, "the existing file was modified"
    assert not list(out_dir.glob("*.tmp"))


def test_write_all_still_refuses_a_differing_non_pdf_output(tmp_path):
    """Widening the PDF comparison must not widen any other format."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    expected = out_dir / "talk.txt"
    expected.write_text("owner data", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_all(
            transcript, formats=["txt"], output_dir=out_dir, stem="talk",
            reuse_published=True,
        )

    assert expected.read_text(encoding="utf-8") == "owner data"
    assert not list(out_dir.glob("*.tmp"))


def test_write_all_still_adopts_a_byte_identical_non_pdf_output(tmp_path):
    """The byte-exact rule for the other formats is unchanged."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    (written,) = write_all(
        transcript, formats=["txt"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )
    before = written.read_bytes()
    mtime = written.stat().st_mtime_ns

    (again,) = write_all(
        transcript, formats=["txt"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )

    assert again == written
    assert written.read_bytes() == before
    assert written.stat().st_mtime_ns == mtime


def test_reuse_identical_without_a_declared_format_stays_byte_exact(tmp_path):
    """A caller that declares no format keeps the documented byte rule.

    The rule is chosen by the *format the caller is publishing*, never by the
    file's suffix: a ``.pdf`` name plus the exact byte rule must still refuse a
    PDF that differs only in its render metadata.
    """
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    other = _render_with_another_id(transcript, published)
    path = tmp_path / "talk.pdf"
    path.write_bytes(published)

    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, other, reuse_identical=True)

    assert path.read_bytes() == published, "the existing file was modified"
    assert not list(tmp_path.glob("*.tmp"))


def test_an_explicit_replace_is_a_write_and_never_adopts(tmp_path):
    """``replace=True`` is the caller's explicit choice to overwrite."""
    transcript = _transcript()
    published = render_bytes(transcript, "pdf")
    other = _render_with_another_id(transcript, published)
    path = tmp_path / "talk.pdf"
    path.write_bytes(published)

    atomic_write_bytes(
        path, other, replace=True, reuse_identical=True, reuse_format="pdf"
    )

    assert path.read_bytes() == other, "replace must write, not reuse"
    assert not list(tmp_path.glob("*.tmp"))
