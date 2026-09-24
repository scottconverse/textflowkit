"""Darwin `renamex_np(2)` with `RENAME_EXCL` as the second no-replace primitive (U33).

`atomic_write_bytes` publishes a fully staged file with `os.link`, which is
atomic and refuses an existing destination. Some filesystems have no hard links
at all - FAT32 and exFAT (most USB drives and SD cards) and some network shares
- and `link(2)` reports that as `EPERM` ("The filesystem containing oldpath and
newpath does not support the creation of hard links"). That used to fail the
whole export after the transcription had already run. Windows has `os.rename`
for that case (U31) and Linux has `renameat2(RENAME_NOREPLACE)` (U32); this
module is the macOS one.

`renamex_np(from, to, RENAME_EXCL)` is the Darwin equivalent: it moves the
staged inode to the destination name atomically, and with `RENAME_EXCL` the
destination already existing is an error rather than a replacement, so
no-clobber stays a kernel rule rather than a userspace check. The alternatives
outside review A4 suggested stay rejected for the same reasons as on the other
platforms: `open(dst, "xb")` puts a partly written file under the destination
name, and an existence check followed by `os.replace` loses to whoever creates
the file between the two calls.

Three things this module deliberately does not do:

- **No `os.rename`, and no `os.replace`.** POSIX `rename(2)` silently replaces
  an existing destination - measured on this unit's Windows host only as
  "POSIX is not Windows", so the premise is pinned by a real `rename(2)` in the
  `MACOS_ONLY` test rather than asserted here. Nothing in this module can
  clobber.
- **No retry on a collision.** `EEXIST` is the no-clobber verdict; it becomes
  `FileExistsError` and is left to the caller.
- **No degradation when the flag is unsupported.** `rename(2)` documents the
  `EEXIST` above as applying "on file systems that support it", and that
  support is a per *volume* property - the bit is `VOL_CAP_INT_RENAME_EXCL`
  (`getattrlist(2)`, surfaced to Cocoa as
  `URLResourceKey.volumeSupportsExclusiveRenaming`) - so this wrapper treats any
  non-`EEXIST` refusal as "this volume cannot publish safely" and raises
  `ExclusiveRenameUnsupported`. What makes that fail-closed choice safe rather
  than merely cautious is that the refusal cannot also be a silent replacement:
  see the `EEXIST` note below.

ABI facts, read from Apple's published headers rather than guessed:

- `bsd/sys/stdio.h`: `#define RENAME_EXCL 0x00000004` and
  `int renamex_np(const char *, const char *, unsigned int) __OSX_AVAILABLE(10.12)`.
- `bsd/sys/vnode_if.h`: `VFS_RENAME_EXCL = 0x00000004` - the same value on the
  kernel side, which is why the flag can be spelled out at all.
- `bsd/vfs/vfs_syscalls.c`: the existing-destination `EEXIST` is not raised by
  an individual filesystem's rename. It is raised by the *shared* rename path
  in the VFS layer, before any filesystem is called: read it in the releases
  for 10.12, 10.15, 12 and the current tree, and each has the same
  `if (tvp && ISSET(flags, VFS_RENAME_EXCL)) { error = EEXIST; }` with only a
  case-insensitive same-file rename exempted (10.12's form is the bare check;
  12 and later add that exemption, which cannot apply here because the two
  paths are always different files). So a filesystem that does not implement
  the flag cannot turn `RENAME_EXCL` into a replacement - it meets the same
  kernel check first. `renameatx_np` separately answers `EINVAL` for an unknown
  flag bit or for `RENAME_EXCL | RENAME_SWAP`.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import NoReturn

# `RENAME_EXCL` from Darwin's `bsd/sys/stdio.h` (`0x00000004`, the same value as
# the kernel's `VFS_RENAME_EXCL` in `bsd/sys/vnode_if.h`): fail with `EEXIST`
# instead of replacing an existing destination. Part of the published ABI,
# unlike a syscall number, so it is spelled out with its definition.
_RENAME_EXCL = 0x00000004

# The libc symbol's shape, taken from the C prototype `int renamex_np(const char
# *from, const char *to, unsigned int flags)`. Three arguments, unlike Linux's
# five: the Darwin call has no directory-descriptor form outside `renameatx_np`,
# and both paths are process paths.
_SIGNATURE = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)

# errno values that mean "this volume or kernel will not do an exclusive
# rename" rather than "the rename failed for some other reason". `rename(2)`
# names `EINVAL` for the invalid flag combinations `renameatx_np` rejects and
# does *not* say what a volume without `VOL_CAP_INT_RENAME_EXCL` returns, so
# every answer that could be that refusal is treated as one - `EINVAL`,
# `ENOTSUP`/`EOPNOTSUPP` (the same value on Darwin) and `ENOSYS`. Guessing the
# other way would mean publishing through something that could clobber. A
# collision (`EEXIST`) is deliberately *not* here: it is the no-clobber verdict
# and gets `FileExistsError` instead.
_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP})

# Indirection over the two ctypes helpers the call needs, so the errno handling
# can be driven by a test without a Darwin kernel in the room. Reading the errno
# immediately after the call is the whole point: it is the only record of why
# the kernel said no.
_get_errno = ctypes.get_errno
_set_errno = ctypes.set_errno


class ExclusiveRenameUnsupported(OSError):
    """This host or volume cannot rename onto a name without replacing what is there.

    Raised instead of degrading to a primitive that could clobber: the caller
    must fail closed, leaving nothing visible under the destination name.
    """


def _load_renamex_np():
    """The libc `renamex_np`, or `ExclusiveRenameUnsupported` if this host lacks it.

    Resolved on every call, with no cached answer, for the reason U32 measured
    on the Linux wrapper: a cache is shared by every thread in the process while
    being published in more than one step, so a second export job could observe
    "the probe has run" before the symbol existed and fail closed on a host that
    has it. Resolving costs one `CDLL` handle per call on a path only reached
    after a link has already failed, and leaves no cross-thread state that could
    be seen half-published.

    `ctypes.CDLL(None)` names the namespace the process itself is loaded from,
    which is how libc is reached without guessing a soname - `renamex_np` lives
    in libSystem, whose path is not part of the supported API surface. It is
    also why a non-Darwin host fails here: on Windows the argument is not even
    accepted and the call raises `TypeError` (measured on this unit's host),
    which is caught with the missing-symbol cases rather than left to escape as
    something that looks like a bug in the caller.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        symbol = libc.renamex_np
    except (AttributeError, OSError, TypeError) as exc:
        raise ExclusiveRenameUnsupported(
            f"libc renamex_np is unavailable on this host ({sys.platform}): {exc}"
        ) from exc
    symbol.argtypes = _SIGNATURE
    symbol.restype = ctypes.c_int
    return symbol


def _raise_rename_error(error_number: int, src: Path, dst: Path) -> NoReturn:
    """Turn the kernel's errno into the exception the caller's contract needs."""
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), os.fspath(dst))
    if error_number in _UNSUPPORTED_ERRNOS:
        raise ExclusiveRenameUnsupported(
            error_number,
            f"renamex_np(RENAME_EXCL) is not usable for {os.fspath(dst)!r}, so it cannot be "
            f"published without risking a clobber: {os.strerror(error_number)}",
        )
    raise OSError(error_number, os.strerror(error_number), os.fspath(src))


def rename_excl(src: Path, dst: Path) -> None:
    """Move `src` to `dst` atomically, refusing an existing `dst`.

    Raises `FileExistsError` when `dst` already exists (the no-clobber verdict),
    `ExclusiveRenameUnsupported` when this host or volume cannot do the
    operation safely, and `OSError` for any other kernel failure. An existing
    destination is never replaced and no partial output is ever visible under
    `dst`: the inode is moved whole, or nothing happens.
    """
    renamex_np = _load_renamex_np()
    # A failing C call leaves errno set. Zeroing it first means a non-zero
    # return can never be explained by a stale value from an unrelated earlier
    # call, so the answer below is always this call's.
    _set_errno(0)
    result = renamex_np(os.fsencode(src), os.fsencode(dst), _RENAME_EXCL)
    if result != 0:
        _raise_rename_error(_get_errno(), src, dst)
