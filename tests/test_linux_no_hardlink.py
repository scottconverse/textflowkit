"""Publishing staged output on Linux where the filesystem has no hard links (U32).

The Linux half of outside-review A4. `atomic_write_bytes` commits a fully
written and fsynced staging file with `os.link`, which is atomic and refuses an
existing destination. Some filesystems have no hard links at all - FAT32 and
exFAT (most USB drives and SD cards) and some network shares - and `link(2)`
reports that as `EPERM` ("The filesystem containing oldpath and newpath does
not support the creation of hard links"), so the export used to fail after the
whole transcription had already run.

Linux has a kernel-enforced no-replace primitive for this case:
`renameat2(dirfd, oldpath, dirfd, newpath, RENAME_NOREPLACE)` moves the staged
inode atomically and answers `EEXIST` instead of replacing an existing
destination. Plain POSIX `rename(2)` cannot stand in for it - it silently
replaces - which the Linux-only premise test at the bottom measures on a real
Linux host rather than taking on faith. So can the review's other suggestions:
`open(dst, "xb")` exposes a partly written file under the destination name, and
an existence check followed by `os.replace` loses to whoever creates the file
between the two calls.

**What this file does not prove.** The host this unit was written on is Windows,
so the tests that reach the Linux branch set the module's platform flags to
Linux and hand it a *double* for the syscall. That proves the module's
behaviour on that branch; it is not evidence that a Linux kernel behaves the
way the double does. The only real measurements of the syscall are the
`LINUX_ONLY` tests, which run on a Linux host - or skip, with the reason, when
the libc symbol or the filesystem refuses.
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
from tests.test_model import sample
from textflowkit.render import atomic_write_bytes, render_bytes, write_all

LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the renameat2 no-replace primitive is Linux-only, so there is nothing real to call here",
)

# `os.replace` as it was before any test installed a spy. The double below
# commits through this, so a spy records the module's own calls and not the
# double's.
_REAL_REPLACE = os.replace

# How `link(2)` fails on a filesystem without hard links: EPERM is the
# documented errno ("The filesystem containing oldpath and newpath does not
# support the creation of hard links"). EOPNOTSUPP is not in that list but a
# filesystem driver may still report it, so both shapes are exercised - the
# fallback is triggered by any failure that is not a collision, and these pin
# that it does not need to recognize a particular errno.
LINK_FAILURES = [
    pytest.param(errno.EPERM, "Operation not permitted", id="eperm-no-hard-links"),
    pytest.param(errno.EOPNOTSUPP, "Operation not supported", id="eopnotsupp"),
]

# The kernel answers "this host will not do a no-replace rename" two ways, and
# a filesystem that does not support the flag (EINVAL, per rename(2)) is not the
# same situation as a kernel without the syscall (ENOSYS). Both must fail
# closed, never degrade to a plain rename; ENOTSUP/EOPNOTSUPP are the same
# value on Linux but are named separately because a driver may report either.
UNSUPPORTED_ANSWERS = [
    pytest.param(errno.EINVAL, id="einval-filesystem-refused-the-flag"),
    pytest.param(errno.ENOSYS, id="enosys-no-renameat2-in-the-kernel"),
    pytest.param(errno.EOPNOTSUPP, id="eopnotsupp"),
    pytest.param(errno.ENOTSUP, id="enotsup"),
]


def _native():
    """The Linux syscall wrapper module under test.

    Imported inside the callers rather than at module scope so that a red run -
    before the module exists - reports one failure per behaviour instead of a
    single collection error that hides which behaviours are unproven. By module
    name rather than by package attribute, so that the package's own binding of
    the callable cannot be mistaken for the module.
    """
    return importlib.import_module("textflowkit.render._rename_noreplace")


def _entries(directory: Path) -> list[str]:
    """Everything left in `directory`; a staging file here is a leak."""
    return sorted(p.name for p in directory.iterdir())


def _linux_host(monkeypatch) -> None:
    """Put the module in the configuration a Linux host has.

    `_IS_LINUX` selects the new primitive and `_RENAME_REFUSES_EXISTING` is
    False on POSIX. Both are set, because on this Windows host the second flag
    is True and the Windows rename branch would otherwise answer first, hiding
    the Linux branch entirely. On a Linux host both assignments restate what
    the module already computes.
    """
    monkeypatch.setattr(render_mod, "_IS_LINUX", True, raising=False)
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False)


def _fail_link(monkeypatch, error: OSError) -> None:
    """Make every `os.link` fail the way a filesystem without hard links does."""

    def link(src, dst, **kwargs):
        raise error

    monkeypatch.setattr(os, "link", link)


def _plant_a_file(path: Path, data: bytes):
    """A stand-in for a competing writer landing on `path` right now."""

    def link(src, dst, **kwargs):
        Path(dst).write_bytes(data)
        raise OSError(errno.EPERM, "Operation not permitted")

    return link


def _spy_publication(monkeypatch, link_error: OSError | None) -> list[str]:
    """Record which publication primitives the module calls, in order.

    `os.link` raises `link_error` when one is given; `os.rename` and
    `os.replace` are recorded and then run for real, because a test that
    reaches them has already failed its assertion and should say why with the
    real filesystem outcome rather than a stub's.
    """
    calls: list[str] = []
    real_link, real_rename, real_replace = os.link, os.rename, os.replace

    def link(src, dst, **kwargs):
        calls.append("link")
        if link_error is not None:
            raise link_error
        return real_link(src, dst, **kwargs)

    def rename(src, dst, **kwargs):
        calls.append("rename")
        return real_rename(src, dst, **kwargs)

    def replace(src, dst, **kwargs):
        calls.append("replace")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", replace)
    return calls


class _KernelDouble:
    """A stand-in for the syscall, carrying the syscall's contract.

    It refuses an existing destination with `FileExistsError` and otherwise
    moves the staged inode, recording what the module handed it - including the
    staged file's bytes, read at call time, so a partly written staging file
    cannot pass. It commits through the original `os.replace` captured before
    any spy installed itself, so a spy records only the module's own calls.

    It is a double, not a measurement: the Linux-only tests below are the only
    place the real kernel is asked.
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
    """Put the double in place of the real syscall, wherever it is called from."""
    kernel = _KernelDouble(fail_with)
    monkeypatch.setattr(_native(), "rename_noreplace", kernel)
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
        self.renameat2 = symbol


