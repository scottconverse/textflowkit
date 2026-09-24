"""A released fonts version must be reused as published, or published as new.

`scripts/fetch_published_fonts.py` answers one question at release time: is the
fonts version declared by the tagged commit already on PyPI? If it is, the core
release has to reuse the *original published bytes* - the wheel and sdist PyPI
recorded - rather than rebuilding them, because a rebuild is a different file
with different hashes and PyPI rejects an upload whose filename already exists.
If it is not, the version is new and the workflow builds and publishes it.

Everything here is deterministic: the index is a local fixture, the HTTP
transport is injected, and nothing reaches PyPI, GitHub, or an account. A
version that cannot be resolved - an error that is not a clean 404, a release
carrying anything other than exactly one wheel and one sdist, a file whose
bytes do not match the digest PyPI recorded, or a working tree that no longer
matches the published source - stops the release instead of publishing
something the manifest would misdescribe.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import fetch_published_fonts

PROJECT = "textflowkit-fonts"
VERSION = "0.1.5"
NEW_VERSION = "0.1.6"
INDEX = "https://pypi.org"
JSON_URL = f"{INDEX}/pypi/{PROJECT}/{VERSION}/json"
WHEEL = f"textflowkit_fonts-{VERSION}-py3-none-any.whl"
SDIST = f"textflowkit_fonts-{VERSION}.tar.gz"
FILE_BASE = "https://files.pythonhosted.org/packages/ab/cd/"
PACKAGE_DIR = "packages/textflowkit-fonts"
SDIST_ROOT = f"textflowkit_fonts-{VERSION}/"
# `tomllib` is stdlib from 3.11; the release runner is 3.12, and the tests that
# read a pyproject file are skipped where it is unavailable.
NEEDS_TOMLLIB = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11"
)


def _pyproject(version: str = VERSION) -> bytes:
    return (
        "[project]\n"
        'name = "textflowkit-fonts"\n'
        f'version = "{version}"\n'
    ).encode("utf-8")


def _sources(version: str = VERSION) -> dict[str, bytes]:
    """The tracked files of `packages/textflowkit-fonts`, as the package ships them."""
    return {
        "pyproject.toml": _pyproject(version),
        "README.md": b"# textflowkit-fonts\n",
        "src/textflowkit_fonts/__init__.py": b'_FONTS = "fonts"\n',
        "src/textflowkit_fonts/fonts/NotoSans.ttf": b"\x00\x01\x00\x00noto-bytes",
        "src/textflowkit_fonts/fonts/OFL-NotoSans.txt": b"SIL Open Font License\n",
    }


def _wheel_bytes(sources: dict[str, bytes], version: str = VERSION) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        prefix = f"textflowkit_fonts-{version}.dist-info/"
        wheel.writestr(
            f"{prefix}METADATA",
            f"Metadata-Version: 2.1\nName: textflowkit-fonts\nVersion: {version}\n",
        )
        for relative, data in sorted(sources.items()):
            if relative.startswith("src/"):
                wheel.writestr(relative[len("src/"):], data)
    return buffer.getvalue()


def _sdist_bytes(sources: dict[str, bytes], version: str = VERSION) -> bytes:
    """A real gzipped tarball: the published sdist, plus the generated PKG-INFO."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add(f"textflowkit_fonts-{version}/PKG-INFO", b"Metadata-Version: 2.1\n")
        for relative, data in sorted(sources.items()):
            add(f"textflowkit_fonts-{version}/{relative}", data)
    return buffer.getvalue()


def _entry(
    filename: str,
    data: bytes,
    *,
    packagetype: str | None = None,
    url: str | None = None,
    sha256: str | None = None,
    size: int | None = None,
) -> dict:
    if packagetype is None:
        packagetype = "bdist_wheel" if filename.endswith(".whl") else "sdist"
    return {
        "filename": filename,
        "packagetype": packagetype,
        "url": FILE_BASE + filename if url is None else url,
        "digests": {"sha256": hashlib.sha256(data).hexdigest() if sha256 is None else sha256},
        "size": len(data) if size is None else size,
    }


def _payload(entries: list[dict], version: str = VERSION) -> dict:
    return {"info": {"name": PROJECT, "version": version}, "urls": entries}


