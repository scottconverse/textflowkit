"""The release tag names core; the fonts package is versioned on its own contract.

The build job refuses to build anything until the release's two versions are
consistent (issue #15, D1). The tag must be a normalized final `v` version and
the core package must declare exactly that version, because the tag names the
core release. The fonts companion package is published on its own contract: it
may be older than the core release when the release reuses an earlier fonts
publication, and what has to hold instead is that the declared fonts version is
one core can actually install - it must satisfy the `export` extra's
requirement in the core `pyproject.toml`.

These tests are deterministic: they build throwaway package roots in the system
temporary directory and never touch PyPI, GitHub, or a git repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import verify_release_versions

NEEDS_TOMLLIB = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11"
)
CORE_VERSION = "0.1.6"
TAG = f"v{CORE_VERSION}"
FONTS_REQUIREMENT = "textflowkit-fonts>=0.1.5,<0.2"
DEFAULT_EXPORT = ["python-docx>=1.1", "reportlab>=4.0", FONTS_REQUIREMENT]
FONTS_PYPROJECT = "packages/textflowkit-fonts/pyproject.toml"


def _core_pyproject(version: str, export: list[str]) -> str:
    requirements = ", ".join(f'"{requirement}"' for requirement in export)
    return (
        "[project]\n"
        'name = "textflowkit"\n'
        f'version = "{version}"\n'
        "\n"
        "[project.optional-dependencies]\n"
        f"export = [{requirements}]\n"
    )


def _fonts_pyproject(version: str) -> str:
    return '[project]\nname = "textflowkit-fonts"\n' f'version = "{version}"\n'


def _root(
    tmp_path: Path,
    *,
    core: str = CORE_VERSION,
    fonts: str = "0.1.5",
    export: list[str] | None = None,
    fonts_pyproject: str | None = None,
) -> Path:
    root = tmp_path / "checkout"
    (root / "packages/textflowkit-fonts").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        _core_pyproject(core, DEFAULT_EXPORT if export is None else export), encoding="utf-8"
    )
    if fonts_pyproject is None:
        fonts_pyproject = _fonts_pyproject(fonts)
    (root / FONTS_PYPROJECT).write_text(fonts_pyproject, encoding="utf-8")
    return root


def _check(tmp_path: Path, capsys, *, tag: str = TAG, **overrides) -> tuple[int, str, str]:
    root = _root(tmp_path, **overrides)
    code = verify_release_versions.main(["--root", str(root), "--tag", tag])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- accepted releases --------------------------------------------------------


@NEEDS_TOMLLIB
def test_a_release_that_publishes_both_packages_at_the_tag_is_accepted(tmp_path, capsys) -> None:
    code, out, err = _check(tmp_path, capsys, fonts=CORE_VERSION)

    assert code == 0, err
    assert "0.1.6" in out
    assert err == ""


@NEEDS_TOMLLIB
def test_a_core_release_may_reuse_an_older_fonts_version(tmp_path, capsys) -> None:
    """issue #15: core 0.1.6 published with the fonts package still at 0.1.5.

    The fonts version is not the tag, and that is the point of the unit: what
    matters is that the declared fonts version is one the `export` extra can
    install.
    """
    code, out, err = _check(tmp_path, capsys, core=CORE_VERSION, fonts="0.1.5")

    assert code == 0, err
    assert "0.1.5" in out and "0.1.6" in out


@NEEDS_TOMLLIB
def test_a_fonts_version_newer_than_the_tag_is_accepted_when_core_can_install_it(
    tmp_path, capsys
) -> None:
    """The two contracts are independent in both directions.

    A core release may ship a fonts version that no earlier release published,
    as long as the `export` range covers it; that is a new fonts publication,
    which the workflow builds and uploads.
    """
    code, _out, err = _check(tmp_path, capsys, core="0.1.5", tag="v0.1.5", fonts="0.1.6")

    assert code == 0, err


@NEEDS_TOMLLIB
def test_the_release_tag_is_read_from_the_environment_by_default(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("RELEASE_TAG", TAG)
    root = _root(tmp_path, fonts=CORE_VERSION)

    code = verify_release_versions.main(["--root", str(root)])

    assert code == 0, capsys.readouterr().err


# --- refused releases ---------------------------------------------------------


@NEEDS_TOMLLIB
def test_a_core_version_that_is_not_the_tag_is_refused(tmp_path, capsys) -> None:
    code, out, err = _check(tmp_path, capsys, core="0.1.5", tag="v0.1.6")

    assert code == 1
    assert out == ""
    assert "0.1.5" in err and "0.1.6" in err


@NEEDS_TOMLLIB
def test_a_fonts_version_outside_the_core_export_range_is_refused(tmp_path, capsys) -> None:
    """A release set pip could not resolve must not be published."""
    code, _out, err = _check(tmp_path, capsys, fonts="0.2.0")

    assert code == 1
    assert "0.2.0" in err
    assert FONTS_REQUIREMENT in err


@NEEDS_TOMLLIB
def test_a_fonts_version_below_the_export_floor_is_refused(tmp_path, capsys) -> None:
    code, _out, err = _check(tmp_path, capsys, fonts="0.1.4")

    assert code == 1
    assert "0.1.4" in err


@NEEDS_TOMLLIB
def test_an_export_extra_without_the_fonts_requirement_is_refused(tmp_path, capsys) -> None:
    """Without the requirement there is nothing the fonts version can be checked against."""
    code, _out, err = _check(
        tmp_path, capsys, export=["python-docx>=1.1", "reportlab>=4.0"]
    )

    assert code == 1
    assert "textflowkit-fonts" in err


@NEEDS_TOMLLIB
def test_a_missing_fonts_pyproject_is_refused(tmp_path, capsys) -> None:
    root = _root(tmp_path)
    (root / FONTS_PYPROJECT).unlink()

    code = verify_release_versions.main(["--root", str(root), "--tag", TAG])

    assert code == 1
    assert capsys.readouterr().err != ""


@NEEDS_TOMLLIB
def test_a_malformed_fonts_pyproject_is_refused(tmp_path, capsys) -> None:
    code, _out, err = _check(tmp_path, capsys, fonts_pyproject="this is not TOML\n")

    assert code == 1
    assert "pyproject.toml" in err


@pytest.mark.parametrize("tag", ["0.1.6", "v0.1.6.0.0", "v0.1.06", "vv0.1.6", ""])
def test_a_tag_that_is_not_a_normalized_v_version_is_refused(tmp_path, capsys, tag) -> None:
    code, out, err = _check(tmp_path, capsys, tag=tag)

    assert code == 1
    assert out == ""
    assert err != ""


@pytest.mark.parametrize("tag", ["v0.1.6rc1", "v0.1.6.dev1", "v0.1.6a1"])
def test_a_prerelease_tag_is_refused(tmp_path, capsys, tag) -> None:
    """PyPI releases here are final versions; a pre-release tag is not a release."""
    code, _out, err = _check(tmp_path, capsys, tag=tag)

    assert code == 1
    assert "final" in err


@NEEDS_TOMLLIB
def test_no_tag_at_all_is_refused(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    root = _root(tmp_path)

    code = verify_release_versions.main(["--root", str(root)])

    assert code == 1
    assert capsys.readouterr().err != ""


@NEEDS_TOMLLIB
def test_a_core_pyproject_without_a_version_is_refused(tmp_path, capsys) -> None:
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "textflowkit"\n', encoding="utf-8")

    code = verify_release_versions.main(["--root", str(root), "--tag", TAG])

    assert code == 1
    assert capsys.readouterr().err != ""