class _LibcWithoutTheSymbol:
    """An old libc: `CDLL(...)` succeeds, the symbol is simply not there."""


# --------------------------------------------------------------------------
# `atomic_write_bytes` on the Linux branch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code,message", LINK_FAILURES)
def test_staged_bytes_publish_when_hard_links_are_unavailable(tmp_path, monkeypatch, code, message):
    """The defect: a destination with no existing file must still be written."""
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(code, message))

    atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"], "the staging file was left behind"
    assert len(kernel.calls) == 1, "the native rename was not called exactly once"


def test_the_kernel_is_handed_the_fully_staged_file_and_the_destination(tmp_path, monkeypatch):
    """What the syscall receives: the fsynced staging file, and the right name."""
    payload = b"complete staged bytes" * 100
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, payload)

    assert len(kernel.calls) == 1
    staged, destination, staged_bytes = kernel.calls[0]
    assert destination == target
    assert staged != target
    assert staged.parent == tmp_path, "the staging file must be on the destination's volume"
    assert staged_bytes == payload, "the kernel was handed a partly written file"


@pytest.mark.parametrize("code,message", LINK_FAILURES)
def test_an_existing_file_is_refused_rather_than_overwritten(
    tmp_path, monkeypatch, code, message
):
    """The fallback must carry the no-clobber rule, not replace it."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(code, message))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]


def test_a_file_created_during_the_failed_link_is_never_overwritten(tmp_path, monkeypatch):
    """The race the review's suggested fixes would lose.

    The destination is verified absent before the staging file is written; this
    competitor appears *after* that check, inside the failed link, so it lands
    exactly in the window an existence check plus `os.replace` would race. The
    native call must refuse it, and the `FileExistsError` can only come from
    that call: the patched link raises `EPERM`.
    """
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    monkeypatch.setattr(os, "link", _plant_a_file(target, b"planted by another writer"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"planted by another writer"
    assert _entries(tmp_path) == ["talk.txt"]
    assert len(kernel.calls) == 1


def test_the_linux_branch_never_touches_a_primitive_that_can_clobber(tmp_path, monkeypatch):
    """No `os.rename` (POSIX rename replaces), no `os.replace`, no direct write.

    The spy raises `EPERM` from `os.link` and forwards `os.rename`/`os.replace`,
    so any use of either shows up in the recorded order.
    """
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"hello")

    assert calls == ["link"], "the Linux fallback used a primitive that can replace a destination"
    assert target.read_bytes() == b"hello"


def test_same_job_resume_rules_hold_under_the_linux_fallback(tmp_path, monkeypatch):
    """Adopt only a byte-identical file of this job; refuse a differing one."""
    target = tmp_path / "talk.txt"
    atomic_write_bytes(target, b"one")
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"one", reuse_identical=True)
    assert target.read_bytes() == b"one"

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"two", reuse_identical=True)

    assert target.read_bytes() == b"one", "a differing file was adopted or clobbered"
    assert _entries(tmp_path) == ["talk.txt"]


def test_an_unsupported_native_call_fails_closed(tmp_path, monkeypatch):
    """When the syscall says it cannot, nothing is published and the staging goes."""
    unsupported = _native().NoReplaceRenameUnsupported
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch, fail_with=unsupported(errno.EINVAL, "not supported here"))
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(unsupported) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert isinstance(excinfo.value, OSError), "the caller's fail-closed contract is an OSError"
    assert not target.exists()
    assert _entries(tmp_path) == [], "the staging file was left behind"


def test_write_all_publishes_every_format_without_hard_links(tmp_path, monkeypatch):
    """The reviewer's done-when, over the real pipeline write path."""
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    transcript = sample()
    paths = write_all(transcript, formats=["txt", "srt"], output_dir=tmp_path, stem="talk")

    assert [p.name for p in paths] == ["talk.txt", "talk.srt"]
    for path in paths:
        fmt = path.suffix.lstrip(".")
        assert path.read_bytes() == render_bytes(transcript, fmt)
    assert _entries(tmp_path) == ["talk.srt", "talk.txt"]
    assert len(kernel.calls) == 2


