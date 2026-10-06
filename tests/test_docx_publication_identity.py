"""Same-job resume of a **DOCX** output across a changed render clock (G3A).

``ensure_outputs`` and ``write_all`` reuse an already-published file only when it
*is* the file this run would publish (``render._is_own_publication``). For every
format but the two binary ones that rule is strict byte equality. PDF has one
documented widening (reportlab writes a random document id and the render time
into every render, and those fields are blanked before the bytes are compared).

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
timestamps actually cross a boundary. One control is real and sleep-bounded to a
single 2.1s wait, so the timestamp-variance premise is observed here and is not
inferred from the source.

The rule the product uses is *bytes-based, timestamps-only*. Each archive's
local and central DOS date/time pairs - the only fields a render clock varies -
are overwritten with a constant in place, and every other byte is then required
to match. That keeps member names, compression methods, flags, extra fields,
per-member and archive comments, attributes and the compressed member data
itself inside the compared bytes, so a change to any meaningful ZIP metadata is
refused rather than silently forgiven. A timestamp cannot change a file's
length, so the two sides must also be the same length, which refuses a
bomb-shaped archive before any member is read and means nothing is ever
decompressed.

Everything else must still fail closed: a content change, a corrupt archive, a
duplicate member, a foreign member shape, a renamed member, an
oversized or bomb-shaped archive, and a caller that declares no format all keep
the strict rule. Nothing here adds a new output format - DOCX is an existing one
and only its reuse rule is at issue.

The ``*_reuses_*``/``*_adopts_*`` cases and the two resume routes turn RED on the
strict byte comparison and are what the widened rule must make pass. The
``*_refuses_*``/``*_still_refuses_*``/``*_never_adopts_*``/``*_stays_byte_exact``
cases are the no-clobber and resource contract that holds today and must keep
holding after the widening. The half-edited timestamp pair, the renamed member
and the declared-size liar are archive shapes the comparator has to reject for
reasons of its own - an inconsistent header, a compared byte, an untrusted field
- not because the bytes differ in content.
"""

from __future__ import annotations

import importlib.util
import io
import time
import zipfile
from pathlib import Path

import pytest

from tests.test_done_output_integrity import _transcript
from tests.test_partial_output_resume import _CountingEngine, _fail_publish_once
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import load_checkpoint, transcript_for_job
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Transcript
from textflowkit.core.submission import SubmissionRequest, resume_job, submit_request
from textflowkit.render import (
    _docx_normalised,
    atomic_write_bytes,
    ensure_outputs,
    render_bytes,
    write_all,
)
from textflowkit.sources.detect import resolve_source

# A DOS date/time two seconds past the epoch, well outside any window a live
# render can land in. Used only where a fixed, readable older value is wanted;
# ``_render_docx_across_a_clock_boundary`` produces the value the real clock
# actually writes.
_OLD_DOS_TIME = (1980, 1, 1, 0, 0, 2)

# The member ``test_helper_refuses_a_docx_bomb`` blows up. It is a real member of
# a python-docx render, so the archive keeps the same member set and order and
# the refusal has to come from the byte bound rather than from a name mismatch.
_BOMB_MEMBER = b"word/document.xml"


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


