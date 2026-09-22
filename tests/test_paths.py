"""Output path confinement (SEC-M2)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from textflowkit.core.paths import (
    ENV_INPUT_ROOT,
    ENV_OUTPUT_ROOT,
    UnsafeInputPathError,
    UnsafeOutputPathError,
    default_input_root,
    ensure_output_dir,
    output_root,
    resolve_input_path,
    resolve_output_dir,
    server_input_root,
)


@pytest.fixture()
def confined_root(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(root))
    return root


def test_relative_path_resolves_inside_root(confined_root):
    assert resolve_output_dir("out") == confined_root / "out"
    assert resolve_output_dir("nested/deep") == confined_root / "nested" / "deep"


def test_none_and_empty_resolve_to_root(confined_root):
    assert resolve_output_dir(None) == confined_root
    assert resolve_output_dir("") == confined_root


def test_absolute_path_inside_root_allowed(confined_root):
    target = confined_root / "sub"
    assert resolve_output_dir(str(target)) == target


def test_parent_traversal_is_blocked(confined_root):
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir(os.path.join(str(confined_root), "..", "traversal-probe"))


def test_relative_traversal_is_blocked(confined_root):
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir("../../../../tmp/evil")


def test_deep_traversal_through_existing_subdir_blocked(confined_root):
    (confined_root / "sub").mkdir()
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir(str(confined_root / "sub" / ".." / ".." / "escape"))


def test_absolute_path_outside_root_blocked(confined_root):
    outside = "C:/Windows/Temp" if os.name == "nt" else "/etc"
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir(outside)


def test_error_message_is_actionable(confined_root):
    with pytest.raises(UnsafeOutputPathError) as exc:
        resolve_output_dir("/etc")
    msg = str(exc.value)
    assert ENV_OUTPUT_ROOT in msg
    assert "outside the allowed root" in msg


def test_ensure_output_dir_creates_directory(confined_root):
    created = ensure_output_dir("made/here")
    assert created.is_dir()
    assert created == confined_root / "made" / "here"


def test_output_root_defaults_to_cwd_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_OUTPUT_ROOT, raising=False)
    monkeypatch.chdir(tmp_path)
    assert output_root() == tmp_path.resolve()


# --- input confinement (SEC: arbitrary local file read) --------------------

def test_input_unconfined_when_no_root(tmp_path):
    """The CLI user typed the path; they are the principal."""
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    assert resolve_input_path(str(f), root=None) == f.resolve()


def test_input_confined_inside_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    f = root / "a.mp4"
    f.write_bytes(b"x")
    assert resolve_input_path(str(f), root=root) == f.resolve()


def test_input_relative_path_resolves_inside_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x")
    assert resolve_input_path("a.mp4", root=root) == (root / "a.mp4").resolve()


def test_input_outside_root_is_blocked(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(UnsafeInputPathError):
        resolve_input_path(str(outside), root=root)


def test_input_traversal_is_blocked(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "sub").mkdir()
    with pytest.raises(UnsafeInputPathError):
        resolve_input_path(os.path.join(str(root), "sub", "..", "..", "escape.mp4"), root=root)


def test_input_missing_file_raises_filenotfound(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_input_path("nope.mp4", root=root)


def test_input_directory_is_not_a_file(tmp_path):
    root = tmp_path / "media"
    (root / "adir").mkdir(parents=True)
    with pytest.raises(ValueError):
        resolve_input_path(str(root / "adir"), root=root)


def test_input_error_message_is_actionable(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(UnsafeInputPathError) as exc:
        resolve_input_path(str(outside), root=root)
    assert ENV_INPUT_ROOT in str(exc.value)


def test_default_input_root_is_none_without_env(monkeypatch):
    monkeypatch.delenv(ENV_INPUT_ROOT, raising=False)
    assert default_input_root() is None


def test_default_input_root_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_INPUT_ROOT, str(tmp_path))
    assert default_input_root() == tmp_path.resolve()


def test_server_input_root_defaults_to_no_confinement(monkeypatch, tmp_path):
    """Adapters do NOT confine by default.

    An adapter runs as the person who started it and inherits their access. When
    that person is the operator, confining it to the working directory only
    refuses paths they are already entitled to use. Confinement is for a
    deployment whose callers are not the owner, and there it is set explicitly.
    """
    monkeypatch.delenv(ENV_INPUT_ROOT, raising=False)
    monkeypatch.chdir(tmp_path)
    assert server_input_root() is None


def test_output_only_confines_when_a_root_is_configured(monkeypatch, tmp_path):
    """Without TEXTFLOWKIT_OUTPUT_ROOT, an arbitrary writable path is allowed."""
    monkeypatch.delenv(ENV_OUTPUT_ROOT, raising=False)
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path.parent / "somewhere-else"
    elsewhere.mkdir(exist_ok=True)
    # No configured root means no boundary to violate.
    assert resolve_output_dir(str(elsewhere)) == elsewhere.resolve()


def test_output_still_confines_when_a_root_is_configured(monkeypatch, tmp_path):
    """An explicit root is enforced exactly as before."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(root))
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir(str(outside))


