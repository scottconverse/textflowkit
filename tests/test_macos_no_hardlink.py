"""Publishing staged output on macOS where the filesystem has no hard links (U33).

The macOS half of outside-review A4. `atomic_write_bytes` commits a fully
written and fsynced staging file with `os.link`; some filesystems - FAT32 and
exFAT (most USB drives and SD cards) and some network shares - have no hard
links, and `link(2)` reports that as `EPERM`, so the export used to fail after
the whole transcription had already run. Windows answers with `os.rename` (U31)
and Linux with `renameat2(RENAME_NOREPLACE)` (U32); macOS is POSIX with
neither, so until now a link failure there simply failed the export.

Darwin's second no-replace primitive is `renamex_np(from, to, RENAME_EXCL)`.
`rename(2)` documents `EEXIST` "if the destination already exists" but only "on
file systems that support it", and the capability behind that is the volume bit
`VOL_CAP_INT_RENAME_EXCL` (`getattrlist(2)`, surfaced to Cocoa as
`volumeSupportsExclusiveRenaming`). So support is a property of the volume, not
of the OS version, and a volume without it must fail closed rather than degrade
- plain POSIX `rename(2)` replaces silently, `open(dst, "xb")` exposes a partly
written file under the destination name, and a check followed by `os.replace`
loses to whoever creates the file between the two calls.

**What this file does not prove.** It was written on Windows, so the tests that
reach the macOS branch set the module's platform flags and hand it a *double*
for the native call: that proves the module's side of the boundary, not that
Darwin behaves as the double does. The only real measurements are the
`MACOS_ONLY` tests, which need a macOS host and skip, with the reason, here.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import os
import sys
from pathlib import Path

import pytest

import textflowkit.render as render_mod

# The link failure, the "everything left in the directory" check, the
# competitor and the publication spy are the same facts on every platform, and
# were written for U32 first; they are imported rather than copied so the two
# platform modules cannot drift apart.
from tests.test_linux_no_hardlink import (
    _REAL_REPLACE,
    LINK_FAILURES,
    _entries,
    _fail_link,
    _plant_a_file,
    _spy_publication,
)
from tests.test_model import sample
from textflowkit.render import atomic_write_bytes, render_bytes, write_all

MACOS_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="renamex_np(RENAME_EXCL) is Darwin-only, so there is nothing real to call here",
)

# The answers that mean "this volume will not do an exclusive rename" rather
# than "the rename failed for some other reason". `rename(2)` does not say what
# an unsupported volume returns - only that `EEXIST` comes back "on file systems
# that support it" - so every errno that could be that refusal is treated as
# one, and the export fails closed. `EEXIST` is deliberately absent: it is the
# no-clobber verdict and becomes `FileExistsError`.
UNSUPPORTED_ANSWERS = [
    pytest.param(errno.EINVAL, id="einval-volume-refused-the-flag"),
    pytest.param(errno.ENOTSUP, id="enotsup-volume-lacks-the-capability"),
    pytest.param(errno.ENOSYS, id="enosys-no-renamex_np-in-the-kernel"),
]


def _native():
    """The wrapper module under test, by name so the package cannot shadow it."""
    return importlib.import_module("textflowkit.render._rename_excl")


def _macos_host(monkeypatch) -> None:
    """Put the module in the configuration a macOS host has.

    All three flags, because on this Windows host `_RENAME_REFUSES_EXISTING` is
    True and the Windows rename branch would otherwise answer first and hide
    the macOS one; on a macOS host the assignments restate the truth.
    """
    monkeypatch.setattr(render_mod, "_IS_MACOS", True, raising=False)
    monkeypatch.setattr(render_mod, "_IS_LINUX", False)
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False)


class _KernelDouble:
    """A stand-in for `renamex_np(RENAME_EXCL)`, carrying that call's contract.

    Refuses an existing destination with `FileExistsError`, otherwise moves the
    staged inode, and records the staged bytes read at call time, so a partly
    written staging file cannot pass. A double, not a measurement: the
    `MACOS_ONLY` tests are where the real call is made.
    """

    def __init__(self, fail_with: OSError | None = None) -> None:
        self.calls: list[tuple[Path, Path, bytes]] = []
        self.fail_with = fail_with

    def __call__(self, src, dst) -> None:
        src, dst = Path(src), Path(dst)
        self.calls.append((src, dst, src.read_bytes()))
        if self.fail_with is not None:
            raise self.fail_with
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "File exists", os.fspath(dst))
        _REAL_REPLACE(src, dst)


def _install_kernel(monkeypatch, fail_with: OSError | None = None) -> _KernelDouble:
    """Put the double in place of the real call, wherever it is called from."""
    kernel = _KernelDouble(fail_with)
    monkeypatch.setattr(_native(), "rename_excl", kernel)
    return kernel


class _RecordingSymbol:
    """A ctypes-shaped stand-in for the resolved libc symbol."""

    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.result


class _LibcWithTheSymbol:
    def __init__(self, symbol) -> None:
        self.renamex_np = symbol


class _LibcWithoutTheSymbol:
    """An older libc or a non-Darwin host: no `renamex_np` to find."""


class _TheKernelAsALibcSymbol:
    """A libc-shaped symbol: takes the ctypes argument tuple, performs the move.

    The shape `rename_excl` sees from the real libc - two path pointers, one
    unsigned flag, and an int back - so a test that swaps it in has the
    wrapper's own path exercised rather than bypassed, and errno is set through
    the same `ctypes.set_errno` the wrapper reads.
    """

    def __init__(self) -> None:
        self.argtypes = None
        self.restype = None

    def __call__(self, oldpath, newpath, _flags) -> int:
        source, destination = Path(os.fsdecode(oldpath)), Path(os.fsdecode(newpath))
        if os.path.lexists(destination):
            ctypes.set_errno(errno.EEXIST)
            return -1
        _REAL_REPLACE(source, destination)
        return 0


@pytest.fixture
def native():
    return _native()


@pytest.fixture
def kernel_symbol(native, monkeypatch):
    """Let a test drive the wrapper as though the kernel had answered."""
    symbol = _RecordingSymbol()
    monkeypatch.setattr(native, "_load_renamex_np", lambda: symbol)
    return symbol


@pytest.fixture
def kernel_errno(native, monkeypatch):
    """Let a test say what errno the kernel call left behind."""
    state: dict[str, int] = {}
    monkeypatch.setattr(native, "_get_errno", lambda: state["errno"])
    monkeypatch.setattr(native, "_set_errno", lambda value: state.__setitem__("cleared", value))
    return state


# --------------------------------------------------------------------------
# `atomic_write_bytes` on the macOS branch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code,message", LINK_FAILURES)
def test_the_staged_file_is_published_by_the_native_call(tmp_path, monkeypatch, code, message):
    """The defect: a free destination must be written, whole and by nothing else.

    The publication spy is the second half of the claim: the branch reached is
    `renamex_np(RENAME_EXCL)`, not a primitive that could replace an existing
    destination.
    """
    payload = b"complete staged bytes" * 100
    target = tmp_path / "talk.txt"
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, OSError(code, message))

    atomic_write_bytes(target, payload)

    assert calls == ["link"], "the macOS branch used a primitive that can replace a destination"
    assert len(kernel.calls) == 1, "the native exclusive rename was not called exactly once"
    staged, destination, staged_bytes = kernel.calls[0]
    assert destination == target
    assert staged != target
    assert staged.parent == tmp_path, "the staging file must be on the destination's volume"
    assert staged_bytes == payload, "the native call was handed a partly written file"
    assert target.read_bytes() == payload
    assert _entries(tmp_path) == ["talk.txt"], "the staging file was left behind"


def test_an_existing_file_is_refused_rather_than_overwritten(tmp_path, monkeypatch):
    """The fallback must carry the no-clobber rule, not replace it."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]
    assert len(kernel.calls) == 1


