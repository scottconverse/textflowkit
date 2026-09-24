"""Linux `renameat2(2)` as the second no-replace publication primitive (U32).

`atomic_write_bytes` publishes a fully staged file with `os.link`, which is
atomic and refuses an existing destination. Some filesystems have no hard links
at all - FAT32 and exFAT (most USB drives and SD cards) and some network shares
- and `link(2)` reports that as `EPERM` ("The filesystem containing oldpath and
newpath does not support the creation of hard links"). That used to fail the
whole export after the transcription had already run. Windows has `os.rename`
for that case (U31); this module is the Linux one.

`renameat2(dirfd, oldpath, dirfd, newpath, RENAME_NOREPLACE)` is the kernel-side
equivalent: it moves the staged inode to the destination name atomically, and
with `RENAME_NOREPLACE` it refuses an existing destination with `EEXIST`
instead of replacing it, so no-clobber stays a kernel rule rather than a
userspace check. The alternatives outside review A4 suggested stay rejected:
`open(dst, "xb")` puts a partly written file under the destination name, and an
existence check followed by `os.replace` loses to whoever creates the file
between the two calls.

Three things this module deliberately does not do:

- **No raw syscall number.** `SYS_renameat2` differs per architecture and is
  not part of the stable ABI, so the libc wrapper (glibc 2.28 and later, per
  rename(2) HISTORY) is resolved by name through `ctypes.CDLL(None)`, the
  portable way to reach the process's own libc. A libc without the symbol - and
  any non-POSIX host, where `ctypes.CDLL(None)` is not even valid and raises
  `TypeError` instead - fails closed with `NoReplaceRenameUnsupported`.
- **No fallback to a primitive that can clobber.** There is no `os.rename`
  here: POSIX `rename(2)` silently replaces an existing destination, which is
  exactly the property that makes it unusable off Windows.
- **No retry on a collision.** `EEXIST` is the no-clobber verdict; it becomes
  `FileExistsError` and is left to the caller.

Support is a property of the running kernel *and* the destination filesystem,
not something a version can be asked about in advance: `RENAME_NOREPLACE` needs
filesystem support that arrived gradually (ext4 in 3.15; btrfs, tmpfs and cifs
in 3.17; xfs in 4.0; many others in 4.9), and a filesystem that does not
support the flag answers `EINVAL` ("The filesystem does not support one of the
flags", rename(2)), while a kernel without the syscall answers `ENOSYS`. Both
mean "this host cannot publish safely here" and both fail closed, rather than
degrading to a plain rename.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import NoReturn

# `AT_FDCWD` from the kernel's `include/uapi/linux/fcntl.h`: resolve a relative
# path against the calling process's own working directory, as `rename(2)` does.
# It is the value for the *directory descriptors*; both paths passed below are
# already process paths, so neither directory is opened and the constant is
# never used to name a real file descriptor. It is part of the kernel's stable
# ABI and the same on every Linux architecture, and `os` does not expose it, so
# it is spelled out with its definition rather than guessed.
_AT_FDCWD = -100

# `RENAME_NOREPLACE` from the kernel's `include/uapi/linux/fs.h` (`1 << 0`):
# refuse to replace an existing destination. Unlike the syscall number, this
# flag value is ABI and identical on every Linux architecture.
_RENAME_NOREPLACE = 1

# The libc symbol's shape, taken from the C prototype `int renameat2(int olddirfd,
# const char *oldpath, int newdirfd, const char *newpath, unsigned int flags)`.
_SIGNATURE = (
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
)

# errno values that mean "this host will not do a no-replace rename" rather than
# "the rename failed for some other reason". `EINVAL` is what rename(2)
# documents for a filesystem that does not support the flag, `ENOSYS` for a
# kernel without the syscall; `EOPNOTSUPP` and `ENOTSUP` are the same value on
# Linux but a driver may report either, so both names are listed. A collision
# (`EEXIST`) is deliberately *not* here: it is the no-clobber verdict and gets
# `FileExistsError` instead.
_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP})

# Indirection over the two ctypes helpers the call needs, so the errno handling
# can be driven by a test without a Linux kernel in the room. Reading the errno
# immediately after the call is the whole point: it is the only record of why
# the kernel said no.
_get_errno = ctypes.get_errno
_set_errno = ctypes.set_errno


class NoReplaceRenameUnsupported(OSError):
    """This host cannot rename onto a name without replacing what is there.

    Raised instead of degrading to a primitive that could clobber: the caller
    must fail closed, leaving nothing visible under the destination name.
    """


# The resolved libc symbol, the fact that the probe has run, and - when it found
# nothing - why. One probe per process: the libc's symbol set does not change
# underfoot, and asking again would only repeat the same answer.
_symbol = None
_probed = False
_probe_error: NoReplaceRenameUnsupported | None = None


def _load_renameat2():
    """The libc `renameat2`, resolved once, or `NoReplaceRenameUnsupported`.

    `ctypes.CDLL(None)` names the namespace the process itself is loaded from,
    which is how libc is reached without guessing a soname - glibc is
    `libc.so.6` and musl is `libc.so`, so a name would be a portability claim
    this module cannot back. It is also why a non-POSIX host fails here: on
    Windows the argument is not even accepted and the call raises `TypeError`,
    which is caught with the missing-symbol cases rather than left to escape as
    something that looks like a bug in the caller.
    """
    global _symbol, _probed, _probe_error
    if not _probed:
        _probed = True
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            symbol = libc.renameat2
        except (AttributeError, OSError, TypeError) as exc:
            _probe_error = NoReplaceRenameUnsupported(
                f"libc renameat2 is unavailable on this host ({sys.platform}): {exc}"
            )
        else:
            symbol.argtypes = _SIGNATURE
            symbol.restype = ctypes.c_int
            _symbol = symbol
    if _symbol is None:
        if _probe_error is None:  # unreachable: a probe that finds nothing records why
            _probe_error = NoReplaceRenameUnsupported(
                f"libc renameat2 is unavailable on this host ({sys.platform})"
            )
        raise _probe_error
    return _symbol


def _raise_rename_error(error_number: int, src: Path, dst: Path) -> NoReturn:
    """Turn the kernel's errno into the exception the caller's contract needs."""
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), os.fspath(dst))
    if error_number in _UNSUPPORTED_ERRNOS:
        raise NoReplaceRenameUnsupported(
            error_number,
            f"renameat2(RENAME_NOREPLACE) is not usable for {os.fspath(dst)!r}, so it cannot be "
            f"published without risking a clobber: {os.strerror(error_number)}",
        )
    raise OSError(error_number, os.strerror(error_number), os.fspath(src))


def rename_noreplace(src: Path, dst: Path) -> None:
    """Move `src` to `dst` atomically, refusing an existing `dst`.

    Raises `FileExistsError` when `dst` already exists (the no-clobber verdict),
    `NoReplaceRenameUnsupported` when this host cannot do the operation safely,
    and `OSError` for any other kernel failure. An existing destination is
    never replaced and no partial output is ever visible under `dst`: the inode
    is moved whole, or nothing happens.
    """
    renameat2 = _load_renameat2()
    # A failing C call leaves errno set. Zeroing it first means a non-zero
    # return can never be explained by a stale value from an unrelated earlier
    # call, so the answer below is always this call's.
    _set_errno(0)
    result = renameat2(
        _AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE
    )
    if result != 0:
        _raise_rename_error(_get_errno(), src, dst)
