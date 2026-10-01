"""Same-job resume of a **DOCX** output across a changed render clock (G3A).

``ensure_outputs`` and ``write_all`` reuse an already-published file only when it
*is* the file this run would publish (``render._is_own_publication``). For every
format but PDF that rule is strict byte equality, and PDF has the one documented
widening (reportlab writes a random document id and the render time into every
render, and those three fields are blanked before the bytes are compared).

DOCX is the gap this module pins. ``python-docx`` writes a ZIP whose local file
headers and central directory carry a per-member DOS date/time drawn from the
render clock. Two renders of the *same* transcript in the same second are
byte-identical, but two renders that straddle a two-second DOS-clock boundary
differ in those timestamp fields and nowhere else. Under the strict byte rule
that makes a job's own earlier output unrecognisable to its own retry:

- the DONE-resume path (``ensure_outputs`` -> ``_reusable_prior`` ->
  ``_is_own_publication``) refuses the recorded DOCX, and
- the partial-publication retry path (``write_all(..., reuse_published=True)`` ->
  ``atomic_write_bytes(..., reuse_identical=True, reuse_format="docx")``) refuses
  the DOCX its own earlier attempt wrote and fails closed, leaving the job stuck.

These tests drive the **real** renderer (``textflowkit.render.docx.render_docx``
through ``render_bytes``), not a stub, and vary only the render clock so the
timestamps actually cross a boundary. One control is real and sleep-bounded
(2.1s, at most 12 attempts), so the timestamp-variance premise is observed here
and is not inferred from the source.

Everything else must still fail closed: a content change, a corrupt archive, a
duplicate member, a foreign member shape, and a caller that declares no format
all keep the strict rule. Nothing here adds a new output format - DOCX is an
existing one and only its reuse rule is at issue.

RED today (strict byte comparison refuses these): the ``*_reuses_*`` and
``*_adopts_*`` cases. Also RED today, but for the opposite reason, are the four
``test_helper_refuses_a_*`` cases: today a timestamp-shifted archive is refused
by *accident* (byte inequality), not by an archive-aware comparison, so the
assertion that it *is* refused still holds at baseline yet does not pin the
future meaning - the widened comparison must refuse them because the content
differs, not because the clock did. The remaining cases (``*_still_refuses_*``,
``*_never_adopts_*``, ``*_stays_byte_exact``) are the no-clobber contract that
holds today and must keep holding after the widening.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest

from tests.test_done_output_integrity import _transcript
from tests.test_partial_output_resume import _CountingEngine, _fail_publish_once
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import transcript_for_job
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import (
    atomic_write_bytes,
    ensure_outputs,
    render_bytes,
    write_all,
)

# A DOS date/time two seconds past the epoch, well outside any window a live
# render can land in. Used only where a fixed, readable older value is wanted;
# ``_render_docx_across_a_clock_boundary`` produces the value the real clock
# actually writes.
_OLD_DOS_TIME = (1980, 1, 1, 0, 0, 2)


def _docx_member_times(data: bytes) -> list[tuple[bytes, tuple[int, ...]]]:
    """``(member name, DOS date/time)`` for every ZIP entry, in file order.

    Reads both the local file headers and the central directory, because a
    python-docx archive populates both and the central directory is what a
    normal reader trusts.
    """
    out: list[tuple[bytes, tuple[int, ...]]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            out.append((info.filename.encode("utf-8"), tuple(info.date_time)))
    return out


def _docx_bytes_equal_ignoring_member_times(left: bytes, right: bytes) -> bool:
    """Whether two DOCX archives hold byte-identical members and nothing else.

    Decompresses every member and compares the *content* bytes and the member
    name list, ignoring only the ZIP date/time stamps. This is the strongest
    "same publication, different clock" statement a test can make about two
    python-docx renders; it is the premise the product comparison must match.
    """
    with zipfile.ZipFile(io.BytesIO(left)) as a, zipfile.ZipFile(io.BytesIO(right)) as b:
        if a.namelist() != b.namelist():
            return False
        return all(a.read(name) == b.read(name) for name in a.namelist())


def _render_docx_across_a_clock_boundary(transcript, *, attempts: int = 12):
    """Render ``transcript`` twice, with the second render two seconds later.

    The two renders are the same transcript, so the only difference python-docx
    can introduce is the ZIP members' date/time - but the clock has to actually
    move across a two-second DOS-second boundary for the bytes to change, so the
    loop waits, re-renders, and returns the first pair whose bytes differ *and*
    whose decompressed members are identical. Anything else is a skip, never an
    assertion.

    Returns ``(before, after)``.
    """
    before = render_bytes(transcript, "docx")
    for _ in range(attempts):
        time.sleep(2.1)
        after = render_bytes(transcript, "docx")
        if after == before:
            continue
        if not _docx_bytes_equal_ignoring_member_times(before, after):
            pytest.skip("two renders of one transcript changed member content, not just time")
        return before, after
    pytest.skip("two renders 2.1s apart never changed the archive bytes")


def _docx_with_a_changed_member(transcript, rewrite) -> bytes:
    """A real DOCX re-zipped with every member's date/time set to ``rewrite``.

    Used where a fixed, obviously-older timestamp is clearer than a live clock
    (the DONE-resume/partial-resume adoption cases). Member content is preserved
    exactly; only ``ZipInfo.date_time`` is overwritten.
    """
    data = render_bytes(transcript, "docx")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            info.date_time = rewrite
            dst.writestr(info, src.read(info.filename))
    return buffer.getvalue()


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

    def extract(media, *, work_dir, check_cancel=None, confined=False):
        audio = Path(work_dir) / "audio.wav"
        audio.write_bytes(media.read_bytes())
        return audio

    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: "ffmpeg")
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return output_dir, engine


# --- premise: the real renderer varies only in ZIP member times -------------


def test_two_real_docx_renders_across_a_clock_boundary_differ_only_in_member_times():
    """The premise, observed: two real renders, only the ZIP stamps moved.

    Bounded by ``_render_docx_across_a_clock_boundary`` (2.1s waits, at most 12
    attempts), so this is a real pair, not a claim about the source.
    """
    transcript = _transcript()
    before, after = _render_docx_across_a_clock_boundary(transcript)

    assert before != after, "the two renders must actually differ"
    assert _docx_member_times(before) != _docx_member_times(after), (
        "the difference must actually be in the ZIP member date/time"
    )
    assert _docx_bytes_equal_ignoring_member_times(before, after), (
        "two renders of one transcript must differ only in ZIP member times"
    )


# --- the defect: a DOCX is unrecognisable to its own retry ------------------


def test_helper_reuses_a_docx_whose_member_times_moved(tmp_path):
    """The DONE-resume rule must accept a real earlier render of this transcript."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    earlier = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    path = out_dir / "talk.docx"
    path.write_bytes(earlier)

    written = ensure_outputs(
        transcript, formats=["docx"], output_dir=out_dir, stem="talk",
        existing=[str(path)],
    )

    assert [Path(p) for p in written] == [path]
    assert path.read_bytes() == earlier, "an adopted file was rewritten"


