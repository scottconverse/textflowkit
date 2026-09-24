"""Write the SHA256SUMS sidecar for the distributions attached to a release.

The README tells a downloader that releases published by this workflow carry
hashes for their wheel and sdist; releases tagged before this step existed have
no such asset. That promise is only worth something if the manifest hashes the
*same bytes* that were attached and names exactly the distributions the release
publishes. So this script takes the downloaded artifact paths, requires
the complete set of four distributions (a wheel and an sdist for each of the
two projects), rejects anything missing, duplicated, or unexpected, and only
then writes the rows.

Rows are `<digest>  <basename>`, sorted by basename, with `\n` endings, so
`sha256sum -c SHA256SUMS` works from any directory the assets were downloaded
into. Only the basename is recorded: a path would be useless to the downloader
and would leak the runner's scratch layout. Nothing is written unless the whole
set validates, so a failed run cannot leave a partial manifest behind.

Versions are validated per project rather than as one value across all four
artifacts (issue #15, D1). The two projects release on separate contracts: the
core version is what the release tag names, and the fonts companion package may
be published at its own version, so a core release can reuse an earlier fonts
release instead of republishing it. Each project's wheel and sdist must still
carry the same version, and each project is checked against the version
expected for it - `--version` for core, `--fonts-version` for fonts. Naming
only `--version` still requires fonts to match it, which is what every caller
did before this flag existed, and naming neither keeps the older and stricter
meaning: one version across all four artifacts, so the reuse case has to be
asked for. Naming only `--fonts-version` is refused, because it would leave the
core artifacts unpinned.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# The two projects the publish workflow builds and attaches. A wheel and an
# sdist for each is the whole published set; a new package must be added here
# deliberately rather than hashed by accident.
EXPECTED_PROJECTS = ("textflowkit", "textflowkit-fonts")
EXPECTED_KINDS = ("wheel", "sdist")
WHEEL_SUFFIX = ".whl"
SDIST_SUFFIX = ".tar.gz"
# A PEP 440 version starts with a digit and uses only these characters. Without
# this check a malformed name such as `textflowkit-extra-0.1.5-...whl` would be
# read as a second `textflowkit` artifact with the version `extra`.
VERSION = re.compile(r"\A[0-9][0-9A-Za-z.!+]*\Z")


class ManifestError(Exception):
    """A release-blocking defect in the candidate distribution set."""


def _normalize(name: str) -> str:
    """Compare distribution names the way wheel and sdist filenames spell them."""
    return name.replace("-", "_")


def _identify(path: Path) -> tuple[str, str, str]:
    """Return `(project, kind, version)` for one artifact basename."""
    name = path.name
    if name.endswith(WHEEL_SUFFIX):
        # `{distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl`
        fields = name[: -len(WHEEL_SUFFIX)].split("-")
        if len(fields) < 5:
            raise ManifestError(f"unexpected artifact: {name} is not a wheel filename")
        raw, version, kind = fields[0], fields[1], "wheel"
    elif name.endswith(SDIST_SUFFIX):
        # `{distribution}-{version}.tar.gz`
        raw, separator, version = name[: -len(SDIST_SUFFIX)].rpartition("-")
        if not separator or not raw or not version:
            raise ManifestError(f"unexpected artifact: {name} is not an sdist filename")
        kind = "sdist"
    else:
        raise ManifestError(f"unexpected artifact: {name} is not a wheel or an sdist")
    if not VERSION.match(version):
        raise ManifestError(f"unexpected artifact: {name} has no PEP 440 version field")
    project = next((p for p in EXPECTED_PROJECTS if _normalize(p) == _normalize(raw)), None)
    if project is None:
        raise ManifestError(f"unexpected artifact: {name} is not a published project")
    return project, kind, version


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError(f"missing artifact: cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def manifest_lines(
    paths: list[Path],
    *,
    expect_version: str | None = None,
    expect_fonts_version: str | None = None,
) -> list[str]:
    """Validate the candidate set and return its sorted `sha256sum` rows.

    `expect_version` pins the core artifacts (the release tag) and
    `expect_fonts_version` pins the fonts artifacts. When the fonts version is
    not named, the core version is expected of it too, so a caller that names
    one version keeps the guarantee it had before the fonts flag existed.

    The reuse case is opt-in, so a call that pins nothing keeps the historic
    meaning of "one build": all four artifacts must carry the same version. A
    fonts version with no core version is refused rather than accepted, because
    it would name which fonts build was hashed and leave the core artifacts
    unpinned.
    """
    found: dict[tuple[str, str], Path] = {}
    versions: dict[str, str] = {}
    for path in paths:
        project, kind, artifact_version = _identify(path)
        if not path.is_file():
            raise ManifestError(f"missing artifact: {path} does not exist")
        if (project, kind) in found:
            raise ManifestError(
                f"duplicate artifact: {path.name} repeats the {project} {kind} "
                f"already supplied as {found[(project, kind)].name}"
            )
        known = versions.get(project)
        if known is None:
            versions[project] = artifact_version
        elif artifact_version != known:
            raise ManifestError(
                f"artifacts disagree on version: {path.name} is {artifact_version}, "
                f"but the other {project} candidates are {known}"
            )
        found[(project, kind)] = path
    missing = [
        f"{project} {kind}"
        for project in EXPECTED_PROJECTS
        for kind in EXPECTED_KINDS
        if (project, kind) not in found
    ]
    if missing:
        raise ManifestError(f"missing artifact for: {', '.join(missing)}")
    # The release tag names core; fonts is the companion package that may be
    # reused from an earlier release.
    core_project, fonts_project = EXPECTED_PROJECTS
    if expect_version is None and expect_fonts_version is None:
        # Nothing was pinned, which is the call that meant "one build" before a
        # fonts version could differ. It keeps that meaning: one version across
        # the whole set. Reuse has to be asked for.
        if len(set(versions.values())) > 1:
            raise ManifestError(
                "artifacts disagree on version: this set mixes "
                + ", ".join(f"{project} {version}" for project, version in sorted(versions.items()))
                + "; pass --version to pin the release, and --fonts-version too to allow the "
                "fonts package a different version"
            )
    elif expect_version is None:
        # Fail closed: core is what the release tag names, and this call would
        # leave the core artifacts without any expected version.
        raise ManifestError(
            "artifacts disagree on version: --fonts-version was given without --version, "
            "so the textflowkit candidates would be unpinned; pass --version as well"
        )
    else:
        expected_for = {
            core_project: expect_version,
            # Backward compatible default: a caller that names only the release
            # tag still gets its version required of the fonts package.
            fonts_project: (
                expect_fonts_version if expect_fonts_version is not None else expect_version
            ),
        }
        for project, expectation in expected_for.items():
            expected = expectation.removeprefix("v")
            if versions[project] != expected:
                raise ManifestError(
                    f"artifacts disagree on version: {project} candidates are "
                    f"{versions[project]}, but the release expects {expected}"
                )
    return [
        f"{_digest(found[key])}  {found[key].name}"
        for key in sorted(found, key=lambda key: found[key].name)
    ]


def write_release_manifest(
    paths: list[Path],
    output: Path,
    *,
    expect_version: str | None = None,
    expect_fonts_version: str | None = None,
) -> list[str]:
    """Write `output` from the validated set; raise before touching it otherwise."""
    lines = manifest_lines(
        paths, expect_version=expect_version, expect_fonts_version=expect_fonts_version
    )
    text = "".join(f"{line}\n" for line in lines)
    try:
        # Pin LF so the manifest is byte-identical on a Windows maintainer run
        # and on the Linux release runner.
        output.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ManifestError(f"cannot write {output}: {exc}") from exc
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifacts", nargs="+", metavar="ARTIFACT",
                        help="downloaded wheel and sdist paths to hash")
    parser.add_argument("--output", default="SHA256SUMS",
                        help="manifest path, outside the directories published to PyPI")
    parser.add_argument("--version", default=None,
                        help="expected release version; the tag name (v0.1.6) is accepted")
    parser.add_argument("--fonts-version", default=None,
                        help="expected textflowkit-fonts version, which may be older than "
                             "--version; defaults to --version and requires it to be given")
    args = parser.parse_args(argv)
    try:
        lines = write_release_manifest(
            [Path(artifact) for artifact in args.artifacts],
            Path(args.output),
            expect_version=args.version,
            expect_fonts_version=args.fonts_version,
        )
    except ManifestError as exc:
        print(f"release manifest failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.output} with {len(lines)} distribution digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