def test_write_all_still_refuses_an_existing_file_without_hard_links(tmp_path, monkeypatch):
    """A collision under the fallback fails the export and publishes nothing else."""
    existing = tmp_path / "talk.txt"
    existing.write_bytes(b"someone else's bytes")
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(FileExistsError):
        write_all(sample(), formats=["txt"], output_dir=tmp_path, stem="talk")

    assert existing.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]


# --------------------------------------------------------------------------
# Guards: the paths that must not change
# --------------------------------------------------------------------------


def test_a_working_link_is_never_routed_through_the_native_call(tmp_path, monkeypatch):
    """The normal path is unchanged: one hard link, no native rename."""
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, None)

    atomic_write_bytes(target, b"hello")

    assert calls == ["link"]
    assert kernel.calls == []
    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


def test_replace_true_still_replaces_and_never_calls_the_native_primitive(tmp_path, monkeypatch):
    """`replace=True` is the caller's explicit choice and keeps `os.replace`."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"stale")
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, None)

    atomic_write_bytes(target, b"hello", replace=True)

    assert calls == ["replace"]
    assert kernel.calls == []
    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


def test_a_collision_from_the_link_is_never_retried_as_the_native_call(tmp_path, monkeypatch):
    """`FileExistsError` is the no-clobber verdict, not a capability gap."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"first")
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(
        monkeypatch, FileExistsError(errno.EEXIST, "File exists", os.fspath(target))
    )

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"second")

    assert calls == ["link"], "a collision was retried as the native rename"
    assert kernel.calls == [], "a collision reached the native rename"
    assert target.read_bytes() == b"first"