def test_a_file_created_during_the_failed_link_is_never_overwritten(tmp_path, monkeypatch):
    """The race the review's fixes would lose.

    The competitor appears *after* the destination was verified absent, inside
    the failed link, which is exactly the window a check plus `os.replace`
    would race. The `FileExistsError` can only come from the native call: the
    patched link raises `EPERM`.
    """
    target = tmp_path / "talk.txt"
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    monkeypatch.setattr(os, "link", _plant_a_file(target, b"planted by another writer"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"planted by another writer"
    assert _entries(tmp_path) == ["talk.txt"]
    assert len(kernel.calls) == 1


def test_same_job_resume_rules_hold_under_the_macos_fallback(tmp_path, monkeypatch):
    """Adopt only a byte-identical file of this job; refuse a differing one."""
    target = tmp_path / "talk.txt"
    atomic_write_bytes(target, b"one")
    _macos_host(monkeypatch)
    _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"one", reuse_identical=True)
    assert target.read_bytes() == b"one"

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"two", reuse_identical=True)

    assert target.read_bytes() == b"one", "a differing file was adopted or clobbered"
    assert _entries(tmp_path) == ["talk.txt"]


def test_an_unsupported_native_call_fails_closed(tmp_path, monkeypatch):
    """When the volume says it cannot, nothing is published and the staging goes.

    This is the case `volumeSupportsExclusiveRenaming` warns about: on a volume
    without the exclusive-rename capability there is no safe primitive left, so
    the export fails rather than falling back to something that could clobber.
    """
    unsupported = _native().ExclusiveRenameUnsupported
    target = tmp_path / "talk.txt"
    _macos_host(monkeypatch)
    _install_kernel(monkeypatch, fail_with=unsupported(errno.ENOTSUP, "not supported here"))
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(unsupported) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert isinstance(excinfo.value, OSError), "the caller's fail-closed contract is an OSError"
    assert not target.exists()
    assert _entries(tmp_path) == [], "the staging file was left behind"


