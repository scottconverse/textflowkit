"""Renderer layer: canonical transcript -> output formats.

Two paths, because two kinds of output:

- `render()` returns **text** (txt, srt, vtt, md, json) and is what the MCP
  `get_transcript` tool returns inline.
- `render_bytes()` returns **bytes** and additionally handles the binary formats
  (docx, pdf), which exist for file export rather than for reading in a model's
  context.

`SUPPORTED_FORMATS` is the union and is what callers should validate against.
`TEXT_FORMATS` is what can be returned as a string.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from textflowkit.core.model import Transcript
from textflowkit.core.service import enforce_output_limit
from textflowkit.render import _rename_excl, _rename_noreplace
from textflowkit.render.markdown import render_markdown
from textflowkit.render.srt import render_srt
from textflowkit.render.txt import render_txt
from textflowkit.render.vtt import render_vtt

RENDERERS = {
    "txt": render_txt,
    "srt": render_srt,
    "vtt": render_vtt,
    "md": render_markdown,
}

TEXT_FORMATS = tuple(RENDERERS) + ("json",)
BINARY_FORMATS = ("docx", "pdf")
SUPPORTED_FORMATS = TEXT_FORMATS + BINARY_FORMATS

# What an omitted or empty `formats` means. It is spelled once, here, because
# every surface has to agree on it: a request that records one meaning and a
# checkpoint that records another can never be matched back to each other.
DEFAULT_FORMATS = ("json", "srt", "txt")

# Whether created files carry umask-derived permission bits. Windows reports
# the same synthetic mode for every file, so there is nothing to normalize
# there and `_ordinary_file_mode` is a no-op.
_HAS_UMASK = os.name == "posix"

# Whether `os.rename` refuses an existing destination, which is what makes it a
# usable second no-replace publication primitive. Windows: yes - it raises
# `FileExistsError` (WinError 183, measured on the unit's host) and moves within
# one volume atomically. POSIX: no - `os.rename` silently overwrites, so off
# Windows a link failure must reach some other primitive or fail closed.
_RENAME_REFUSES_EXISTING = os.name == "nt"

# Whether that other primitive is Linux's `renameat2(..., RENAME_NOREPLACE)`,
# which moves the staged inode atomically and refuses an existing destination
# with `EEXIST`. `os.name == "posix"` is not the test: macOS is POSIX with no
# `renameat2`, and a Linux filesystem without support for the flag answers
# `EINVAL` - so the syscall wrapper probes for the libc symbol and fails closed
# on either, rather than this flag promising something the host cannot do.
_IS_LINUX = sys.platform.startswith("linux")

# Whether that primitive is Darwin's `renamex_np(..., RENAME_EXCL)`, which moves
# the staged inode atomically and refuses an existing destination with `EEXIST`.
# The capability is per *volume* there (`VOL_CAP_INT_RENAME_EXCL`, surfaced as
# `volumeSupportsExclusiveRenaming`), so this flag says which call to try, not
# that it will work: the wrapper answers `ExclusiveRenameUnsupported` on a
# volume without it and the export then fails closed.
_IS_MACOS = sys.platform == "darwin"

# A PDF is the one format whose rendering is not byte-reproducible, but only
# three fields are to blame, and each is written afresh for every render:
#
# - the trailer's `/ID`, a random pair of 32-hex-character digests;
# - `/CreationDate` and `/ModDate`, the render time, written by reportlab's one
#   date formatter `D:%04d%02d%02d%02d%02d%02d%+03d'%02d'`.
#
# Measured on this unit's host (reportlab 5.0.1): two renders of one transcript
# back to back are the same length and differ only inside `/ID`; a third render
# two seconds later differs only in the two date values (2 bytes at second
# resolution). The date fields matter for a real resume, which renders long
# after the file was published, so a comparison that blanked only `/ID` would
# refuse the file this tool published itself.
#
# Comparison blanks exactly those three fields, in place and only when each
# occurs exactly once in exactly the shape below, and compares every other byte
# - so a tamper anywhere else, including a same-length edit, is refused. A file
# whose metadata has an unknown or ambiguous shape cannot be compared at all and
# is refused rather than accepted on a weaker check.
_PDF_ID_BLOCK = re.compile(rb"/ID\s*\[<([0-9A-Fa-f]{32})><([0-9A-Fa-f]{32})>\]")
_PDF_DATE_VALUE = re.compile(rb"/(CreationDate|ModDate)(\s*)\(D:(\d{14}[+-]\d{2}'\d{2}')\)")


def validate_export_requirements(formats: list[str]) -> None:
    """Fail before acquisition or inference when a requested export is unavailable."""
    normalized = {fmt.lower().lstrip(".") for fmt in formats}
    try:
        if "docx" in normalized:
            from textflowkit.render import docx  # noqa: F401 - import checks optional dependency
        if "pdf" in normalized:
            from textflowkit.render.pdf import _ensure_fonts

            _ensure_fonts()
    except ImportError as exc:
        raise ValueError(str(exc)) from exc


def _render_requested(
    transcript: Transcript, formats: list[str], title: str | None
) -> list[tuple[str, bytes]]:
    """Validate and render every format before touching any destination file."""
    rendered: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for fmt in formats:
        norm = fmt.lower().lstrip(".")
        if norm not in SUPPORTED_FORMATS:
            raise ValueError(f"unsupported format: {fmt}")
        if norm in seen:
            raise ValueError(f"duplicate output format: {fmt}")
        seen.add(norm)
        rendered.append((norm, render_bytes(transcript, norm, title=title)))
    enforce_output_limit(sum(len(data) for _, data in rendered))
    return rendered


def _is_regular_file(path: Path) -> bool:
    """Whether `path` names a regular file, without following a link.

    Publication here only ever links or renames a staged *regular* file into
    place, so a symlink or a directory at a destination name was not put there
    by this module. `lstat` is deliberate: a link must be refused, never read
    through - adopting one would return a path that resolves somewhere else
    (outside a confined output root, in the symlink case) as this job's output.
    """
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _has_length(path: Path, data: bytes) -> bool:
    """Whether `path` is a regular file of exactly `len(data)` bytes."""
    if not _is_regular_file(path):
        return False
    try:
        return path.stat().st_size == len(data)
    except OSError:
        return False


def _holds_exactly(path: Path, data: bytes) -> bool:
    """Whether `path` is a regular file whose bytes are exactly `data`.

    The bytes are read through the name rather than from a held handle, so this
    is a verdict about this moment and not a promise that the name still means
    the same file when the publish below runs - the no-clobber link below is
    what decides that.
    """
    if not _has_length(path, data):
        return False
    try:
        with path.open("rb") as handle:
            return handle.read() == data
    except OSError:
        return False


def _normalize_pdf_metadata(data: bytes) -> bytes | None:
    """`data` with the render-random PDF metadata blanked, or None.

    Blanked in place, so the result is the same length as the input: the two
    document-ID digests, and the `/CreationDate` and `/ModDate` values. Nothing
    else is touched, so every other byte still has to match a fresh rendering
    exactly.

    None means a field is absent, repeated, or not of the shape reportlab
    writes - see `_PDF_ID_BLOCK` and `_PDF_DATE_VALUE`. Such a file has no
    comparable form, and the caller must fail closed rather than fall back to a
    weaker check. The blanking is an in-memory comparison artefact only; no
    blanked bytes are ever written anywhere.
    """
    ids = list(_PDF_ID_BLOCK.finditer(data))
    if len(ids) != 1:
        return None
    dates = list(_PDF_DATE_VALUE.finditer(data))
    if len(dates) != 2:
        return None
    by_name = {match.group(1): match for match in dates}
    if set(by_name) != {b"CreationDate", b"ModDate"}:
        return None
    spans = [(ids[0].start(1), ids[0].end(1)), (ids[0].start(2), ids[0].end(2))]
    for field in (b"CreationDate", b"ModDate"):
        if data.count(b"/" + field) != 1:
            return None
        spans.append((by_name[field].start(3), by_name[field].end(3)))
    blanked = bytearray(data)
    for start, end in spans:
        blanked[start:end] = b"0" * (end - start)
    return bytes(blanked)


def _matches_a_pdf_rendering(path: Path, data: bytes) -> bool:
    """Whether `path` is the rendering `data`, up to its render-random metadata.

    That means every byte matches except the two document-ID digests and the
    `/CreationDate`/`/ModDate` values (`_normalize_pdf_metadata`). The link
    refusal of `_is_regular_file` applies here too: a recorded PDF is read
    through a name this job published, never through a link. A rendering that is
    itself uncomparable fails closed, so an unknown reportlab shape can never
    turn into an accepted file.
    """
    if not _is_regular_file(path):
        return False
    try:
        with path.open("rb") as handle:
            recorded = handle.read()
    except OSError:
        return False
    expected = _normalize_pdf_metadata(data)
    if expected is None:
        return False
    actual = _normalize_pdf_metadata(recorded)
    if actual is None:
        return False
    return actual == expected


def _is_own_publication(path: Path, data: bytes, norm: str | None) -> bool:
    """Whether `path` already holds what this call would publish as `norm`.

    Byte equality for every format but PDF. A PDF's rendering is not
    byte-reproducible - reportlab writes a random document id and the render
    time into every file - so that one format is compared with
    `_matches_a_pdf_rendering`, which blanks exactly those three fields, still
    requires every other byte to match, and refuses a file whose metadata is
    absent, repeated or unrecognisably shaped. `norm is None` means the caller
    declared no format and gets the strict byte rule.

    The rule is selected by this declared format and not by the file's suffix:
    the suffix is part of a name the caller chose, while the format is what the
    caller is actually publishing, and `atomic_write_bytes` is a public
    primitive whose documented byte-equality meaning must not silently widen for
    any name ending in `.pdf`.
    """
    if norm == "pdf":
        return _matches_a_pdf_rendering(path, data)
    return _holds_exactly(path, data)


def _ordinary_file_mode(directory: Path) -> int | None:
    """The mode an ordinary newly created file gets in `directory`, or None.

    `tempfile` always creates its staging file 0600, so publication through it
    would hand the destination that private mode; a published transcript should
    instead carry whatever mode a plainly created file would have had.

    The umask cannot be read without setting it, and setting it is
    process-global - unsafe when threads share the process - so it is measured
    rather than computed. The probe is created exactly the way an ordinary file
    is (`open` asks for 0o666 and the kernel applies the umask), and it is made
    in the same directory as the file being published, so it sees the same
    filesystem and the same umask.

    Returns None when the mode cannot be measured, which includes Windows,
    where files carry no umask-derived bits, and a name that is already taken.
    This is metadata rather than a safety property, so a filesystem that
    refuses the probe must not fail an export that would otherwise succeed.

    Cleanup is not best-effort, because it deletes by name. The probe is
    removed only once the exclusive create has proved this call owns it: a
    failed create means the name belongs to somebody else, and unlinking it
    would destroy a file this function never created. If the removal itself
    fails, that OSError is left to propagate and the export fails closed -
    a visible error is better than a probe left in the user's directory
    forever, and the staging file's own cleanup already behaves this way. An
    ambiguous failure between the create and the claim would leave a harmless
    empty file rather than delete a stranger's.
    """
    if not _HAS_UMASK:
        return None
    probe = directory / f".{os.urandom(8).hex()}.textflowkit-mode"
    created = False
    try:
        with open(probe, "xb") as handle:
            created = True
            return stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
    except OSError:
        return None
    finally:
        if created:
            probe.unlink(missing_ok=True)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    replace: bool = False,
    reuse_identical: bool = False,
    reuse_format: str | None = None,
) -> None:
    """Publish a complete file; optionally replace an explicitly chosen path.

    `reuse_identical` is the same-job resume case: a file an earlier attempt of
    this job already published is what this call would write, so leaving it in
    place finishes the publication instead of failing on it. Nothing is
    clobbered to make that work - only a file that holds this call's own
    publication is adopted (see `_is_own_publication`). A differing file still
    fails closed through the no-clobber link below, and the default (`False`)
    keeps a fresh attempt from taking over an existing file even when its bytes
    match.

    `reuse_format` names the output format being published, and is how the
    caller says whether "already published" can mean anything other than byte
    equality. Only `"pdf"` has a wider rule, and a narrow one: reportlab writes
    a random document id and the render time into every render, so those three
    fields are blanked before the comparison and every other byte still has to
    match, with an uncomparable shape refused rather than accepted
    (`_matches_a_pdf_rendering`). Every other format, and a caller that declares
    none, keeps the strict byte rule. `reuse_format` is only read when
    `reuse_identical` is set, and `replace` is an explicit instruction to write
    the path - so a replace never adopts, whatever the format.

    On POSIX the staging file is given the mode an ordinary file gets here
    before it is published, so the destination does not inherit the private
    0600 `tempfile` gave it. The staged bytes stay 0600 until they are fully
    written and fsynced, and nothing becomes visible under the destination name
    until the link or replace below.

    Publication is no-clobber whenever `replace` is false: a hard link normally,
    then a platform's second no-replace primitive after a link failure, since a
    filesystem without hard links (FAT32, exFAT, some network shares) may still
    have one - Windows' `os.rename`, Linux's `renameat2(RENAME_NOREPLACE)`, and
    macOS's `renamex_np(RENAME_EXCL)`. A platform with no second primitive
    re-raises the link's own error. A host that has one but cannot run it - no
    libc symbol, or a filesystem that will not take the flag - raises that
    wrapper's error instead (`NoReplaceRenameUnsupported` on Linux,
    `ExclusiveRenameUnsupported` on macOS): a different exception, with the link
    failure one or two steps down its `__cause__`/`__context__` chain rather
    than the error the caller sees. Either way nothing is published, and none of
    these paths writes bytes to the destination name directly, so a partial file
    is never visible there.
    """
    from textflowkit.core.paths import verify_output_file_target

    enforce_output_limit(len(data))
    verify_output_file_target(path)
    if reuse_identical and not replace and _is_own_publication(path, data, reuse_format):
        return
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        verify_output_file_target(path)
        # The destination inherits its mode from this inode on both publication
        # paths below, so the mode is set here: the bytes are already written
        # and fsynced, and neither name exposes them yet. The probe the mode
        # comes from lands in this same, just-verified directory.
        mode = _ordinary_file_mode(path.parent)
        if mode is not None:
            try:
                os.chmod(temp, mode)
            except OSError:
                pass  # a filesystem that cannot carry a mode must not fail the export
        # A hard link commits the fully-written temp file atomically and fails
        # if the destination already exists, unlike os.replace().
        if replace:
            os.replace(temp, path)
            temp = None
        else:
            try:
                os.link(temp, path)
            except OSError as exc:
                # Some filesystems have no hard links at all - FAT32 and exFAT
                # (most USB drives and SD cards) and some network shares - so
                # the link above cannot publish there. A collision is the
                # no-clobber verdict rather than a capability gap, so it is
                # raised instead of retried; the fallback below is for the
                # "this filesystem cannot link" answer.
                if isinstance(exc, FileExistsError):
                    raise
                # Windows offers an equivalent primitive: `os.rename` refuses an
                # existing destination with FileExistsError and moves within one
                # volume atomically. Linux offers `renameat2(RENAME_NOREPLACE)`,
                # which moves the staged inode and refuses an existing
                # destination with EEXIST - kernel-enforced like the link, and
                # unlike POSIX rename(2), which would replace whatever is there.
                # macOS offers `renamex_np(RENAME_EXCL)`, the same rule on
                # Darwin. All three keep the destination name either fully
                # published or untouched. The staging file is created in
                # `path.parent`, so the move stays on the destination's volume.
                if _RENAME_REFUSES_EXISTING:
                    os.rename(temp, path)
                elif _IS_LINUX:
                    # Raises NoReplaceRenameUnsupported when this Linux host
                    # cannot do it (no libc symbol, no filesystem support for
                    # the flag): fails closed as its own error, never through a
                    # plain rename, with the link failure left further down the
                    # exception chain.
                    _rename_noreplace.rename_noreplace(temp, path)
                elif _IS_MACOS:
                    # Raises ExclusiveRenameUnsupported on a macOS volume
                    # without exclusive renaming (no libc symbol, no
                    # `VOL_CAP_INT_RENAME_EXCL`): same fail-closed rule.
                    _rename_excl.rename_excl(temp, path)
                else:
                    # No second primitive on this platform: fail closed with
                    # the link's own error rather than degrading to a write
                    # that could expose or overwrite a destination.
                    raise
                temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def render(transcript: Transcript, fmt: str, *, title: str | None = None) -> str:
    """Render to text. Raises for a binary format - use `render_bytes`."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "json":
        return transcript.to_json()
    if fmt in BINARY_FORMATS:
        raise ValueError(
            f"'{fmt}' is a binary format; use render_bytes() (and export to a file)"
        )
    if fmt not in RENDERERS:
        raise ValueError(f"unsupported format: {fmt} (choose from {', '.join(SUPPORTED_FORMATS)})")
    if fmt == "md":
        return render_markdown(transcript, title=title)
    return RENDERERS[fmt](transcript)