def test_a_host_with_neither_no_replace_primitive_stays_fail_closed(tmp_path, monkeypatch):
    """The branch for anywhere else - macOS, say: the link error propagates untouched."""
    monkeypatch.setattr(render_mod, "_IS_LINUX", False, raising=False)
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False)
    kernel = _install_kernel(monkeypatch)
    target = tmp_path / "talk.txt"
    error = OSError(errno.EPERM, "Operation not permitted")
    _fail_link(monkeypatch, error)

    with pytest.raises(OSError) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert excinfo.value is error, "the link failure must propagate untouched"
    assert kernel.calls == [], "a host with no native primitive reached the Linux one"
    assert not target.exists()
    assert _entries(tmp_path) == [], "the staging file was left behind"


def test_the_platform_flags_describe_this_host():
    """The premise the dispatch rests on, asserted against the host's own name."""
    assert render_mod._IS_LINUX == sys.platform.startswith("linux")
    assert render_mod._RENAME_REFUSES_EXISTING == (os.name == "nt")


# --------------------------------------------------------------------------
# The native wrapper itself
# --------------------------------------------------------------------------


@pytest.fixture
def native(monkeypatch):
    """The wrapper module, with its one-shot libc probe reset for this test."""
    module = _native()
    monkeypatch.setattr(module, "_symbol", None)
    monkeypatch.setattr(module, "_probed", False)
    monkeypatch.setattr(module, "_probe_error", None)
    return module


@pytest.fixture
def kernel_symbol(native, monkeypatch):
    """Let a test drive the wrapper as though the kernel had answered."""
    symbol = _RecordingSymbol()
    monkeypatch.setattr(native, "_load_renameat2", lambda: symbol)
    return symbol


@pytest.fixture
def kernel_errno(native, monkeypatch):
    """Let a test say what errno the kernel call left behind."""
    state: dict[str, int] = {}
    monkeypatch.setattr(native, "_get_errno", lambda: state["errno"])
    monkeypatch.setattr(native, "_set_errno", lambda value: state.__setitem__("cleared", value))
    return state


def test_the_call_passes_fdcwd_encoded_paths_and_the_noreplace_flag(native, kernel_symbol, tmp_path):
    """The syscall arguments, and the two ABI constants they are built from.

    The numbers come from the kernel's own UAPI headers (`AT_FDCWD` -100 in
    `linux/fcntl.h`, `RENAME_NOREPLACE` `1 << 0` in `linux/fs.h`); they are the
    same on every Linux architecture, unlike the syscall number, which is why
    the libc wrapper is used instead of `syscall()`.
    """
    src = tmp_path / "staged.tmp"
    dst = tmp_path / "talk.txt"
    src.write_bytes(b"staged")

    native.rename_noreplace(src, dst)

    assert kernel_symbol.calls == [
        (native._AT_FDCWD, os.fsencode(src), native._AT_FDCWD, os.fsencode(dst),
         native._RENAME_NOREPLACE)
    ]
    assert native._AT_FDCWD == -100
    assert native._RENAME_NOREPLACE == 1
    assert isinstance(kernel_symbol.calls[0][1], bytes), "paths must be fsencoded, not str"


