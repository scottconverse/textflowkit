"""The release must publish hashes of the exact four attached distributions.

`scripts/write_release_manifest.py` turns the distributions downloaded for the
GitHub release into a `SHA256SUMS` sidecar. The point of the file is that a
downloader can verify the wheel or sdist they got against the release; a
manifest that hashes a different build, names a fifth artifact, silently drops
one, or lists a filesystem path is worse than no manifest at all. These tests
use local fixture files only: no network, no PyPI, no GitHub.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.write_release_manifest import (
    ManifestError,
    main,
    manifest_lines,
    write_release_manifest,
)

VERSION = "0.1.5"


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _artifacts(root: Path, version: str = VERSION, fonts_version: str | None = None) -> list[Path]:
    """The four distributions the release workflow builds, in `dist/` layout."""
    fonts_version = version if fonts_version is None else fonts_version
    return [
        _write(root / "dist/main" / f"textflowkit-{version}-py3-none-any.whl", b"main wheel"),
        _write(root / "dist/main" / f"textflowkit-{version}.tar.gz", b"main sdist"),
        _write(root / "dist/fonts" / f"textflowkit_fonts-{fonts_version}-py3-none-any.whl",
               b"fonts wheel"),
        _write(root / "dist/fonts" / f"textflowkit_fonts-{fonts_version}.tar.gz", b"fonts sdist"),
    ]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_hashes_the_four_attached_distributions_sorted_by_name(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    expected = [f"{_sha(path)}  {path.name}" for path in sorted(paths, key=lambda p: p.name)]

    lines = manifest_lines(paths)

    assert lines == expected
    assert len(lines) == 4


def test_manifest_is_written_as_plain_sha256sum_rows_without_paths(tmp_path) -> None:
    """Only basenames, so the rows stay valid in any download directory."""
    paths = _artifacts(tmp_path)
    output = tmp_path / "SHA256SUMS"

    write_release_manifest(paths, output)

    raw = output.read_bytes()
    text = raw.decode("utf-8")
    assert text.endswith("\n") and text.count("\n") == 4
    # A CRLF or a path would make `sha256sum -c` fail or point outside the release.
    assert b"\r" not in raw
    assert "/" not in text and "\\" not in text
    assert str(tmp_path) not in text
    for path in paths:
        assert f"{_sha(path)}  {path.name}\n" in text
    assert sorted(line.split("  ")[1] for line in text.splitlines()) == sorted(
        path.name for path in paths
    )


def test_manifest_rows_are_stable_across_argument_order(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    shuffled = [paths[2], paths[0], paths[3], paths[1]]

    assert manifest_lines(shuffled) == manifest_lines(paths)


def test_a_missing_distribution_is_rejected_and_nothing_is_written(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    output = tmp_path / "SHA256SUMS"

    with pytest.raises(ManifestError, match="missing"):
        write_release_manifest(paths[:-1], output)

    assert not output.exists()


def test_a_missing_file_is_rejected_rather_than_hashed(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    paths[0].unlink()

    with pytest.raises(ManifestError, match="missing"):
        manifest_lines(paths)


def test_a_duplicated_distribution_is_rejected(tmp_path) -> None:
    paths = _artifacts(tmp_path)

    with pytest.raises(ManifestError, match="duplicate"):
        manifest_lines([*paths, paths[1]])


def test_the_same_distribution_in_two_directories_is_a_duplicate(tmp_path) -> None:
    """`dist/main/*` and `dist/fonts/*` must not be able to name one artifact twice."""
    paths = _artifacts(tmp_path)
    repeated = _write(tmp_path / "elsewhere" / "textflowkit-0.1.5.tar.gz", b"main sdist")

    with pytest.raises(ManifestError, match="duplicate"):
        manifest_lines([paths[0], paths[1], repeated, paths[2], paths[3]])


def test_an_extra_distribution_is_rejected(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    extra = _write(tmp_path / "dist/main" / "textflowkit-0.1.5.zip", b"not a release artifact")

    with pytest.raises(ManifestError, match="unexpected"):
        manifest_lines([*paths, extra])


def test_an_unlisted_project_wheel_is_rejected(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    other = _write(tmp_path / "dist/main" / "textflowkit_extra-0.1.5-py3-none-any.whl", b"other")

    with pytest.raises(ManifestError, match="unexpected"):
        manifest_lines([*paths, other])


def test_a_name_that_only_looks_like_a_fifth_wheel_is_rejected(tmp_path) -> None:
    """`textflowkit-extra-...whl` must not be read as a second `textflowkit`."""
    paths = _artifacts(tmp_path)
    lookalike = _write(tmp_path / "dist/main" / "textflowkit-extra-0.1.5-py3-none-any.whl", b"x")

    with pytest.raises(ManifestError, match="unexpected"):
        manifest_lines([*paths, lookalike])


def test_distributions_that_disagree_on_version_are_rejected(tmp_path) -> None:
    """Four artifacts from two different builds would make the manifest misleading."""
    paths = _artifacts(tmp_path, version=VERSION, fonts_version="0.1.4")

    with pytest.raises(ManifestError, match="version"):
        manifest_lines(paths)


@pytest.mark.parametrize("tag", [VERSION, f"v{VERSION}"])
def test_the_expected_release_version_accepts_the_tag_name(tmp_path, tag) -> None:
    paths = _artifacts(tmp_path)

    assert len(manifest_lines(paths, expect_version=tag)) == 4


def test_an_artifact_from_another_release_is_rejected(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    output = tmp_path / "SHA256SUMS"

    with pytest.raises(ManifestError, match="version"):
        write_release_manifest(paths, output, expect_version="0.1.6")

    assert not output.exists()


def test_a_failed_run_leaves_an_existing_manifest_untouched(tmp_path) -> None:
    paths = _artifacts(tmp_path)
    output = tmp_path / "SHA256SUMS"
    output.write_text("previous release rows\n", encoding="utf-8")

    with pytest.raises(ManifestError):
        write_release_manifest([*paths, paths[0]], output)

    assert output.read_text(encoding="utf-8") == "previous release rows\n"


def test_cli_writes_the_manifest_and_separates_failure_from_success(tmp_path, capsys) -> None:
    paths = _artifacts(tmp_path)
    output = tmp_path / "SHA256SUMS"

    argv = [*map(str, paths), "--output", str(output), "--version", f"v{VERSION}"]
    assert main(argv) == 0
    assert "wrote" in capsys.readouterr().out
    assert manifest_lines(paths) == output.read_text(encoding="utf-8").splitlines()

    bad = tmp_path / "other" / "SHA256SUMS"
    assert main([str(paths[0]), "--output", str(bad)]) == 1
    captured = capsys.readouterr()
    assert "missing" in captured.err and captured.out == ""
    assert not bad.exists()