def render_bytes(transcript: Transcript, fmt: str, *, title: str | None = None) -> bytes:
    """Render to bytes, for any supported format including binary ones."""
    fmt = fmt.lower().lstrip(".")

    if fmt == "docx":
        from textflowkit.render.docx import render_docx

        return render_docx(transcript, title=title or "Transcript")
    if fmt == "pdf":
        from textflowkit.render.pdf import render_pdf

        return render_pdf(transcript, title=title or "Transcript")
    if fmt not in TEXT_FORMATS:
        raise ValueError(f"unsupported format: {fmt} (choose from {', '.join(SUPPORTED_FORMATS)})")
    return render(transcript, fmt, title=title).encode("utf-8")


def _reusable_prior(prior: Path, expected: Path, data: bytes, norm: str) -> bool:
    """Whether a recorded path is this job's own rendering for `norm`.

    The name must be exactly the one this job's stem produces, so a
    same-suffix neighbour in the output directory is never handed back as the
    requested output; and the file must hold what this call would write, by the
    one rule both resume paths share (`_is_own_publication`: byte equality, or
    for a PDF the render-metadata-aware comparison).
    """
    if prior.name != expected.name:
        return False
    return _is_own_publication(prior, data, norm)


def ensure_outputs(
    transcript: Transcript,
    *,
    formats: list[str],
    output_dir: str | Path | None,
    stem: str,
    existing: list[str | Path] | None = None,
    title: str | None = None,
) -> list[Path]:
    """Write requested formats, reusing already-present outputs when possible.

    Resume must not redo transcription. Rendering missing files is cheap and
    keeps the checkpoint contract true even when the original output directory
    was removed between runs.

    A recorded output is reused only when it is the file this job published:
    inside the requested output directory, named exactly `{stem}.{format}`, a
    regular file, and holding what this call would write (see
    `_reusable_prior`; for a PDF that means every byte but the render-random
    metadata, and only when that metadata has the one shape reportlab writes).
    Nothing else is adopted. A missing output is rendered
    again from the transcript in hand, and an expected name already taken by
    other bytes fails closed through the no-clobber publish rather than being
    overwritten or returned in place of the transcript.
    """
    if output_dir is None:
        return []
    from textflowkit.core.paths import ensure_output_dir

    out_dir = ensure_output_dir(str(output_dir))
    rendered = _render_requested(transcript, formats, title)
    by_suffix: dict[str, Path] = {}
    for raw in existing or []:
        path = Path(raw)
        if path.parent.resolve() == out_dir.resolve():
            by_suffix[path.suffix.lower().lstrip(".")] = path
    written: list[Path] = []
    for norm, data in rendered:
        expected = out_dir / f"{stem}.{norm}"
        prior = by_suffix.get(norm)
        if prior is not None and _reusable_prior(prior, expected, data, norm):
            written.append(prior)
            continue
        atomic_write_bytes(expected, data)
        written.append(expected)
    return written