def _routes(
    sources: dict[str, bytes],
    *,
    version: str = VERSION,
    entries: list[dict] | None = None,
    json_answer: object | None = None,
) -> dict[str, object]:
    """The index answers for one published release, keyed by URL."""
    wheel = _wheel_bytes(sources, version)
    sdist = _sdist_bytes(sources, version)
    published = entries if entries is not None else [
        _entry(f"textflowkit_fonts-{version}-py3-none-any.whl", wheel),
        _entry(f"textflowkit_fonts-{version}.tar.gz", sdist),
    ]
    return {
        f"{INDEX}/pypi/{PROJECT}/{version}/json": (
            _payload(published, version) if json_answer is None else json_answer
        ),
        FILE_BASE + f"textflowkit_fonts-{version}-py3-none-any.whl": wheel,
        FILE_BASE + f"textflowkit_fonts-{version}.tar.gz": sdist,
    }


class _Response:
    """A minimal urllib response: status, streaming `read`, context manager."""

    def __init__(self, raw: bytes, status: int = 200) -> None:
        self._raw = raw
        self.status = status

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self._raw = self._raw, b""
            return data
        data, self._raw = self._raw[:size], self._raw[size:]
        return data

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener(routes: dict[str, object], seen: list[str] | None = None):
    def open_(request, timeout=None):
        url = request.full_url
        if seen is not None:
            seen.append(url)
        answer = routes.get(url)
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return _Response(json.dumps(answer).encode("utf-8"))
        assert isinstance(answer, bytes), url
        return _Response(answer)

    return open_


@dataclass
class Resolved:
    code: int
    package_dir: Path
    out_dir: Path
    stdout: str
    stderr: str

    def files(self) -> list[str]:
        if not self.out_dir.is_dir():
            return []
        return sorted(path.name for path in self.out_dir.iterdir())


def _run(
    tmp_path: Path,
    routes: dict[str, object] | None = None,
    *,
    sources: dict[str, bytes] | None = None,
    version: str = VERSION,
    argv: list[str] | None = None,
    seen: list[str] | None = None,
    capsys=None,
) -> Resolved:
    package_dir = tmp_path / PACKAGE_DIR
    for relative, data in (sources if sources is not None else _sources(version)).items():
        path = package_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    out_dir = tmp_path / "dist" / "fonts"
    arguments = argv if argv is not None else [
        "--package-dir", str(package_dir), "--out-dir", str(out_dir), "--version", version,
    ]
    code = fetch_published_fonts.main(arguments, opener=_opener(routes or {}, seen))
    captured = capsys.readouterr()
    return Resolved(code, package_dir, out_dir, captured.out, captured.err)


# --- the two outcomes ---------------------------------------------------------


def test_a_version_already_on_pypi_is_reused_from_the_published_files(tmp_path, capsys) -> None:
    sources = _sources()
    result = _run(tmp_path, _routes(sources), sources=sources, capsys=capsys)

    assert result.code == 0, result.stderr
    assert result.files() == [WHEEL, SDIST]
    assert result.files() == sorted([WHEEL, SDIST])
    assert "reuse" in result.stdout
    for name in (WHEEL, SDIST):
        assert (result.out_dir / name).read_bytes() == (
            _wheel_bytes(sources) if name == WHEEL else _sdist_bytes(sources)
        )


def test_the_fetched_files_are_the_bytes_the_index_recorded(tmp_path, capsys) -> None:
    sources = _sources()
    result = _run(tmp_path, _routes(sources), sources=sources, capsys=capsys)

    assert result.code == 0, result.stderr
    for entry in _routes(sources)[JSON_URL]["urls"]:
        fetched = (result.out_dir / entry["filename"]).read_bytes()
        assert hashlib.sha256(fetched).hexdigest() == entry["digests"]["sha256"]
        assert len(fetched) == entry["size"]


def test_a_version_that_is_not_on_pypi_is_new_and_downloads_nothing(tmp_path, capsys) -> None:
    """An exact 404 is the only signal that this version is new."""
    result = _run(tmp_path, {}, version=NEW_VERSION, capsys=capsys)

    assert result.code == 0, result.stderr
    assert "new" in result.stdout
    assert not result.out_dir.exists()