def test_server_input_root_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_INPUT_ROOT, str(tmp_path))
    assert server_input_root() == tmp_path.resolve()


# --- the pipeline applies it ----------------------------------------------

def test_pipeline_rejects_input_outside_root(tmp_path, monkeypatch):
    from textflowkit.core.pipeline import PipelineError, transcribe

    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")

    with pytest.raises(PipelineError) as exc:
        transcribe(str(outside), input_root=root)
    assert "outside the allowed input root" in str(exc.value)


def test_pipeline_allows_input_inside_root(tmp_path):
    """An in-root file gets past the confinement gate (it then fails on tools)."""
    from textflowkit.core.pipeline import PipelineError, transcribe

    root = tmp_path / "allowed"
    root.mkdir()
    inside = root / "in.mp4"
    inside.write_bytes(b"not really media")

    with pytest.raises(PipelineError) as exc:
        transcribe(str(inside), input_root=root)
    # it must fail for a media/tool reason, NOT a confinement reason
    assert "outside the allowed input root" not in str(exc.value)


def test_pipeline_ignores_input_root_for_urls(tmp_path):
    """URLs are guarded by the SSRF check, not the input-root check."""
    from textflowkit.core.pipeline import PipelineError, transcribe

    root = tmp_path / "allowed"
    root.mkdir()
    with pytest.raises(PipelineError) as exc:
        transcribe("http://169.254.169.254/x.mp4", input_root=root)
    assert "non-public IP" in str(exc.value)


# --- symlink escapes -------------------------------------------------------
# SECURITY.md states that symlinks which escape the root are rejected. The
# implementation gets this from Path.resolve() following links before the
# containment check, so the property is real but was untested: a refactor that
# dropped resolve() would silently reopen the hole with no red test.


def _make_link(link: Path, target: Path, *, directory: bool) -> None:
    """Create a link for the confinement tests, or skip if impossible.

    Windows symlinks need Developer Mode or elevation, but directory junctions
    do not - and `Path.resolve()` follows both. Using a junction here means the
    escape property is genuinely exercised on Windows rather than skipped.
    """
    import subprocess
    import sys

    if sys.platform == "win32" and directory:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and link.exists():
            return
        pytest.skip(f"cannot create a junction here: {proc.stderr.strip() or proc.stdout.strip()}")

    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"cannot create links here: {exc}")


def test_output_symlink_escape_is_blocked(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    _make_link(link, outside, directory=True)

    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(root))
    with pytest.raises(UnsafeOutputPathError):
        resolve_output_dir(str(link))


def test_output_symlink_staying_inside_is_allowed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    real = root / "real"
    root.mkdir()
    real.mkdir()
    link = root / "alias"
    _make_link(link, real, directory=True)

    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(root))
    # Resolves back inside the root, so it is permitted.
    assert resolve_output_dir(str(link)) == real.resolve()


def test_input_symlink_escape_is_blocked(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "secret.wav"
    target.write_bytes(b"x")
    link = root / "link.wav"
    if sys.platform == "win32":
        pytest.skip("file links need Developer Mode/elevation on Windows; junction case is covered above")
    _make_link(link, target, directory=False)

    with pytest.raises(UnsafeInputPathError):
        resolve_input_path(str(link), root=root)
