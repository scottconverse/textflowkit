"""Publishing staged output on Linux where the filesystem has no hard links (U32).

The Linux half of outside-review A4. `atomic_write_bytes` commits a fully
written and fsynced staging file with `os.link`; some filesystems - FAT32 and
exFAT (most USB drives and SD cards) and some network shares - have no hard
links, and `link(2)` reports that as `EPERM`, so the export used to fail after
the whole transcription had run. Linux's second no-replace primitive is the
kernel's `renameat2(..., RENAME_NOREPLACE)`, which moves the staged inode
atomically and answers `EEXIST` instead of replacing an existing destination.
Plain POSIX `rename(2)` replaces silently and cannot stand in for it, and
neither can the review's suggestions: `open(dst, "xb")` exposes a partly written
file under the destination name, and a check followed by `os.replace` races
whoever creates the file in between.

**What this file does not prove.** It was written on Windows, so the tests that
reach the Linux branch set the module's platform flags and hand it a *double*
for the syscall: that proves the module's side of the boundary, not that a Linux
kernel behaves as the double does. The only real measurements are the
`LINUX_ONLY` tests, which run on a Linux host or skip with the reason.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import os
import sys
import threading
from pathlib import Path

import pytest

import textflowkit.render as render_mod
from tests.test_model import sample
from textflowkit.render import atomic_write_bytes, render_bytes, write_all

LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the renameat2 no-replace primitive is Linux-only, so there is nothing real to call here",
)

# `os.replace` before any spy replaced it: the kernel double commits through
# this, so a spy records the module's own calls only.
_REAL_REPLACE = os.replace

# `link(2)`'s documented answer for a filesystem without hard links is EPERM
# ("The filesystem containing oldpath and newpath does not support the creation
# of hard links"); EOPNOTSUPP is not in that list but a driver may report it,
# and the fallback triggers on any failure that is not a collision.
LINK_FAILURES = [
    pytest.param(errno.EPERM, "Operation not permitted", id="eperm-no-hard-links"),
    pytest.param(errno.EOPNOTSUPP, "Operation not supported", id="eopnotsupp"),
]

# The three distinct values meaning "this host will not do a no-replace
# rename": a filesystem that does not support the flag (EINVAL, per rename(2)),
# a kernel without the syscall (ENOSYS), and not-supported (EOPNOTSUPP, the
# same value as ENOTSUP on Linux). All must fail closed.
UNSUPPORTED_ANSWERS = [
    pytest.param(errno.EINVAL, id="einval-filesystem-refused-the-flag"),
    pytest.param(errno.ENOSYS, id="enosys-no-renameat2-in-the-kernel"),
    pytest.param(errno.EOPNOTSUPP, id="eopnotsupp"),
]


def _native():
    """The wrapper module under test, by name so the package cannot shadow it."""
    return importlib.import_module("textflowkit.render._rename_noreplace")


def _entries(directory: Path) -> list[str]:
    """Everything left in `directory`; a staging file here is a leak."""
    return sorted(p.name for p in directory.iterdir())


def _linux_host(monkeypatch) -> None:
    """Put the module in the configuration a Linux host has.

    Both flags, because on this Windows host `_RENAME_REFUSES_EXISTING` is True
    and the Windows rename branch would otherwise answer first and hide the
    Linux one; on Linux both assignments restate the truth.
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


class _KernelDouble:
    """A stand-in for the syscall, carrying the syscall's contract.

    Refuses an existing destination with `FileExistsError`, otherwise moves the
    staged inode, and records the staged bytes read at call time, so a partly
    written staging file cannot pass. A double, not a measurement: the
    `LINUX_ONLY` tests are where the real kernel is asked.
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


def _spy_publication(monkeypatch, link_error: OSError | None) -> list[str]:
    """Record the publication primitives the module calls, in order.

    `os.link` raises `link_error` when one is given, otherwise runs for real;
    `os.rename`/`os.replace` are recorded and then run for real, so a test that
    reaches them has already failed its assertion and reports the real outcome.
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


class _TheKernelAsALibcSymbol:
    """A libc-shaped symbol: takes the ctypes argument tuple, performs the move.

    The shape `rename_noreplace` sees from the real libc - five arguments and an
    int back - so a test that swaps it in has the wrapper's own path exercised
    rather than bypassed, and errno is set through the same `ctypes.set_errno`
    the wrapper reads.
    """

    def __init__(self) -> None:
        self.argtypes = None
        self.restype = None

    def __call__(self, _olddirfd, oldpath, _newdirfd, newpath, _flags) -> int:
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
    monkeypatch.setattr(native, "_load_renameat2", lambda: symbol)
    return symbol


@pytest.fixture
def kernel_errno(native, monkeypatch):
    """Let a test say what errno the kernel call left behind."""
    state: dict[str, int] = {}
    monkeypatch.setattr(native, "_get_errno", lambda: state["errno"])
    monkeypatch.setattr(native, "_set_errno", lambda value: state.__setitem__("cleared", value))
    return state


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