def _docx_member_names(data: bytes) -> list[str]:
    """Every member name, in file order, as a plain reader sees them."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


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


def _render_docx_across_a_clock_boundary(transcript):
    """Render ``transcript`` twice with exactly one wait between the two.

    The two renders are the same transcript, so the only difference python-docx
    can introduce is the ZIP members' date/time. The wait is a single 2.1s
    sleep - one DOS second past a boundary - so the two renders land in different
    two-second buckets on any host whose clock is not far coarser than DOS's own
    2s resolution. The *assertion* that this is what happened stays here: the
    caller must be told if the environment cannot vary the clock, so the premise
    is measured rather than assumed. Missing the `docx` extra is the one case
    that skips, because that is a dependency absence and not a failed premise.

    Returns ``(before, after)``.
    """
    if importlib.util.find_spec("docx") is None:
        pytest.skip("DOCX export requires the optional 'docx' dependency")
    before = render_bytes(transcript, "docx")
    time.sleep(2.1)
    after = render_bytes(transcript, "docx")
    assert after != before, (
        "two renders 2.1s apart produced identical bytes, so the clock could not "
        "be advanced across a DOS-second boundary and the timestamp premise is "
        "unproven on this host"
    )
    assert _docx_bytes_equal_ignoring_member_times(before, after), (
        "two renders of one transcript changed member content, not just the "
        "member times this module is about"
    )
    return before, after


def _docx_with_a_changed_member(transcript, rewrite) -> bytes:
    """A real DOCX with every member's stamp set to ``rewrite``, in place.

    Used where a fixed, obviously-older timestamp is clearer than a live clock
    (the DONE-resume/partial-resume adoption cases). The two DOS words are
    overwritten *in the bytes* - no re-zip, no recompression - so the result
    differs from the renderer's own output in exactly the four bytes per header
    that a real clock moves, which is the premise these cases rest on. Writing a
    fresh archive through ``zipfile`` would risk recompressing the members and
    changing bytes that are not timestamps, which would make the test prove the
    wrong thing.
    """
    return _docx_with_stamps_patched(render_bytes(transcript, "docx"), rewrite)


def _docx_with_stamps_patched(data: bytes, rewrite) -> bytes:
    """``data`` whose local and central DOS stamp pairs are all set to ``rewrite``.

    Walks the archive with ``zipfile`` so each stamp pair is located by the real
    header offsets, then patches all four bytes of each pair. The local and
    central copies get the same value, which is what a renderer writes and what a
    consistent archive has.
    """
    dostime = (rewrite[3] << 11) | (rewrite[4] << 5) | (rewrite[5] // 2)
    dosdate = ((rewrite[0] - 1980) << 9) | (rewrite[1] << 5) | rewrite[2]
    pair = dostime.to_bytes(2, "little") + dosdate.to_bytes(2, "little")
    patched = data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            for at in (info.header_offset + 10, _central_stamp_offset(data, info.header_offset)):
                patched = patched[:at] + pair + patched[at + 4 :]
    return patched


def _central_stamp_offset(data: bytes, local_header_offset: int) -> int:
    """The central record's stamp offset, found from the local header's position.

    The central directory is not reached through ``ZipInfo`` (it exposes only the
    local header offset), so the record is found by matching the local header
    offset it stores: scan the central records and return the one whose
    ``local-header-offset`` field equals ``local_header_offset``.
    """
    offset = 0
    while True:
        offset = data.find(b"PK\x01\x02", offset)
        if offset < 0:
            raise AssertionError("no central record for a local header")
        stored = int.from_bytes(data[offset + 42 : offset + 46], "little")
        if stored == local_header_offset:
            return offset + 12
        offset += 4


def _docx_with_a_central_directory_only_time(data: bytes, rewrite) -> bytes:
    """A real DOCX whose central-directory stamps move but whose local ones do not.

    ``zipfile`` rewrites both copies from ``ZipInfo.date_time``, so the split is
    made on the finished bytes: for each central-directory record the DOS
    date/time field is replaced in place, leaving the local file header at the
    original value. The archive is structurally intact and still readable - what
    is inconsistent is only that its two copies of one member's time disagree,
    which is what the comparator must notice.
    """
    patched = data
    offset = 0
    while True:
        offset = patched.find(b"PK\x01\x02", offset)
        if offset < 0:
            return patched
        # In the central record: signature (4) + 4 version/system bytes (4) +
        # flag bits (2) + method (2), then the two 16-bit time and date words.
        dostime = (rewrite[3] << 11) | (rewrite[4] << 5) | (rewrite[5] // 2)
        dosdate = ((rewrite[0] - 1980) << 9) | (rewrite[1] << 5) | rewrite[2]
        at = offset + 12
        patched = (
            patched[:at]
            + dostime.to_bytes(2, "little")
            + dosdate.to_bytes(2, "little")
            + patched[at + 4 :]
        )
        offset += 4


def _docx_with_a_flate_bomb(data: bytes, member: bytes, *, megabytes: int) -> bytes:
    """A real DOCX with one existing member replaced by ``megabytes`` of zeroes.

    The member keeps its name and position, so the two archives have the same
    member *set*; a comparator that compared only names, or that reached for the
    member's declared size, could be fooled here. ``zipfile`` compresses the
    zeroes at deflate's ceiling, so the file stays small while the member really
    would inflate to far more than the trusted render it is compared against.
    The bytes-based comparison refuses it on the archive's own length (a
    timestamp cannot change a size) and never inflates it.
    """
    payload = bytes(megabytes * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            content = payload if info.filename.encode() == member else src.read(info.filename)
            dst.writestr(info, content)
    return buffer.getvalue()


def _docx_with_a_renamed_member(data: bytes) -> bytes:
    """A real DOCX whose member *names* are changed in place, one byte each.

    Every member still has a name and the archive keeps its length, member count
    and order; the compressed member data is untouched. The change is a real,
    meaningful ZIP field - the member name, which a docx reader keys on - and
    because it is length-preserving it cannot be caught by a size check, only by
    comparing the bytes that are not a timestamp. ``zipfile`` writes both the
    local header's and the central record's copy of each name, so both are
    rewritten to keep the two views consistent.
    """
    patched = data
    for signature, name_at in ((b"PK\x03\x04", 30), (b"PK\x01\x02", 46)):
        offset = 0
        while True:
            offset = patched.find(signature, offset)
            if offset < 0:
                break
            # Flip one ASCII byte of this record's name, keeping length.
            at = offset + name_at
            head = patched[at : at + 1]
            if head.isalpha():
                flipped = bytes([head[0] ^ 0x20])
                patched = patched[:at] + flipped + patched[at + 1 :]
            offset += 4
    return patched


def _docx_with_a_lying_member_size(data: bytes, member: bytes, *, claimed: int) -> bytes:
    """A real DOCX whose member *declares* an enormous uncompressed size.

    The local and central headers' ``file_size`` fields are overwritten to
    ``claimed``, a legal uint32; no such bytes exist and the member's real
    content is unchanged. The archive is still the same length, so it is not
    refused on length - it is refused because those two edited fields are, simply,
    bytes that differ from a fresh render's. The field may never be trusted as a
    budget, and it is not one here: nothing is ever decompressed.
    """
    # Offsets per the ZIP layout: the local header's file name length is at 26
    # with the name at 30 and `file_size` at 22; the central record's name length
    # is at 28, the name at 46 and `uncompressed_size` at 24.
    patched = data
    offset = 0
    while True:
        offset = patched.find(b"PK\x03\x04", offset)
        if offset < 0:
            break
        name_len = int.from_bytes(patched[offset + 26 : offset + 28], "little")
        name = patched[offset + 30 : offset + 30 + name_len]
        if bytes(name) == member:
            patched = (
                patched[: offset + 22]
                + claimed.to_bytes(4, "little")
                + patched[offset + 26 :]
            )
        offset += 4
    offset = 0
    while True:
        offset = patched.find(b"PK\x01\x02", offset)
        if offset < 0:
            return patched
        name_len = int.from_bytes(patched[offset + 28 : offset + 30], "little")
        name = patched[offset + 46 : offset + 46 + name_len]
        if bytes(name) == member:
            patched = (
                patched[: offset + 24]
                + claimed.to_bytes(4, "little")
                + patched[offset + 28 :]
            )
        offset += 4


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


# --- the normaliser itself, on a real archive -------------------------------


def test_normaliser_accepts_a_real_render_and_returns_the_same_length():
    """The direct control: a real render normalises, and to the same length.

    Every adoption case below depends on ``_docx_normalised`` returning bytes for
    a genuine python-docx archive. If it returned ``None`` the higher-level tests
    could only fail - or, worse, a refuse-all comparator could pass the whole
    refuse suite while adopting nothing. This pins the precondition on a *real*
    render rather than on a fixture: the value must be non-``None`` and the exact
    length of the archive it came from, since the rule blanks fields in place and
    must never change a byte's position. One render is enough here; the
    clock-straddling pair is the separate premise case above.
    """
    if importlib.util.find_spec("docx") is None:
        pytest.skip("DOCX export requires the optional 'docx' dependency")
    real = render_bytes(_transcript(), "docx")

    normalised = _docx_normalised(real)

    assert normalised is not None, "a real render must be a comparable archive"
    assert len(normalised) == len(real), (
        "normalisation may only overwrite the stamp fields in place, never resize"
    )
    # The stamps really are what moved: the constant differs from the render's own
    # bytes at a located stamp, and the normalised bytes differ from the input.
    assert normalised != real, "the control render's stamps were not blanked"


def test_normaliser_refuses_a_truncated_archive():
    """The negative of the control: an unreadable archive is ``None``, not bytes.

    A refuse-all comparator would pass the adoption suite by accident; the control
    above stops that. This is the other direction - a genuinely uncomparable blob
    must still be refused - so the pair pins that the normaliser *discriminates*
    rather than answering a constant.
    """
    real = render_bytes(_transcript(), "docx")
    assert _docx_normalised(real[: len(real) // 2]) is None


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
        engine="whisper",
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
    #
    # That transcript lives in the checkpoint, not the job field. A job in ERROR
    # has no DONE canonical transcript - the checkpoint text is the only copy the
    # resume will render from (see the checkpoint module's storage contract), so
    # `transcript_for_job` correctly answers None here and cannot be used to
    # reconstruct the file the retry must adopt. Read the same record the resume
    # does instead.
    failed = store.get(job_id)
    assert failed.state is JobState.ERROR, failed.state
    assert transcript_for_job(failed) is None, (
        "an ERROR job must not carry a final transcript; the checkpoint is the "
        "only durable copy of the transcript the retry renders from"
    )
    record = load_checkpoint(failed)
    assert record is not None and record.transcript is not None
    # Mirror the pipeline's publication so the bytes being varied are the job's
    # own render: the pipeline stamps the source and the classified platform onto
    # the transcript *before* handing it to the renderer, and a checkpoint that
    # captured it any earlier than those assignments would otherwise miss them.
    stored = Transcript.from_dict(record.transcript)
    stored.source = failed.source
    stored.platform = resolve_source(failed.source).platform
    earlier = _docx_with_a_changed_member(stored, _OLD_DOS_TIME)
    assert _docx_with_stamps_patched(render_bytes(stored, "docx"), _OLD_DOS_TIME) == earlier, (
        "the varied archive must differ from a fresh render of the same "
        "transcript only in ZIP member times"
    )
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
        engine="whisper", model="tiny", device="cpu",
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


# --- the bounds the widened comparison must keep ---------------------------


def test_helper_refuses_a_docx_whose_two_copies_of_a_member_time_disagree(tmp_path):
    """Only a *consistent* timestamp shift is forgiven, not a half-edited one.

    A member's time is stored twice; the comparator ignores the field, so a
    forged archive must not be able to move one copy and claim the other still
    matches. Here the central-directory copy moves and the local header does not,
    which is not something any render writes.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    real = render_bytes(transcript, "docx")
    forged = _docx_with_a_central_directory_only_time(real, _OLD_DOS_TIME)
    assert forged != real
    # The archive is still a readable ZIP with the same members, so the refusal
    # is about the metadata shape and not about an unreadable file.
    with zipfile.ZipFile(io.BytesIO(forged)) as forged_zf, zipfile.ZipFile(
        io.BytesIO(real)
    ) as real_zf:
        assert forged_zf.namelist() == real_zf.namelist()
    path = out_dir / "talk.docx"
    path.write_bytes(forged)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == forged, "the recorded file was modified"


