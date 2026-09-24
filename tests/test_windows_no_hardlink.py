"""Publishing staged output where the filesystem has no hard links (U31; review A4).

`atomic_write_bytes` commits a fully written and fsynced staging file with
`os.link`: that is atomic, and it refuses an existing destination. Some
filesystems - FAT32 and exFAT (most USB drives and SD cards), and some network
shares - have no hard links at all, so `os.link` raises there and an export to
`--output-dir E:\\` fails after the whole transcription has already run.

Windows has a second no-replace primitive: `os.rename` refuses an existing
destination with `FileExistsError` (WinError 183, measured on this host) and
moves within one volume atomically. The staging file is created in the
destination's own directory, so the move cannot leave that volume. The outside
review suggested instead an exclusive-create write (`open(path, "xb")`) or an
existence check followed by `os.replace`; both are rejected here, because the
first exposes a partially written file under the destination name and the
second races whoever creates the file between the check and the replace.

POSIX `os.rename` overwrites, so it is not the same primitive and is not used
there: off Windows a link failure stays fail-closed, which the last two tests
pin by turning the module's own platform flag off.

A real FAT or exFAT volume was not available to this unit, so the fallback is
proved against a simulated link failure, not a real drive. That is a stated
limit of the evidence, not a claim of support; `reports/U31-windows-no-hardlink.md`
records it.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import textflowkit.render as render_mod
from tests.test_model import sample
from textflowkit.render import atomic_write_bytes, render_bytes, write_all

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="the no-replace rename fallback is Windows-only; POSIX os.rename overwrites",
)

# How a filesystem without hard links fails `CreateHardLinkW`: either the API
# rejects the request as invalid (WinError 1 "Incorrect function" and WinError
# 50 "not supported" both map to errno 22 on Python 3.13 / Windows, measured),
# or the facility is reported unsupported. Both shapes are exercised so the
# fallback cannot pass by recognizing one errno: on Windows `errno.EOPNOTSUPP`
# is 10045 and `errno.ENOTSUP` is 129, so an errno allowlist would be wrong
# twice over.
LINK_FAILURES = [
    pytest.param(errno.EINVAL, "Incorrect function.", id="winerror-1-einval"),
    pytest.param(errno.ENOTSUP, "no hard links", id="enotsup"),
]


def _entries(directory: Path) -> list[str]:
    """Everything left in `directory`; a staging file here is a leak."""
    return sorted(p.name for p in directory.iterdir())


def _plant_a_file(path: Path, data: bytes):
    """A stand-in for a competing writer landing on `path` right now."""

    def link(src, dst, **kwargs):
        Path(dst).write_bytes(data)
        raise OSError(errno.ENOTSUP, "no hard links")

    return link


def _fail_link(monkeypatch, error: OSError) -> None:
    """Make every `os.link` fail the way a filesystem without hard links does."""

    def link(src, dst, **kwargs):
        raise error

    monkeypatch.setattr(os, "link", link)


def _spy_publication(monkeypatch) -> list[str]:
    """Record which publication primitives are called, in order."""
    calls: list[str] = []
    real_link, real_rename, real_replace = os.link, os.rename, os.replace

    def link(src, dst, **kwargs):
        calls.append("link")
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


@WINDOWS_ONLY
def test_the_platform_premise_rename_refuses_an_existing_destination(tmp_path):
    """The OS property the fallback rests on, measured rather than assumed.

    No monkeypatching here: this is a real `os.rename` against a real existing
    file, which is the thing that decides whether the fallback may be used at
    all. If a future Python or filesystem made rename replace silently, this
    fails loudly instead of the product quietly clobbering a destination. The
    module's own platform flag is asserted against the same measurement.
    """
    target = tmp_path / "talk.txt"
    target.write_bytes(b"original")
    staged = tmp_path / "staged.tmp"
    staged.write_bytes(b"staged")

    with pytest.raises(FileExistsError):
        os.rename(staged, target)

    assert target.read_bytes() == b"original", "os.rename overwrote an existing destination"
    assert staged.read_bytes() == b"staged", "the staged file did not survive"
    assert render_mod._RENAME_REFUSES_EXISTING is True


@WINDOWS_ONLY
@pytest.mark.parametrize("code,message", LINK_FAILURES)
def test_staged_bytes_publish_when_hard_links_are_unavailable(tmp_path, monkeypatch, code, message):
    """The defect: a destination with no existing file must still be written."""
    target = tmp_path / "talk.txt"
    _fail_link(monkeypatch, OSError(code, message))

    atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"], "the staging file was left behind"


@WINDOWS_ONLY
@pytest.mark.parametrize("code,message", LINK_FAILURES)
def test_an_existing_file_is_refused_rather_than_overwritten(tmp_path, monkeypatch, code, message):
    """The fallback must carry the no-clobber rule, not replace it."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    _fail_link(monkeypatch, OSError(code, message))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]