def test_a_collision_is_reported_as_the_no_clobber_verdict(
    native, kernel_symbol, kernel_errno, tmp_path
):
    """`EEXIST` is the caller's `FileExistsError`, and is not a capability gap."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = errno.EEXIST
    dst = tmp_path / "talk.txt"

    with pytest.raises(FileExistsError) as excinfo:
        native.rename_noreplace(tmp_path / "staged.tmp", dst)

    assert excinfo.value.errno == errno.EEXIST
    assert not isinstance(excinfo.value, native.NoReplaceRenameUnsupported)
    assert kernel_errno["cleared"] == 0, "errno was not cleared before the call"


@pytest.mark.parametrize("code", UNSUPPORTED_ANSWERS)
def test_an_unsupported_answer_fails_closed(native, kernel_symbol, kernel_errno, tmp_path, code):
    """No flag support, or no syscall: refuse, and never degrade to a plain rename."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = code

    with pytest.raises(native.NoReplaceRenameUnsupported) as excinfo:
        native.rename_noreplace(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert isinstance(excinfo.value, OSError), "the failure must still be an OSError"
    assert "renameat2" in str(excinfo.value), "the error must name the primitive it could not use"


def test_any_other_kernel_failure_propagates_as_a_plain_oserror(
    native, kernel_symbol, kernel_errno, tmp_path
):
    """A real failure (a permission problem, say) is neither a collision nor a gap."""
    kernel_symbol.result = -1
    kernel_errno["errno"] = errno.EPERM

    with pytest.raises(OSError) as excinfo:
        native.rename_noreplace(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert excinfo.value.errno == errno.EPERM
    assert not isinstance(excinfo.value, FileExistsError)
    assert not isinstance(excinfo.value, native.NoReplaceRenameUnsupported)


def test_the_loaded_symbol_is_given_the_declared_ctypes_signature(native, monkeypatch):
    """The libc symbol is typed: two ints, two char pointers, one unsigned flag."""
    symbol = _RecordingSymbol()
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithTheSymbol(symbol))

    assert native._load_renameat2() is symbol
    assert symbol.argtypes == (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_uint)
    assert symbol.restype is ctypes.c_int


def test_the_libc_probe_runs_once_and_keeps_its_answer(native, monkeypatch):
    """Resolved once per process: the libc symbol set does not change underfoot."""
    symbol = _RecordingSymbol()
    loads: list[tuple] = []

    def cdll(*args, **kwargs):
        loads.append(args)
        return _LibcWithTheSymbol(symbol)

    monkeypatch.setattr(native.ctypes, "CDLL", cdll)

    assert native._load_renameat2() is symbol
    assert native._load_renameat2() is symbol
    assert len(loads) == 1, "the libc probe ran more than once"


def test_a_libc_without_the_symbol_is_reported_as_unsupported(native, monkeypatch):
    """glibc only has the wrapper from 2.28 on; an older one must fail closed."""
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithoutTheSymbol())

    with pytest.raises(native.NoReplaceRenameUnsupported) as excinfo:
        native.rename_noreplace(Path("staged.tmp"), Path("talk.txt"))

    assert "renameat2" in str(excinfo.value)


def test_the_real_probe_reports_this_host_honestly(native):
    """Whatever this host has, the module must not claim more.

    On this Windows host `ctypes.CDLL(None)` is not even valid, which is the
    shape a POSIX-less host has to fail closed on as well.
    """
    try:
        symbol = native._load_renameat2()
    except native.NoReplaceRenameUnsupported as exc:
        assert "renameat2" in str(exc)
        return
    assert symbol.restype is ctypes.c_int


def test_the_real_linux_branch_fails_closed_on_a_host_without_the_symbol(native, tmp_path, monkeypatch):
    """The real wrapper, in the real branch, on a host whose libc has no symbol.

    Only the platform flag and the link failure are simulated: the call that
    decides is this host's own libc probe, so the fail-closed answer is
    measured here rather than faked. On a host that does have the symbol there
    is nothing to measure, and the skip says so.
    """
    try:
        native._load_renameat2()
    except native.NoReplaceRenameUnsupported:
        pass
    else:
        pytest.skip("this host has a real renameat2 symbol; see the Linux-only tests")

    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(native.NoReplaceRenameUnsupported):
        atomic_write_bytes(target, b"hello")

    assert not target.exists(), "a host without the native primitive published anyway"
    assert _entries(tmp_path) == [], "the staging file was left behind"