def test_helper_refuses_a_docx_bomb_without_inflating_it(tmp_path):
    """A member that inflates far past the trusted render is refused, cheaply.

    A bomb is refused on its *length*: a timestamp-only difference cannot change
    a file's size, so an archive of a different length is turned away before any
    member is touched. The refusal therefore costs no inflation at all - the
    point of this test is that the comparison never expands the member. The
    timing bound below is deliberately generous; the substantive assertion is
    that the archive was refused and left untouched.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    real = render_bytes(transcript, "docx")
    bomb = _docx_with_a_flate_bomb(real, _BOMB_MEMBER, megabytes=16)
    # The same member set as the real render, so a name-set refusal cannot be
    # what saves us; the difference the rule sees is the archive's own bytes.
    assert _docx_member_names(bomb) == _docx_member_names(real)
    assert len(bomb) != len(real), "a bomb must differ in length to be refused cheaply"
    path = out_dir / "talk.docx"
    path.write_bytes(bomb)

    started = time.monotonic()
    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )
    elapsed = time.monotonic() - started

    assert path.read_bytes() == bomb, "the recorded file was modified"
    # 16 MiB of zeroes inflates in well under a second, so a whole second of
    # headroom still proves the work was not done.
    assert elapsed < 1.0, f"the comparison inflated the bomb ({elapsed:.2f}s)"


def test_helper_refuses_a_docx_that_changes_meaningful_zip_metadata(tmp_path):
    """A change outside the timestamp fields is refused, not silently forgiven.

    The member names are rewritten in place - same byte length, same member
    count, same order, structurally valid - so a comparator that forgot the names
    as "metadata" and compared only member contents would adopt it. Only the
    local and central DOS date/time pairs may be ignored, and this pins that the
    comparison forgives *only* those.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    real = render_bytes(transcript, "docx")
    renamed = _docx_with_a_renamed_member(real)
    assert len(renamed) == len(real), "the rewrite must not change the length"
    assert renamed != real
    # Still one member per name and still readable; only the names differ.
    assert len(_docx_member_names(renamed)) == len(_docx_member_names(real))
    path = out_dir / "talk.docx"
    path.write_bytes(renamed)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == renamed, "the recorded file was modified"