def test_write_all_reuses_its_own_docx_rendered_at_another_time(tmp_path):
    """The partial-publication retry must adopt the DOCX its own attempt wrote."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    (written,) = write_all(
        transcript, formats=["docx"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )
    earlier = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    written.write_bytes(earlier)
    mtime = written.stat().st_mtime_ns

    (again,) = write_all(
        transcript, formats=["docx"], output_dir=out_dir, stem="talk",
        reuse_published=True,
    )

    assert again == written
    assert written.read_bytes() == earlier, "an adopted file was rewritten"
    assert written.stat().st_mtime_ns == mtime


def test_atomic_write_adopts_a_docx_that_differs_only_in_member_times(tmp_path):
    """The primitive itself, with the format declared: adopt, do not rewrite."""
    transcript = _transcript()
    earlier = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    path = tmp_path / "talk.docx"
    path.write_bytes(earlier)
    mtime = path.stat().st_mtime_ns

    atomic_write_bytes(
        path, render_bytes(transcript, "docx"),
        reuse_identical=True, reuse_format="docx",
    )

    assert path.read_bytes() == earlier, "an adopted file was rewritten"
    assert path.stat().st_mtime_ns == mtime


def test_helper_adopts_a_later_real_docx_render(tmp_path):
    """The far side of a real clock boundary, observed then adopted.

    ``_render_docx_across_a_clock_boundary`` produces a genuine second render
    (bounded 2.1s waits, no inference from the source); that exact pair is then
    handed to the reuse rule, so the file on disk is one this renderer really
    wrote at another time.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    _before, later = _render_docx_across_a_clock_boundary(transcript)
    path = out_dir / "talk.docx"
    path.write_bytes(later)

    written = ensure_outputs(
        transcript, formats=["docx"], output_dir=out_dir, stem="talk",
        existing=[str(path)],
    )

    assert [Path(p) for p in written] == [path]
    assert path.read_bytes() == later, "an adopted file was rewritten"