@WINDOWS_ONLY
def test_a_file_created_during_the_fallback_is_never_overwritten(tmp_path, monkeypatch):
    """The race the review's suggested fixes would lose.

    The destination is verified absent before the staging file is written; this
    competitor appears *after* that check, inside the failed link, so it lands
    exactly in the window an existence check plus `os.replace` would race. The
    rename must refuse it, and the `FileExistsError` can only come from the
    rename: the patched link raises `ENOTSUP`.
    """
    target = tmp_path / "talk.txt"
    monkeypatch.setattr(os, "link", _plant_a_file(target, b"planted by another writer"))

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"hello")

    assert target.read_bytes() == b"planted by another writer"
    assert _entries(tmp_path) == ["talk.txt"]


@WINDOWS_ONLY
def test_same_job_resume_rules_hold_under_the_fallback(tmp_path, monkeypatch):
    """Adopt only a byte-identical file of this job; refuse a differing one."""
    target = tmp_path / "talk.txt"
    atomic_write_bytes(target, b"one")
    _fail_link(monkeypatch, OSError(errno.ENOTSUP, "no hard links"))

    atomic_write_bytes(target, b"one", reuse_identical=True)
    assert target.read_bytes() == b"one"

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"two", reuse_identical=True)

    assert target.read_bytes() == b"one", "a differing file was adopted or clobbered"
    assert _entries(tmp_path) == ["talk.txt"]


@WINDOWS_ONLY
def test_write_all_publishes_every_format_without_hard_links(tmp_path, monkeypatch):
    """The reviewer's done-when, over the real pipeline write path."""
    _fail_link(monkeypatch, OSError(errno.EINVAL, "Incorrect function."))

    transcript = sample()
    paths = write_all(transcript, formats=["txt", "srt"], output_dir=tmp_path, stem="talk")

    assert [p.name for p in paths] == ["talk.txt", "talk.srt"]
    for path in paths:
        fmt = path.suffix.lstrip(".")
        assert path.read_bytes() == render_bytes(transcript, fmt)
    assert _entries(tmp_path) == ["talk.srt", "talk.txt"]


@WINDOWS_ONLY
def test_write_all_still_refuses_an_existing_file_without_hard_links(tmp_path, monkeypatch):
    """A collision under the fallback fails the export and publishes nothing else."""
    existing = tmp_path / "talk.txt"
    existing.write_bytes(b"someone else's bytes")
    _fail_link(monkeypatch, OSError(errno.ENOTSUP, "no hard links"))

    with pytest.raises(FileExistsError):
        write_all(sample(), formats=["txt"], output_dir=tmp_path, stem="talk")

    assert existing.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]


def test_a_working_link_is_never_routed_through_the_fallback(tmp_path, monkeypatch):
    """The normal path is unchanged: one hard link, no rename."""
    target = tmp_path / "talk.txt"
    calls = _spy_publication(monkeypatch)

    atomic_write_bytes(target, b"hello")

    assert calls == ["link"]
    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


def test_replace_true_still_replaces_and_never_renames(tmp_path, monkeypatch):
    """`replace=True` is the caller's explicit choice and keeps `os.replace`."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"stale")
    calls = _spy_publication(monkeypatch)

    atomic_write_bytes(target, b"hello", replace=True)

    assert calls == ["replace"]
    assert target.read_bytes() == b"hello"
    assert _entries(tmp_path) == ["talk.txt"]


def test_a_collision_from_the_link_is_never_retried_as_a_rename(tmp_path, monkeypatch):
    """`FileExistsError` is the no-clobber verdict, not a capability gap."""
    target = tmp_path / "talk.txt"
    target.write_bytes(b"first")
    calls = _spy_publication(monkeypatch)

    def link(src, dst, **kwargs):
        calls.append("link")
        raise FileExistsError(errno.EEXIST, "exists", dst)

    monkeypatch.setattr(os, "link", link)
    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"second")

    assert calls == ["link"], "a collision was retried as a rename"
    assert target.read_bytes() == b"first"


def test_a_link_failure_stays_fail_closed_where_rename_would_overwrite(tmp_path, monkeypatch):
    """The non-Windows branch: the original error propagates, nothing is published.

    The flag is the module's own statement that `os.rename` refuses an existing
    destination - true on Windows, false on POSIX, where rename clobbers. It is
    turned off here so the fail-closed branch runs on the Windows host that
    cannot otherwise reach it; the export must not degrade to a direct write.
    """
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False, raising=False)
    target = tmp_path / "talk.txt"
    error = OSError(errno.EINVAL, "Incorrect function.")
    _fail_link(monkeypatch, error)

    with pytest.raises(OSError) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert excinfo.value is error, "the link failure must propagate untouched"
    assert not target.exists()
    assert _entries(tmp_path) == [], "the staging file was left behind"


def test_fail_closed_platform_does_not_clobber_a_competing_file(tmp_path, monkeypatch):
    """And it does not touch a file that is already there, either."""
    monkeypatch.setattr(render_mod, "_RENAME_REFUSES_EXISTING", False, raising=False)
    target = tmp_path / "talk.txt"
    target.write_bytes(b"someone else's bytes")
    error = OSError(errno.ENOTSUP, "no hard links")
    _fail_link(monkeypatch, error)

    with pytest.raises(OSError) as excinfo:
        atomic_write_bytes(target, b"hello")

    assert excinfo.value is error
    assert target.read_bytes() == b"someone else's bytes"
    assert _entries(tmp_path) == ["talk.txt"]