def test_docx_identity_never_decompresses_a_hostile_archive(tmp_path, monkeypatch):
    """A hostile archive is refused by structure and length, never inflated.

    The normaliser reads only headers; it must not call `ZipExtFile.read`. The
    bomb below differs in length, so it is refused before its members would even
    be located - and the spy proves no member read happened on either side.
    """
    transcript = _transcript()
    real = render_bytes(transcript, "docx")
    bomb = _docx_with_a_flate_bomb(real, _BOMB_MEMBER, megabytes=16)
    path = tmp_path / "talk.docx"
    path.write_bytes(bomb)

    def forbidden(*args, **kwargs):
        raise AssertionError("the comparison decompressed a member")

    monkeypatch.setattr(zipfile.ZipExtFile, "read", forbidden)
    with pytest.raises(FileExistsError):
        atomic_write_bytes(
            path, real, reuse_identical=True, reuse_format="docx",
        )

    assert path.read_bytes() == bomb, "the recorded file was modified"


def test_helper_refuses_a_docx_whose_member_lies_about_its_size(tmp_path):
    """The size *fields* are not trusted; only the bytes actually produced are."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    transcript = _transcript()
    real = render_bytes(transcript, "docx")
    # A legal ZIP field is a uint32, so the largest a `file_size` can claim is
    # 2**32 - 1. A larger value would make the fixture itself raise OverflowError
    # on `to_bytes(4)` before the product ever sees the archive, which pins
    # nothing. The claim is still absurd next to the render it is compared with.
    lying = _docx_with_a_lying_member_size(
        real, b"word/document.xml", claimed=0xFFFFFFFF
    )
    assert lying != real
    path = out_dir / "talk.docx"
    path.write_bytes(lying)

    with pytest.raises(FileExistsError):
        ensure_outputs(
            transcript, formats=["docx"], output_dir=out_dir, stem="talk",
            existing=[str(path)],
        )

    assert path.read_bytes() == lying, "the recorded file was modified"


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