def test_write_all_publishes_every_format_and_refuses_a_collision(tmp_path, monkeypatch):
    """A4's done-when over the real pipeline write path, plus its refusal."""
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))
    transcript = sample()

    paths = write_all(transcript, formats=["txt", "srt"], output_dir=tmp_path, stem="talk")

    assert [p.name for p in paths] == ["talk.txt", "talk.srt"]
    for path in paths:
        assert path.read_bytes() == render_bytes(transcript, path.suffix.lstrip("."))
    assert _entries(tmp_path) == ["talk.srt", "talk.txt"]
    assert len(kernel.calls) == 2

    (tmp_path / "talk.txt").write_bytes(b"someone else's bytes")
    with pytest.raises(FileExistsError):
        write_all(sample(), formats=["txt"], output_dir=tmp_path, stem="talk")
    assert (tmp_path / "talk.txt").read_bytes() == b"someone else's bytes"


# --------------------------------------------------------------------------
# Guards: the paths that must not change
# --------------------------------------------------------------------------


def test_a_working_link_never_reaches_the_macos_branch(tmp_path, monkeypatch):
    """The normal path is unchanged: one hard link, no native rename."""
    target = tmp_path / "talk.txt"
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, None)

    atomic_write_bytes(target, b"hello")

    assert calls == ["link"]
    assert kernel.calls == []
    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


def test_replace_true_still_replaces_and_never_reaches_the_macos_branch(tmp_path, monkeypatch):
    """`replace=True` is the caller's explicit choice and keeps `os.replace`."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"stale")
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"hello", replace=True)

    assert calls == ["replace"]
    assert kernel.calls == []
    assert target.read_bytes() == b"hello"


def test_a_collision_from_the_link_is_never_retried_as_the_macos_branch(tmp_path, monkeypatch):
    """`FileExistsError` is the no-clobber verdict, not a capability gap."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"first")
    _macos_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(
        monkeypatch, FileExistsError(errno.EEXIST, "File exists", os.fspath(target))
    )

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"second")

    assert calls == ["link"], "a collision was retried as the native rename"
    assert kernel.calls == [], "a collision reached the native rename"
    assert target.read_bytes() == b"first"


def test_a_host_with_no_macos_primitive_stays_fail_closed(tmp_path, monkeypatch):
    """A plain POSIX host: the link error propagates untouched."""
    assert render_mod._IS_MACOS == (sys.platform == "darwin")
    assert render_mod._RENAME_REFUSES_EXISTING == (os.name == "nt")
    assert render_mod._IS_LINUX == sys.platform.startswith("linux")
    monkeypatch.setattr(render_mod, "_IS_MACOS", False)
    monkeypatch.setattr(render_mod, "_IS_LINUX", False)
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False)
    kernel = _install_kernel(monkeypatch)
    target = tmp_path / "talk.txt"
    error = OSError(errno.EPERM, "Operation not permitted")
    _fail_link(monkeypatch, error)

    with pytest.raises(OSError) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert excinfo.value is error, "the link failure must propagate untouched"
    assert kernel.calls == [], "a host with no native primitive reached the macOS one"
    assert not target.exists()
    assert _entries(tmp_path) == [], "the staging file was left behind"