def test_the_kernel_is_handed_the_staged_file_and_no_clobbering_primitive(tmp_path, monkeypatch):
    """The syscall's arguments, and the primitives the module may use at all."""
    payload = b"complete staged bytes" * 100
    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, payload)

    assert calls == ["link"], "the Linux branch used a primitive that can replace a destination"
    assert len(kernel.calls) == 1
    staged, destination, staged_bytes = kernel.calls[0]
    assert destination == target
    assert staged != target
    assert staged.parent == tmp_path, "the staging file must be on the destination's volume"
    assert staged_bytes == payload, "the kernel was handed a partly written file"
    assert target.read_bytes() == payload
    assert _entries(tmp_path) == ["talk.txt"]


def test_an_existing_file_is_refused_rather_than_overwritten(tmp_path, monkeypatch):
    """The fallback must carry the no-clobber rule, not replace it."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    _linux_host(monkeypatch)
    _install_kernel(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]


def test_a_file_created_during_the_failed_link_is_never_overwritten(tmp_path, monkeypatch):
    """The race the review's fixes would lose.

    The competitor appears *after* the destination was verified absent, inside
    the failed link, which is exactly the window a check plus `os.replace`
    would race. The `FileExistsError` can only come from the native call: the
    patched link raises `EPERM`.
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


def test_write_all_publishes_every_format_and_refuses_a_collision(tmp_path, monkeypatch):
    """A4's done-when over the real pipeline write path, plus its refusal."""
    _linux_host(monkeypatch)
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


def test_a_working_link_never_reaches_the_native_call(tmp_path, monkeypatch):
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


def test_replace_true_still_replaces_and_never_reaches_the_native_call(tmp_path, monkeypatch):
    """`replace=True` is the caller's explicit choice and keeps `os.replace`."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"stale")
    _linux_host(monkeypatch)
    kernel = _install_kernel(monkeypatch)
    calls = _spy_publication(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    atomic_write_bytes(target, b"hello", replace=True)

    assert calls == ["replace"]
    assert kernel.calls == []
    assert target.read_bytes() == b"hello"


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
    """Anywhere else - macOS, say: the link error propagates untouched."""
    assert render_mod._IS_LINUX == sys.platform.startswith("linux")
    assert render_mod._RENAME_REFUSES_EXISTING == (os.name == "nt")
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


# --------------------------------------------------------------------------
# The native wrapper itself
# --------------------------------------------------------------------------


def test_the_call_passes_fdcwd_encoded_paths_and_the_noreplace_flag(native, kernel_symbol, tmp_path):
    """The arguments, and the ABI constants behind them.

    `AT_FDCWD` is `-100` (`linux/fcntl.h`) and `RENAME_NOREPLACE` is `1 << 0`
    (`linux/fs.h`); both are the same on every Linux architecture, unlike the
    syscall number, which is why the libc wrapper is used instead of `syscall()`.
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

    with pytest.raises(FileExistsError) as excinfo:
        native.rename_noreplace(tmp_path / "staged.tmp", tmp_path / "talk.txt")

    assert excinfo.value.errno == errno.EEXIST
    assert not isinstance(excinfo.value, native.NoReplaceRenameUnsupported)
    assert kernel_errno["cleared"] == 0, "errno was not cleared before the call"


@pytest.mark.parametrize("code", UNSUPPORTED_ANSWERS)
def test_an_unsupported_answer_fails_closed(native, kernel_symbol, kernel_errno, tmp_path, code):
    """No flag support, or no syscall: refuse, never degrade to a plain rename."""
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
    """Two ints, two char pointers, one unsigned flag, and an int back."""
    symbol = _RecordingSymbol()
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithTheSymbol(symbol))

    assert native._load_renameat2() is symbol
    assert symbol.argtypes == (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_uint)
    assert symbol.restype is ctypes.c_int


def test_the_symbol_is_resolved_for_each_call_and_never_cached(native, monkeypatch):
    """No process-wide cached answer, so none can be seen half-published.

    The first version of the wrapper cached under a "already probed" flag set
    before the symbol was published, which failed a second thread while the
    first was still resolving (see the concurrency test below). Resolving is
    cheap and this path is only reached after a link has already failed.
    """
    symbol = _RecordingSymbol()
    loads: list[tuple] = []

    def cdll(*args, **kwargs):
        loads.append(args)
        return _LibcWithTheSymbol(symbol)

    monkeypatch.setattr(native.ctypes, "CDLL", cdll)

    assert native._load_renameat2() is symbol
    assert native._load_renameat2() is symbol
    assert len(loads) == 2, "a resolve was answered from a cache"
    assert [name for name in ("_symbol", "_probed", "_probe_error") if hasattr(native, name)] == []


def test_a_libc_without_the_symbol_is_reported_as_unsupported(native, monkeypatch):
    """glibc only has the wrapper from 2.28 on; an older one must fail closed."""
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: _LibcWithoutTheSymbol())

    with pytest.raises(native.NoReplaceRenameUnsupported) as excinfo:
        native.rename_noreplace(Path("staged.tmp"), Path("talk.txt"))

    assert "renameat2" in str(excinfo.value)


