"""Streamed file uploads to a durable, owned scratch path.

A browser file is sent as the **raw request body** (a single ``fetch`` with the
file as the body), not as ``multipart/form-data``. That choice is deliberate:

- **No multipart dependency.** The mounted developer app has no multipart parser
  and adding one would pull a new dependency into the base install for a feature
  that only the UI needs.
- **No whole-file RAM buffer.** ``multipart`` parsers commonly accumulate the
  part before handing it over; here the body is streamed straight to disk in
  fixed-size chunks, so a 2 GiB upload never becomes a 2 GiB Python object. The
  in-memory footprint is one chunk.
- **A body cap enforced *while* streaming.** ``Content-Length`` is checked first
  when the browser sends one (``fetch`` with a ``File`` body does), and the
  running total is checked on every chunk regardless, so a chunked body without a
  length cannot exceed the cap either.

The destination is a fresh, uniquely named file under the uploads directory. The
name is generated here (a random token plus the original extension) and never
taken from caller input, so no request can choose where bytes land. The file is
owned by this request: if the stream is aborted, or the size cap is hit, or any
error occurs, the *partial* file is removed before the error propagates. Only
files this endpoint created are ever unlinked - the path is the one just
generated, never a caller-supplied one.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import unquote

from fastapi import Request

from textflowkit.ui import paths

#: Extensions we are willing to echo back onto a staged upload. The value is
#: used only to keep a helpful suffix for the decoder; it is never interpreted
#: as a path. Anything outside this set is dropped, so a hostile filename cannot
#: introduce a suffix the pipeline would treat specially.
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

#: Chunk size for the streamed copy. 1 MiB keeps syscall overhead low while
#: keeping peak memory to a single chunk.
_CHUNK = 1024 * 1024

#: Suffix for the UI-owned sidecar that remembers an upload's *original* name.
#: The staged file itself is named with a random token, so the original name a
#: person recognises is kept here, in a small sibling record, and never used as a
#: path. This is UI-owned metadata: it changes no core contract.
_META_SUFFIX = ".upload.json"

#: The only fields an upload sidecar carries. The stored name is display text,
#: never a path component.
_META_FIELDS = ("name", "bytes")

#: Longest original name kept for display, so a pathological name cannot bloat
#: the record or the UI. Truncation is display-only.
_NAME_MAX = 200


def decode_filename_header(raw: str | None) -> str | None:
    """The real filename from the upload header the browser sent.

    A filename travels in a header, and HTTP header values are Latin-1: the UI
    percent-encodes the name (``encodeURIComponent``) so a non-Latin-1 name such as
    ``meeting-中文.wav`` can be sent at all. This decodes it back. Decoding is
    **best-effort and safe**: a legacy plain-ASCII name contains no ``%`` and is
    returned unchanged, and a stray/invalid ``%`` sequence is left literal rather
    than raising, so the existing upload API behaves exactly as before. The result
    is still only ever used as a *label* and a suffix source, never as a path.
    """
    if raw is None:
        return None
    try:
        # utf-8 (not latin-1) so `%E4%B8%AD` decodes to 中; errors="strict" so a
        # stray/invalid `%` sequence raises and falls back to the literal below.
        return unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return raw
    except ValueError:  # pragma: no cover - a malformed escape; keep it literal
        return raw


def _clean_display_name(filename: str | None) -> str:
    """A safe, human-readable display name for an uploaded file.

    Path separators and control characters are stripped so the value is a plain
    label: it is shown with ``textContent`` in the UI and stored in a sidecar, but
    is never joined onto a directory or used to open a file.
    """
    if not filename:
        return ""
    # Drop any directory part a browser might include, then any residual
    # separators or control characters (a label, not a path).
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n\t")
    return name[:_NAME_MAX]


def meta_path_for(upload_path: Path) -> Path:
    """The sidecar record path for a staged upload."""
    return upload_path.with_name(upload_path.name + _META_SUFFIX)


def write_upload_meta(upload_path: Path, *, name: str, size: int) -> None:
    """Record the original display name and size for a staged upload.

    Best-effort and atomic: a failed write leaves the upload usable, the UI just
    falls back to a basename or job id for its label.
    """
    record = {"name": _clean_display_name(name), "bytes": int(size)}
    payload = {k: record[k] for k in _META_FIELDS}
    tmp = upload_path.with_name(upload_path.name + _META_SUFFIX + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, meta_path_for(upload_path))
    except OSError:  # pragma: no cover - label metadata is a convenience
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_upload_meta(upload_path: Path) -> dict:
    """The sidecar record for a staged upload, or ``{}`` if absent/unreadable."""
    try:
        data = json.loads(meta_path_for(upload_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _META_FIELDS if k in data}


def original_name_for_source(source: str | None) -> str | None:
    """The original upload filename for a stored ``source``, if it is one.

    A job submitted in ``mode=upload`` records the *staged path* as its source, so
    the recent-jobs list would otherwise show an opaque token. When the source is a
    file under the uploads directory, this returns the original name from its
    sidecar - the label a person recognises. Returns ``None`` for anything that is
    not a staged upload (a URL, a plain path, a source with no sidecar).
    """
    if not source:
        return None
    try:
        candidate = Path(source)
        uploads_root = paths.default_uploads_dir().resolve()
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if resolved.parent != uploads_root:
        return None
    name = read_upload_meta(resolved).get("name")
    return name if isinstance(name, str) and name else None


class UploadTooLarge(ValueError):
    """The body exceeded the configured upload cap."""


class UploadAborted(RuntimeError):
    """The client disconnected before the body finished."""


def _clean_suffix(filename: str | None) -> str:
    if not filename:
        return ""
    suffix = Path(filename).suffix
    return suffix if _SAFE_SUFFIX.match(suffix) else ""


def owned_upload_path(filename: str | None) -> Path:
    """A fresh, unique scratch path for one upload, under the uploads dir.

    The directory is created on demand. The file name is a random token, so two
    concurrent uploads cannot collide and no caller can steer the destination.
    """
    directory = paths.default_uploads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16) + _clean_suffix(filename)
    return directory / name


async def stream_to_owned_path(request: Request, filename: str | None) -> tuple[Path, int]:
    """Stream the request body to a new owned file and return ``(path, size)``.

    Raises :class:`UploadTooLarge` when the configured cap is exceeded and
    :class:`UploadAborted` when the client goes away mid-stream; in both cases
    the partial file is removed first. Any other exception is also cleaned up
    before it propagates, so a failed upload never leaves debris behind.
    """
    limit = paths.max_upload_bytes()
    # The header is percent-encoded by the browser for non-Latin-1 names (see
    # :func:`decode_filename_header`); decode once here so both the staged suffix
    # and the display-name sidecar use the real name. Idempotent and a no-op for a
    # plain name.
    filename = decode_filename_header(filename)

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdecimal() and int(declared) > limit:
        # Refuse before opening a file at all when the size is declared up front.
        raise UploadTooLarge(
            f"upload is {int(declared)} bytes, over the {limit}-byte limit"
        )

    path = owned_upload_path(filename)
    written = 0
    try:
        # "xb" is exclusive create: the random name should never exist, but if it
        # somehow does, refusing beats truncating a file we do not own.
        with path.open("xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    raise UploadTooLarge(
                        f"upload exceeds the {limit}-byte limit"
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Remove only the file this call just created. `unlink` on the exact
        # path we generated; `missing_ok` guards a race with our own cleanup.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if written == 0:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("upload body was empty")
    # Remember the original display name beside the staged file, so the recent-jobs
    # list can show the name a person recognises rather than the random token. This
    # is UI-owned metadata and changes no core contract; a failure here leaves the
    # upload usable (the label falls back to a basename or the job id).
    write_upload_meta(path, name=filename or "", size=written)
    return path, written