# --- the two real resume routes --------------------------------------------


def _partially_published_docx_job(tmp_path, fake_pipeline, monkeypatch):
    """A job in ERROR with its DOCX published and its ``txt`` not.

    ``formats=["docx", "txt"]`` publishes in that order, so failing the second
    publish leaves exactly the partial state the retry has to finish.
    """
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    _fail_publish_once(monkeypatch, ".txt")

    request = SubmissionRequest(
        source=str(source),
        formats=["docx", "txt"],
        output_dir=str(out_dir),
        model="tiny",
        device="cpu",
    )
    job = submit_request(store, request, background=False)

    assert store.get(job.id).state is JobState.ERROR, store.get(job.id).error
    assert engine.calls == 1
    docx_path = out_dir / f"clip-{job.id}.docx"
    txt_path = out_dir / f"clip-{job.id}.txt"
    assert docx_path.is_file(), "the first format should have published"
    assert not txt_path.exists(), "the second format is the one that failed"
    return store, engine, docx_path, txt_path, job.id


def test_partial_docx_resume_adopts_its_own_earlier_docx_without_transcribing_again(
    tmp_path, monkeypatch, fake_pipeline
):
    """The retry must finish a job it left half-published, from the stored output.

    The recorded DOCX is a real render of the same transcript whose member
    timestamps were drawn again, so it is not byte-equal to a fresh render -
    only the ZIP date/time fields differ. The engine must not run again: the
    resume reuses the stored transcript and the published DOCX.
    """
    store, engine, docx_path, txt_path, job_id = _partially_published_docx_job(
        tmp_path, fake_pipeline, monkeypatch
    )
    # Replace the published DOCX with a real render of the *job's own* stored
    # transcript whose member times moved: not byte-equal, but the same
    # publication, which is what the retry is supposed to recognise.
    stored = transcript_for_job(store.get(job_id))
    assert stored is not None
    earlier = _docx_with_a_changed_member(stored, _OLD_DOS_TIME)
    docx_path.write_bytes(earlier)
    assert earlier != render_bytes(stored, "docx")
    mtime = docx_path.stat().st_mtime_ns

    resumed = resume_job(store, job_id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert resumed.id == job_id
    assert engine.calls == 1, "a resume must not transcribe again"
    assert {Path(p).stem for p in resumed.outputs} == {f"clip-{job_id}"}
    assert docx_path.read_bytes() == earlier, "the reused DOCX changed bytes"
    assert docx_path.stat().st_mtime_ns == mtime, (
        "the adopted DOCX was rewritten instead of reused"
    )
    assert txt_path.is_file(), "the missing format must be finished"
    assert "hello world" in txt_path.read_text(encoding="utf-8")


def test_done_resume_adopts_its_own_docx_without_a_render_or_a_refetch(
    tmp_path, fake_pipeline, monkeypatch
):
    """The DONE route: adopt the stored DOCX, no engine run, no source fetch.

    A completed job's explicit resume renders each format from the *stored*
    transcript and reuses the file already on disk. The recorded DOCX differs
    from a fresh render only in its member times, which must not turn a
    completed job's own output into a mismatch.
    """
    out_dir, engine = fake_pipeline
    source = tmp_path / "clip.wav"
    source.write_bytes(b"fake media")
    store = MemoryJobStore()
    request = SubmissionRequest(
        source=str(source), formats=["docx"], output_dir=str(out_dir),
        model="tiny", device="cpu",
    )
    job = submit_request(store, request, background=False)
    assert store.get(job.id).state is JobState.DONE, store.get(job.id).error
    (output,) = (Path(p) for p in job.outputs)
    stored = transcript_for_job(store.get(job.id))
    assert stored is not None
    earlier = _docx_with_a_changed_member(stored, _OLD_DOS_TIME)
    output.write_bytes(earlier)

    # Re-acquisition is what the resume must not need: make either the fetch or
    # the engine fatal if it is reached. Neither should be - the transcript is
    # in the checkpoint and the DOCX is already published.
    def forbidden(*args, **kwargs):
        raise AssertionError("the resume re-acquired the source or re-decoded audio")

    monkeypatch.setattr(pipeline, "fetch_media", forbidden)
    monkeypatch.setattr(pipeline, "extract_audio", forbidden)
    engine.calls = 0

    resumed = resume_job(store, job.id, background=False)

    assert resumed.state is JobState.DONE, resumed.error
    assert engine.calls == 0, "a DONE reuse must not transcribe again"
    assert output.read_bytes() == earlier, "an adopted DOCX was rewritten"
    assert [Path(p) for p in resumed.outputs] == [output]


# --- what must still fail closed -------------------------------------------


def test_helper_refuses_a_docx_with_changed_member_content(tmp_path):
    """A same-name, same-size content edit inside a member is refused."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    earlier = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    # Replace one byte of one member's content with another, keeping the archive
    # otherwise identical in structure. This is not a timestamp change.
    tampered = _docx_with_a_same_length_member_edit(earlier)
    assert tampered != earlier
    path = out_dir / "talk.docx"
    path.write_bytes(tampered)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == tampered, "the recorded file was modified"
    assert not list(out_dir.glob("*.tmp"))


def test_helper_refuses_a_truncated_docx(tmp_path):
    """A corrupt/truncated archive is refused, never adopted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    whole = render_bytes(transcript, "docx")
    truncated = whole[: len(whole) // 2]
    path = out_dir / "talk.docx"
    path.write_bytes(truncated)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == truncated


def test_helper_refuses_a_docx_with_a_duplicate_member(tmp_path):
    """An archive carrying a member twice cannot be compared and is refused."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    duplicated = _docx_with_a_duplicate_member(render_bytes(transcript, "docx"))
    path = out_dir / "talk.docx"
    path.write_bytes(duplicated)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == duplicated


def test_helper_refuses_a_docx_with_an_extra_foreign_member(tmp_path):
    """A member a real render does not produce cannot be adopted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    foreign = _docx_with_an_extra_member(render_bytes(transcript, "docx"))
    path = out_dir / "talk.docx"
    path.write_bytes(foreign)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == foreign


def test_write_all_still_refuses_a_differing_docx(tmp_path):
    """The retry route keeps no-clobber for a genuinely different DOCX."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    expected = out_dir / "talk.docx"
    expected.write_bytes(b"not a docx at all")

    with pytest.raises(FileExistsError):
        write_all(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            reuse_published=True,
        )

    assert expected.read_bytes() == b"not a docx at all"
    assert not list(out_dir.glob("*.tmp"))


def test_fresh_write_all_never_adopts_an_existing_docx(tmp_path):
    """A fresh run keeps the strict no-clobber rule, timestamps aside."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    existing = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    expected = out_dir / "talk.docx"
    expected.write_bytes(existing)

    with pytest.raises(FileExistsError):
        write_all(transcript, formats=["docx"], output_dir=out_dir, stem="talk")

    assert expected.read_bytes() == existing, "the existing file was modified"


# --- other formats keep the old rules --------------------------------------


def test_reuse_identical_without_a_declared_format_stays_byte_exact(tmp_path):
    """A caller declaring no format keeps the documented byte rule (no new format)."""
    transcript = _transcript()
    published = render_bytes(transcript, "docx")
    earlier = _docx_with_a_changed_member(transcript, _OLD_DOS_TIME)
    path = tmp_path / "talk.docx"
    path.write_bytes(published)

    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, earlier, reuse_identical=True)

    assert path.read_bytes() == published, "the existing file was modified"
    assert not list(tmp_path.glob("*.tmp"))


def test_write_all_still_refuses_a_differing_non_docx_output(tmp_path):
    """Widening DOCX must not widen any other format."""
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


# --- helpers that build the refuse cases -----------------------------------


def _docx_with_a_same_length_member_edit(data: bytes) -> bytes:
    """A real DOCX with one byte of the main document part's XML changed.

    Re-zipped with the members untouched otherwise, so the only difference from
    the input is content, not time.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        target = "word/document.xml"
        assert target in src.namelist(), src.namelist()
        payload = bytearray(src.read(target))
        # Flip one byte of the XML body, keeping the member length identical.
        payload[len(payload) // 2] ^= 0x20
        for info in src.infolist():
            content = bytes(payload) if info.filename == target else src.read(info.filename)
            dst.writestr(info, content)
    return buffer.getvalue()


def _docx_with_a_duplicate_member(data: bytes) -> bytes:
    """A real DOCX with one member name written twice into the archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        target = src.namelist()[0]
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(target, src.read(target))
    return buffer.getvalue()


def _docx_with_an_extra_member(data: bytes) -> bytes:
    """A real DOCX carrying one extra, foreign member."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr("word/foreign.xml", b"<foreign/>")
    return buffer.getvalue()