def test_a_host_without_the_symbol_publishes_nothing(native, tmp_path):
    """The fail-closed probe, measured on this host rather than faked.

    `_load_renameat2` is left real, so on a host with no symbol the probe finds
    none; on a Linux host it finds one and the skip states that this host has
    nothing to measure. Nothing is written to the destination either way.
    """
    target = tmp_path / "talk.txt"
    try:
        native._load_renameat2()
    except native.NoReplaceRenameUnsupported:
        pass
    else:
        pytest.skip("this host has a real renameat2 symbol; the refused host is faked above")

    with pytest.raises(native.NoReplaceRenameUnsupported):
        native.rename_noreplace(tmp_path / "staged.tmp", target)

    assert not target.exists()
    assert _entries(tmp_path) == []


# --------------------------------------------------------------------------
# The real syscall, on a Linux host. Skipped elsewhere, and skipped with its
# reason when the libc or the filesystem refuses - never by falling back.
# --------------------------------------------------------------------------


def _require_real_syscall(native, tmp_path) -> None:
    """Skip, naming the reason, when this host cannot run the real syscall.

    The probe uses `tmp_path`, so the capability asked about is the capability
    of the filesystem the assertions below run on.
    """
    src = tmp_path / "capability-probe-src"
    dst = tmp_path / "capability-probe-dst"
    src.write_bytes(b"probe")
    try:
        native.rename_noreplace(src, dst)
    except native.NoReplaceRenameUnsupported as exc:
        pytest.skip(f"no usable renameat2 on this host or filesystem: {exc}")
    dst.unlink()


@LINUX_ONLY
def test_plain_posix_rename_would_clobber_the_destination(tmp_path):
    """The premise for not using `os.rename`: measured, not assumed.

    No monkeypatching: this is a real `rename(2)` on a real existing file. If
    the kernel ever stopped replacing, this fails loudly, and the module flags
    below would have to change with it rather than the product quietly keeping
    a primitive it no longer needs.
    """
    target = tmp_path / "talk.txt"
    target.write_bytes(b"original")
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"staged")

    os.rename(staged, target)

    assert target.read_bytes() == b"staged", "POSIX rename did not replace the destination"
    assert render_mod._RENAME_REFUSES_EXISTING is False
    assert render_mod._IS_LINUX is True


@LINUX_ONLY
def test_the_real_syscall_publishes_a_staged_file(tmp_path):
    """The syscall against a real kernel and filesystem."""
    native = _native()
    _require_real_syscall(native, tmp_path)
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"complete staged bytes")
    target = tmp_path / "talk.txt"

    native.rename_noreplace(staged, target)

    assert target.read_bytes() == b"complete staged bytes"
    assert not staged.exists()
    assert _entries(tmp_path) == ["talk.txt"]


@LINUX_ONLY
def test_the_real_syscall_refuses_an_existing_destination(tmp_path):
    """No-clobber is the kernel's answer, not a userspace check."""
    native = _native()
    _require_real_syscall(native, tmp_path)
    target = tmp_path / "talk.txt"
    target.write_bytes(b"original")
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"staged")

    with pytest.raises(FileExistsError):
        native.rename_noreplace(staged, target)

    assert target.read_bytes() == b"original"
    assert staged.read_bytes() == b"staged", "the staged file did not survive"
    assert _entries(tmp_path) == ["staged.tmp", "talk.txt"]


@LINUX_ONLY
def test_atomic_write_bytes_publishes_through_the_real_syscall(tmp_path, monkeypatch):
    """The whole path on a real Linux host, with the link failure simulated."""
    native = _native()
    _require_real_syscall(native, tmp_path)
    target = tmp_path / "talk.txt"
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


@LINUX_ONLY
def test_atomic_write_bytes_refuses_a_collision_through_the_real_syscall(tmp_path, monkeypatch):
    """And the no-clobber rule, through the same real path."""
    native = _native()
    _require_real_syscall(native, tmp_path)
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]