def write_all(
    transcript: Transcript,
    *,
    formats: list[str],
    output_dir: str | Path,
    stem: str,
    title: str | None = None,
    reuse_published: bool = False,
) -> list[Path]:
    """Write each requested format to `output_dir`.

    `reuse_published` is set only by a resume of the job that owns `stem`, whose
    earlier attempt named its files with that same stem. It finishes a partial
    publication by adopting a format that already holds this run's own
    rendering, and leaves every other collision to the strict no-clobber rule.
    Each format is published with its own name, so the format being rendered is
    what selects the reuse rule (`_is_own_publication`) - a PDF published by an
    earlier attempt is adopted even though reportlab renders it with a new
    document id and render time, while every other format still has to match
    byte for byte.
    """
    from textflowkit.core.paths import ensure_output_dir

    out_dir = ensure_output_dir(str(output_dir))
    rendered = _render_requested(transcript, formats, title)
    written: list[Path] = []
    for norm, data in rendered:
        path = out_dir / f"{stem}.{norm}"
        atomic_write_bytes(
            path, data, reuse_identical=reuse_published, reuse_format=norm
        )
        written.append(path)
    return written


__all__ = [
    "BINARY_FORMATS",
    "RENDERERS",
    "SUPPORTED_FORMATS",
    "TEXT_FORMATS",
    "atomic_write_bytes",
    "ensure_outputs",
    "render",
    "render_bytes",
    "render_markdown",
    "render_srt",
    "render_txt",
    "render_vtt",
    "validate_export_requirements",
    "write_all",
]