def test_a_second_job_never_fails_while_another_resolves_the_symbol(tmp_path, monkeypatch):
    """The concurrency defect the coordinator reproduced, pinned deterministically.

    The fake holds the *first* resolve inside `ctypes.CDLL` and the second job
    starts exactly then, so a one-shot cache makes the second thread read
    "already probed" before the symbol exists and raise
    `NoReplaceRenameUnsupported` on a host whose libc has it - reproduced out of
    pytest as `{'second': NoReplaceRenameUnsupported, 'first': Sym}`. Against
    such a cache this fails either way, which is the point: on a fresh module the
    second job raises, and on one whose cache an earlier test already filled the
    first job never reaches a resolve, which the assertion below names.
    """
    native = _native()
    first_inside = threading.Event()
    release_first = threading.Event()
    resolves: list[str] = []
    outcomes: dict[str, OSError | None] = {}

    def cdll(*args, **kwargs):
        resolves.append(threading.current_thread().name)
        if len(resolves) == 1:
            first_inside.set()
            assert release_first.wait(timeout=10), "the test never released the first resolve"
        return _LibcWithTheSymbol(_TheKernelAsALibcSymbol())

    monkeypatch.setattr(native.ctypes, "CDLL", cdll)
    _linux_host(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    def publish(name: str) -> None:
        try:
            atomic_write_bytes(tmp_path / name, b"payload for " + name.encode())
        # OSError is the contract this code fails through (the wrapper's own
        # NoReplaceRenameUnsupported included). Anything else is a bug the test
        # should not dress up as a job failure: it stays uncaught, and the dict
        # comparison below then fails for the missing entry.
        except OSError as exc:
            outcomes[name] = exc
        else:
            outcomes[name] = None

    first = threading.Thread(target=publish, args=("first.txt",), name="first")
    first.start()
    assert first_inside.wait(timeout=10), (
        "the first job never reached the libc resolve - a symbol cached earlier in this process "
        "would answer before any resolve runs"
    )
    second = threading.Thread(target=publish, args=("second.txt",), name="second")
    second.start()
    second.join(timeout=10)
    release_first.set()
    first.join(timeout=10)
    assert not first.is_alive() and not second.is_alive(), "a job never finished"

    assert outcomes == {"first.txt": None, "second.txt": None}, (
        f"a job failed while another was resolving the libc symbol: {outcomes}"
    )
    assert sorted(resolves) == ["first", "second"], "a job skipped the resolve"
    for name in outcomes:
        assert (tmp_path / name).read_bytes() == b"payload for " + name.encode()
    assert _entries(tmp_path) == ["first.txt", "second.txt"]


def test_the_real_branch_fails_closed_on_a_host_without_the_symbol(tmp_path, monkeypatch):
    """The real wrapper, in the real branch, on a host whose libc has no symbol.

    Only the platform flag and the link failure are simulated: this host's own
    libc probe decides, so the fail-closed answer is measured rather than faked.
    """
    native = _native()
    try:
        native._load_renameat2()
    except native.NoReplaceRenameUnsupported:
        pass
    else:
        pytest.skip("this host has a real renameat2 symbol; see the LINUX_ONLY tests")

    target = tmp_path / "talk.txt"
    _linux_host(monkeypatch)
    _fail_link(monkeypatch, OSError(errno.EPERM, "Operation not permitted"))

    with pytest.raises(native.NoReplaceRenameUnsupported):
        atomic_write_bytes(target, b"hello")

    assert not target.exists(), "a host without the native primitive published anyway"
    assert _entries(tmp_path) == [], "the staging file was left behind"


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
    """The premise for not using `os.rename`, measured on a real Linux host.

    No monkeypatching: a real `rename(2)` on a real existing file. If the
    kernel ever stopped replacing, this fails loudly rather than the product
    keeping a primitive it no longer needs.
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
def test_the_real_syscall_publishes_and_refuses(tmp_path):
    """The syscall against a real kernel and filesystem, both outcomes."""
    native = _native()
    _require_real_syscall(native, tmp_path)
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"complete staged bytes")
    target = tmp_path / "talk.txt"

    native.rename_noreplace(staged, target)

    assert target.read_bytes() == b"complete staged bytes"
    assert not staged.exists()
    assert _entries(tmp_path) == ["talk.txt"]

    staged.write_bytes(b"staged again")
    with pytest.raises(FileExistsError):
        native.rename_noreplace(staged, target)

    assert target.read_bytes() == b"complete staged bytes"
    assert staged.read_bytes() == b"staged again", "the staged file did not survive"
    assert _entries(tmp_path) == ["staged.tmp", "talk.txt"]


@LINUX_ONLY
def test_atomic_write_bytes_publishes_and_refuses_through_the_real_syscall(tmp_path, monkeypatch):
    """The whole path on a real Linux host, with the link failure simulated."""
    native = _native()
    _require_real_syscall(native, tmp_path)
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