# --------------------------------------------------------------------------
# The native wrapper itself
# --------------------------------------------------------------------------


def test_the_call_passes_encoded_paths_and_the_excl_flag(native, kernel_symbol, tmp_path):
    """The arguments, and the ABI constants behind them.

    `RENAME_EXCL` is `0x00000004` in Darwin's `sys/stdio.h` (and
    `VFS_RENAME_EXCL` is the same value in `sys/vnode_if.h`); unlike a syscall
    number it is part of the published ABI, which is why the libc wrapper is
    used instead of `syscall()`.
    """
    src = tmp_path / "staged.tmp"
    dst = tmp_path / "talk.txt"
    src.write_bytes(b"staged")

    native.rename_excl(src, dst)

    assert kernel_symbol.calls == [(os.fsencode(src), os.fsencode(dst), native._RENAME_EXCL)]
    assert native._RENAME_EXCL == 0x00000004
    assert isinstance(kernel_symbol.calls[0][0], bytes), "paths must be fsencoded, not str"


def test_a_collision_is_reported_as_the_no_clobber_verdict(
    native, kernel_symbol, kernel_errno, tmp_path
):
    """`EEXIST` is the caller's `FileExistsError`, and is not a capability gap."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = errno.EEXIST

    with pytest.raises(FileExistsError) as excinfo:
        native.rename_excl(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert excinfo.value.errno == errno.EEXIST
    assert not isinstance(excinfo.value, native.ExclusiveRenameUnsupported)
    assert kernel_errno["cleared"] == 0, "errno was not cleared before the call"


@pytest.mark.parametrize("code", UNSUPPORTED_ANSWERS)
def test_an_unsupported_answer_fails_closed(native, kernel_symbol, kernel_errno, tmp_path, code):
    """A volume without the capability: refuse, never degrade to a plain rename."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = code

    with pytest.raises(native.ExclusiveRenameUnsupported) as excinfo:
        native.rename_excl(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert isinstance(excinfo.value, OSError), "the failure must still be an OSError"
    assert "renamex_np" in str(excinfo.value), "the error must name the primitive it could not use"


def test_any_other_kernel_failure_propagates_as_a_plain_oserror(
    native, kernel_symbol, kernel_errno, tmp_path
):
    """A real failure (a permission problem, say) is neither a collision nor a gap."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = errno.EPERM

    with pytest.raises(OSError) as excinfo:
        native.rename_excl(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert excinfo.value.errno == errno.EPERM
    assert not isinstance(excinfo.value, FileExistsError)
    assert not isinstance(excinfo.value, native.ExclusiveRenameUnsupported)


def test_the_loaded_symbol_is_given_the_declared_ctypes_signature(native, monkeypatch):
    """Two char pointers, one unsigned flag, and an int back."""
    symbol = _RecordingSymbol()
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithTheSymbol(symbol))

    assert native._load_renamex_np() is symbol
    assert symbol.argtypes == (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    assert symbol.restype is ctypes.c_int


def test_the_symbol_is_resolved_for_each_call_and_never_cached(native, monkeypatch):
    """No process-wide cached answer, so none can be seen half-published.

    U32 measured a fail-closed answer leaking across threads when the Linux
    wrapper cached its probe before publishing the symbol; this wrapper is
    written the same way so the same race cannot exist here.
    """
    symbol = _RecordingSymbol()
    loads: list[tuple] = []

    def cdll(*args, **kwargs):
        loads.append(args)
        return _LibcWithTheSymbol(symbol)

    monkeypatch.setattr(native.ctypes, "CDLL", cdll)

    assert native._load_renamex_np() is symbol
    assert native._load_renamex_np() is symbol
    assert len(loads) == 2, "a resolve was answered from a cache"
    assert [name for name in ("_symbol", "_probed", "_probe_error") if hasattr(native, name)] == []


def test_a_libc_without_the_symbol_is_reported_as_unsupported(native, monkeypatch):
    """`renamex_np` arrived in 10.12; an older libc must fail closed."""
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithoutTheSymbol())

    with pytest.raises(native.ExclusiveRenameUnsupported) as excinfo:
        native.rename_excl(Path("staged.tmp"), Path("talk.txt"))

    assert "renamex_np" in str(excinfo.value)


def test_the_real_branch_fails_closed_on_a_host_without_the_symbol(tmp_path, monkeypatch):
    """The real wrapper, in the real branch, on a host whose libc has no symbol.

    Only the platform flag and the link failure are simulated: this host's own
    libc probe decides, so the fail-closed answer is measured rather than faked.
    On Windows, `ctypes.CDLL(None)` is not even valid and raises `TypeError`
    instead, which is the same answer for the same reason.
    """
    native = _native()
    try:
        native._load_renamex_np()
    except native.ExclusiveRenameUnsupported:
        pass
    else:
        pytest.skip("this host has a real renamex_np symbol; see the MACOS_ONLY tests")

    target = tmp_path / "talk.txt"
    _macos_host(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(native.ExclusiveRenameUnsupported):
        atomic_write_bytes(target, b"hello")

    assert not target.exists(), "a host without the native primitive published anyway"
    assert _entries(tmp_path) == [], "the staging file was left behind"


# --------------------------------------------------------------------------
# The real call, on a macOS host. Skipped elsewhere, and skipped with its
# reason when the volume refuses - never by falling back.
# --------------------------------------------------------------------------


def _require_real_exclusive_rename(native, tmp_path) -> None:
    """Skip, naming the reason, when this host or volume cannot run the real call.

    The probe uses `tmp_path`, so the capability asked about is the capability
    of the filesystem the assertions below run on.
    """
    src = tmp_path / "capability-probe-src"
    dst = tmp_path / "capability-probe-dst"
    src.write_bytes(b"probe")
    try:
        native.rename_excl(src, dst)
    except native.ExclusiveRenameUnsupported as exc:
        pytest.skip(f"no usable renamex_np(RENAME_EXCL) on this host or volume: {exc}")
    dst.unlink()


@MACOS_ONLY
def test_plain_posix_rename_would_clobber_the_destination(tmp_path):
    """The premise for not using `os.rename`, measured on a real macOS host.

    No monkeypatching: a real `rename(2)` on a real existing file. If Darwin
    ever stopped replacing, this fails loudly rather than the product keeping an
    avoidance it no longer needs.
    """
    target = tmp_path / "talk.txt"
    target.write_bytes(b"original")
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"staged")

    os.rename(staged, target)

    assert target.read_bytes() == b"staged", "POSIX rename did not replace the destination"
    assert render_mod._RENAME_REFUSES_EXISTING is False
    assert render_mod._IS_MACOS is True


@MACOS_ONLY
def test_the_real_call_publishes_and_refuses(tmp_path):
    """The call against a real Darwin kernel and volume, both outcomes."""
    native = _native()
    _require_real_exclusive_rename(native, tmp_path)
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"complete staged bytes")
    target = tmp_path / "talk.txt"

    native.rename_excl(staged, target)

    assert target.read_bytes() == b"complete staged bytes"
    assert not staged.exists()
    assert _entries(tmp_path) == ["talk.txt"]

    staged.write_bytes(b"staged again")
    with pytest.raises(FileExistsError):
        native.rename_excl(staged, target)

    assert target.read_bytes() == b"complete staged bytes"
    assert staged.read_bytes() == b"staged again", "the staged file did not survive"
    assert _entries(tmp_path) == ["staged.tmp", "talk.txt"]


@MACOS_ONLY
def test_atomic_write_bytes_publishes_and_refuses_through_the_real_call(tmp_path, monkeypatch):
    """The whole path on a real macOS host, with the link failure simulated."""
    native = _native()
    _require_real_exclusive_rename(native, tmp_path)
    target = tmp_path / "talk.txt"
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]

    (tmp_path / "talk.txt").write_bytes(b"someone else's bytes")
    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]
