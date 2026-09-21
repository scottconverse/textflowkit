"""Output path confinement (SEC-M2)."""

from __future__ import annotations

import os

import pytest

from textflowkit.core.paths import (
    ENV_OUTPUT_ROOT,
    UnsafeOutputPathError,
    ensure_output_dir,
    output_root,
    resolve_output_dir,
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
