"""Exported file permissions (U14; outside review A13, issue #14).

`tempfile` creates its staging file 0600, and publication links that inode into
place, so a published transcript kept the staging mode. On POSIX that means
another account - the web server user the export was written for, say - cannot
read the subtitles or documents the tool was asked to produce, while a file
created any other way in the same directory would be readable.

The published file should instead carry the mode an ordinary newly created file
gets under the active process umask. The umask cannot be read without setting
it, and setting it is process-global (unsafe under threads), so the
implementation measures it by creating a throwaway file with the default 0o666
request and keeping whatever the kernel grants.

These tests assert the equality that defines the requirement - the published
file's mode equals the mode of a plainly created control file in the same
directory - rather than a hardcoded literal, so they hold under whatever umask
the test process happens to have. The literal values are asserted in
`test_exported_mode_follows_the_active_umask`, which sets the umask itself and
is POSIX-only.

Windows reports the same synthetic mode (0o666) for every file it creates, so
it cannot show the umask arithmetic at all; it can still show the wiring - that
the staging file is made ordinary *before* the destination becomes visible -
which is what the first test does by running the real POSIX branch there.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import textflowkit.render as render_mod
from textflowkit.render import atomic_write_bytes

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="the umask is a POSIX concept; Windows has no mode arithmetic"
)


def _mode_of(path: Path) -> int:
    """The permission bits actually recorded for `path`."""
    return stat.S_IMODE(path.stat().st_mode)


def _control_mode(directory: Path) -> int:
    """The mode a plainly created file gets in `directory` right now.

    This is the specification, measured rather than assumed: an ordinary file
    creation (no explicit mode, no temporary-file machinery) in the same
    directory, under the same umask, at the same moment.
    """
    control = directory / "control.txt"
    control.write_bytes(b"")
    return _mode_of(control)


def _run_posix_branch(monkeypatch) -> None:
    """Make the permission path run on any host.

    Normalization is a no-op on Windows, where every created file reports the
    same synthetic mode, so a Windows run of this suite would otherwise never
    execute it. Flipping the module's own platform flag runs the real code: the
    probe really creates and stats a file, and the real `os.chmod` is called.
    Only the *answer* is degenerate on Windows, which is why the assertion
    compares against a control file rather than a literal. `os.name` is left
    alone on purpose - pathlib derives its flavour from it, so changing it
    mid-process breaks unrelated path handling.

    `raising=False` because the flag is part of the fix: against the pre-fix
    module the test must fail on the missing adjustment, not on the missing
    name. If the flag were renamed the implementation would find nothing to
    measure and the mode assertion would still fail, so this hides nothing.
    """
    monkeypatch.setattr(render_mod, "_HAS_UMASK", True, raising=False)


def test_staging_file_is_made_ordinary_before_the_destination_appears(tmp_path, monkeypatch):
    """The fully written staging file gets its mode before anything is published.

    This is the regression for the defect itself: before the fix the staging
    file was published as-is, so `os.chmod` was never called at all. The spy
    also records whether the destination existed at that instant, which pins
    the *order* the brief requires - a mode applied after publication would
    leave a window where the exported file is readable by nobody else, and the
    atomicity contract would have been rewritten rather than extended.
    """
    _run_posix_branch(monkeypatch)
    target = tmp_path / "talk.txt"
    expected = _control_mode(tmp_path)

    seen: list[tuple[Path, int, bool]] = []
    real_chmod = os.chmod

    def spy(path, mode, **kwargs):
        seen.append((Path(path), stat.S_IMODE(mode), target.exists()))
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", spy)
    atomic_write_bytes(target, b"hello")

    assert len(seen) == 1, "the staging file was never given an ordinary mode"
    staged, mode, published_yet = seen[0]
    assert staged.parent == tmp_path
    assert staged.name.endswith(".tmp"), staged.name
    assert mode == expected, f"got {oct(mode)}, an ordinary file here gets {oct(expected)}"
    assert published_yet is False, "the destination was visible before the mode was set"

    assert target.read_bytes() == b"hello"
    # Neither the staging file nor the mode probe may survive the call.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["control.txt", "talk.txt"]


def test_no_clobber_still_refuses_and_leaves_nothing_behind(tmp_path, monkeypatch):
    """The new mode probe must not leak, and must not soften the no-clobber rule."""
    _run_posix_branch(monkeypatch)
    target = tmp_path / "talk.txt"
    atomic_write_bytes(target, b"first")

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"second")

    assert target.read_bytes() == b"first"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["talk.txt"]


@POSIX_ONLY
@pytest.mark.parametrize("replace", [False, True], ids=["link", "replace"])
@pytest.mark.parametrize(
    "umask,expected",
    [(0o022, 0o644), (0o027, 0o640), (0o077, 0o600)],
    ids=["umask022", "umask027", "umask077"],
)
def test_exported_mode_follows_the_active_umask(tmp_path, umask, expected, replace):
    """Live POSIX check: the published mode is 0o666 & ~umask, not the staging 0600.

    Both publication paths are covered, because `os.replace` takes the mode from
    the new inode exactly as `os.link` does - the mode has to be on the staging
    file before either one runs.

    The three umasks run in one process on purpose: an implementation that
    cached the answer (or, worse, set the umask once) would pass any single case
    and fail these together. Under `umask022` and `umask027` the pre-fix
    behaviour (0o600) is strictly narrower than expected, so those two cases are
    red before the fix.

    CI runs this on the Linux and macOS jobs. The developer host for this unit
    is Windows, where it is skipped, so it is not live evidence from that host.
    """
    target = tmp_path / "talk.txt"
    if replace:
        target.write_bytes(b"stale")  # an existing file the caller chose to replace

    previous = os.umask(umask)
    try:
        expected_from_a_control_file = _control_mode(tmp_path)
        atomic_write_bytes(target, b"hello", replace=replace)
    finally:
        os.umask(previous)

    assert expected_from_a_control_file == expected, "the test's own premise"
    assert _mode_of(target) == expected
    assert target.read_bytes() == b"hello"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["control.txt", "talk.txt"]