def test_a_reused_release_does_not_touch_the_package_tree(tmp_path, capsys) -> None:
    """Reuse must be a fetch, never a rebuild: the source tree is left alone."""
    sources = _sources()
    before = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    result = _run(tmp_path, _routes(sources), sources=sources, capsys=capsys)

    assert result.code == 0, result.stderr
    after = {
        str(path.relative_to(result.package_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(result.package_dir.rglob("*"))
        if path.is_file()
    }
    assert after == before


# --- the index answer ---------------------------------------------------------


def test_a_transient_index_error_stops_the_release(tmp_path, capsys) -> None:
    routes = {JSON_URL: urllib.error.HTTPError(JSON_URL, 503, "Service Unavailable", {}, None)}
    result = _run(tmp_path, routes, capsys=capsys)

    assert result.code == 1
    assert "503" in result.stderr
    assert result.stdout == ""
    assert not result.out_dir.exists()


def test_an_unreachable_index_stops_the_release(tmp_path, capsys) -> None:
    routes = {JSON_URL: urllib.error.URLError("no route to host")}
    result = _run(tmp_path, routes, capsys=capsys)

    assert result.code == 1
    assert "cannot reach" in result.stderr
    assert not result.out_dir.exists()


def test_a_response_that_is_not_json_stops_the_release(tmp_path, capsys) -> None:
    routes = {JSON_URL: b"<html>not json</html>"}
    result = _run(tmp_path, routes, capsys=capsys)

    assert result.code == 1
    assert "JSON" in result.stderr


def test_a_response_without_a_urls_list_stops_the_release(tmp_path, capsys) -> None:
    routes = {JSON_URL: {"info": {"name": PROJECT, "version": VERSION}}}
    result = _run(tmp_path, routes, capsys=capsys)

    assert result.code == 1
    assert "urls" in result.stderr


def test_a_release_with_only_one_published_file_stops_the_release(tmp_path, capsys) -> None:
    """A half-finished publication must not be read as a reuse or as a new version."""
    sources = _sources()
    wheel = _entry(WHEEL, _wheel_bytes(sources))
    result = _run(
        tmp_path, _routes(sources, entries=[wheel]), sources=sources, capsys=capsys
    )

    assert result.code == 1
    assert "sdist" in result.stderr
    assert not result.out_dir.exists()


def test_a_release_with_a_duplicated_file_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    wheel = _entry(WHEEL, _wheel_bytes(sources))
    sdist = _entry(SDIST, _sdist_bytes(sources))
    result = _run(
        tmp_path,
        _routes(sources, entries=[wheel, sdist, _entry(SDIST, _sdist_bytes(sources))]),
        sources=sources,
        capsys=capsys,
    )

    assert result.code == 1
    assert "duplicate" in result.stderr
    assert not result.out_dir.exists()


def test_an_extra_published_file_stops_the_release(tmp_path, capsys) -> None:
    """Only the wheel and the sdist are ever attached, so a third file is refused."""
    sources = _sources()
    extra = _entry(f"textflowkit_fonts-{VERSION}.zip", b"not an attached artifact")
    entries = [
        _entry(WHEEL, _wheel_bytes(sources)),
        _entry(SDIST, _sdist_bytes(sources)),
        extra,
    ]
    result = _run(tmp_path, _routes(sources, entries=entries), sources=sources, capsys=capsys)

    assert result.code == 1
    assert "unexpected" in result.stderr


def test_a_published_file_for_another_version_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    other = _entry("textflowkit_fonts-0.1.4-py3-none-any.whl", b"another release")
    entries = [
        other,
        _entry(WHEEL, _wheel_bytes(sources)),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    result = _run(tmp_path, _routes(sources, entries=entries), sources=sources, capsys=capsys)

    assert result.code == 1
    assert "0.1.4" in result.stderr


def test_a_published_file_without_a_digest_stops_the_release(tmp_path, capsys) -> None:
    """Without PyPI's own digest there is nothing to verify the download against."""
    sources = _sources()
    sdist = _entry(SDIST, _sdist_bytes(sources))
    sdist["digests"] = {}
    entries = [_entry(WHEEL, _wheel_bytes(sources)), sdist]
    result = _run(tmp_path, _routes(sources, entries=entries), sources=sources, capsys=capsys)

    assert result.code == 1
    assert "sha256" in result.stderr.lower()
    assert not result.out_dir.exists()


def test_a_file_url_that_is_not_https_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    entries = [
        _entry(WHEEL, _wheel_bytes(sources), url=f"http://files.example/{WHEEL}"),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    result = _run(tmp_path, _routes(sources, entries=entries), sources=sources, capsys=capsys)

    assert result.code == 1
    assert "https" in result.stderr
    assert not result.out_dir.exists()


def test_a_url_that_climbs_out_of_the_output_directory_stops_the_release(
    tmp_path, capsys
) -> None:
    """A `..` segment in the file URL must not become a write outside the release."""
    sources = _sources()
    escaping = f"../../../{WHEEL}"
    entries = [
        _entry(WHEEL, _wheel_bytes(sources), url=FILE_BASE + escaping),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    routes = _routes(sources, entries=entries)
    routes[FILE_BASE + escaping] = _wheel_bytes(sources)
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert not (tmp_path / WHEEL).exists()
    assert not result.out_dir.exists()


def test_a_filename_with_a_path_separator_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    entries = [
        _entry(f"sub/{WHEEL}", _wheel_bytes(sources), url=FILE_BASE + f"sub/{WHEEL}"),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    routes = _routes(sources, entries=entries)
    routes[FILE_BASE + f"sub/{WHEEL}"] = _wheel_bytes(sources)
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert not result.out_dir.exists()


def test_a_url_that_does_not_name_its_filename_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    entries = [
        _entry(WHEEL, _wheel_bytes(sources), url=FILE_BASE + SDIST),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    result = _run(tmp_path, _routes(sources, entries=entries), sources=sources, capsys=capsys)

    assert result.code == 1
    assert not result.out_dir.exists()


# --- the download itself ------------------------------------------------------


def test_a_download_that_does_not_match_the_recorded_digest_stops_the_release(
    tmp_path, capsys
) -> None:
    sources = _sources()
    entries = [
        _entry(WHEEL, _wheel_bytes(sources)),
        _entry(SDIST, _sdist_bytes(sources)),
    ]
    routes = _routes(sources, entries=entries)
    routes[FILE_BASE + SDIST] = _sdist_bytes(sources) + b"corruption"
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert "SHA-256" in result.stderr
    assert result.files() == []


def test_a_truncated_download_stops_the_release(tmp_path, capsys) -> None:
    sources = _sources()
    sdist = _sdist_bytes(sources)
    routes = _routes(sources)
    routes[FILE_BASE + SDIST] = sdist[: len(sdist) // 2]
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert "bytes" in result.stderr
    assert result.files() == []


def test_an_artifact_that_is_not_a_valid_archive_stops_the_release(tmp_path, capsys) -> None:
    """A digest PyPI recorded for a file that is not a distribution is still no release."""
    sources = _sources()
    broken = b"this is not a gzipped tarball"
    entries = [
        _entry(WHEEL, _wheel_bytes(sources)),
        _entry(SDIST, broken),
    ]
    routes = _routes(sources, entries=entries)
    routes[FILE_BASE + SDIST] = broken
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert result.files() == []


def test_a_failed_download_leaves_no_partial_file_behind(tmp_path, capsys) -> None:
    sources = _sources()
    routes = _routes(sources)
    routes[FILE_BASE + WHEEL] = urllib.error.HTTPError(
        FILE_BASE + WHEEL, 404, "Not Found", {}, None
    )
    result = _run(tmp_path, routes, sources=sources, capsys=capsys)

    assert result.code == 1
    assert result.files() == []
    if result.out_dir.is_dir():
        assert not [path for path in result.out_dir.iterdir() if path.name.endswith(".part")]


# --- source drift -------------------------------------------------------------


def test_an_unchanged_fonts_tree_is_reused(tmp_path, capsys) -> None:
    sources = _sources()
    result = _run(tmp_path, _routes(sources), sources=sources, capsys=capsys)

    assert result.code == 0, result.stderr
    assert result.files() == [WHEEL, SDIST]


def test_a_changed_fonts_tree_stops_the_reuse_and_names_the_file(tmp_path, capsys) -> None:
    """A changed package with an unbumped version must not silently reuse the old files."""
    published = _sources()
    changed = {**published, "src/textflowkit_fonts/fonts/README.md": b"a new attribution note\n"}

    assert "src/textflowkit_fonts/fonts/README.md" not in published
    result = _run(tmp_path, _routes(published), sources=changed, capsys=capsys)

    assert result.code == 1
    assert "src/textflowkit_fonts/fonts/README.md" in result.stderr
    assert "bump" in result.stderr
    assert result.files() == []


def test_a_new_file_in_the_fonts_tree_stops_the_reuse(tmp_path, capsys) -> None:
    published = _sources()
    added = {**published, "src/textflowkit_fonts/fonts/NotoSerif.ttf": b"\x00\x01\x00\x00serif"}
    result = _run(tmp_path, _routes(published), sources=added, capsys=capsys)

    assert result.code == 1
    assert "src/textflowkit_fonts/fonts/NotoSerif.ttf" in result.stderr


def test_a_fonts_source_file_missing_from_the_tree_stops_the_reuse(tmp_path, capsys) -> None:
    published = _sources()
    removed = {name: data for name, data in published.items() if not name.endswith("OFL-NotoSans.txt")}
    result = _run(tmp_path, _routes(published), sources=removed, capsys=capsys)

    assert result.code == 1
    assert "OFL-NotoSans.txt" in result.stderr


def test_an_added_pyproject_change_alone_stops_the_reuse(tmp_path, capsys) -> None:
    """The comparison is the whole package tree, not only the font binaries."""
    published = _sources()
    changed = {**published, "README.md": b"# textflowkit-fonts\n\nNew wording.\n"}
    result = _run(tmp_path, _routes(published), sources=changed, capsys=capsys)

    assert result.code == 1
    assert "README.md" in result.stderr


# --- the declared version and the CLI -----------------------------------------


@NEEDS_TOMLLIB
def test_the_declared_fonts_version_comes_from_the_package_pyproject(tmp_path, capsys) -> None:
    sources = _sources()
    result = _run(
        tmp_path,
        _routes(sources),
        sources=sources,
        capsys=capsys,
        argv=["--package-dir", str(tmp_path / PACKAGE_DIR), "--out-dir", str(tmp_path / "dist/fonts")],
    )

    assert result.code == 0, result.stderr
    assert result.files() == [WHEEL, SDIST]


@NEEDS_TOMLLIB
def test_an_unreadable_pyproject_stops_the_release(tmp_path, capsys) -> None:
    result = _run(
        tmp_path,
        {},
        sources={"README.md": b"# no pyproject here\n"},
        capsys=capsys,
        argv=["--package-dir", str(tmp_path / PACKAGE_DIR), "--out-dir", str(tmp_path / "dist/fonts")],
    )

    assert result.code == 1
    assert "pyproject.toml" in result.stderr


def test_the_workflow_output_file_records_the_mode(tmp_path, capsys) -> None:
    """The build job publishes the mode to later jobs; it must be one exact word."""
    sources = _sources()
    output = tmp_path / "github-output"
    result = _run(
        tmp_path,
        _routes(sources),
        sources=sources,
        capsys=capsys,
        argv=[
            "--package-dir", str(tmp_path / PACKAGE_DIR),
            "--out-dir", str(tmp_path / "dist/fonts"),
            "--version", VERSION,
            "--github-output", str(output),
        ],
    )

    assert result.code == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "fonts_mode=reuse\n"


def test_the_workflow_output_records_a_new_version(tmp_path, capsys) -> None:
    output = tmp_path / "github-output"
    result = _run(
        tmp_path,
        {},
        version=NEW_VERSION,
        capsys=capsys,
        argv=[
            "--package-dir", str(tmp_path / PACKAGE_DIR),
            "--out-dir", str(tmp_path / "dist/fonts"),
            "--version", NEW_VERSION,
            "--github-output", str(output),
        ],
    )

    assert result.code == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "fonts_mode=new\n"


def test_a_failed_release_writes_no_mode_at_all(tmp_path, capsys) -> None:
    """A later job must never read a mode that was not earned."""
    output = tmp_path / "github-output"
    routes = {JSON_URL: urllib.error.HTTPError(JSON_URL, 503, "Service Unavailable", {}, None)}
    result = _run(
        tmp_path,
        routes,
        capsys=capsys,
        argv=[
            "--package-dir", str(tmp_path / PACKAGE_DIR),
            "--out-dir", str(tmp_path / "dist/fonts"),
            "--version", VERSION,
            "--github-output", str(output),
        ],
    )

    assert result.code == 1
    assert not output.exists()


def test_only_the_index_and_the_recorded_files_are_requested(tmp_path, capsys) -> None:
    sources = _sources()
    seen: list[str] = []
    result = _run(tmp_path, _routes(sources), sources=sources, capsys=capsys, seen=seen)

    assert result.code == 0, result.stderr
    assert seen == [JSON_URL, FILE_BASE + WHEEL, FILE_BASE + SDIST]


def test_a_plain_http_index_is_refused(tmp_path, capsys) -> None:
    """The release reads PyPI over HTTPS; a downgraded index is not negotiated."""
    result = _run(
        tmp_path,
        {},
        version=VERSION,
        capsys=capsys,
        argv=[
            "--package-dir", str(tmp_path / PACKAGE_DIR),
            "--out-dir", str(tmp_path / "dist/fonts"),
            "--version", VERSION,
            "--index-url", "http://pypi.org",
        ],
    )

    assert result.code == 1
    assert "https" in result.stderr
