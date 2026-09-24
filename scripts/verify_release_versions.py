"""Fail the release unless the tag and both declared package versions agree.

The tag names the core release, so it must be a normalized final `v` version and
the core package must declare exactly that version. The fonts companion package
is versioned on its own contract (issue #15, D1): it may be older than the core
release when the release reuses an earlier fonts publication, and it may be
newer when it ships a fonts change of its own. What has to hold instead is that
the declared fonts version is one core can install - it must satisfy the
requirement the core `export` extra places on `textflowkit-fonts`, which is the
range pip resolves against.

This is deliberately fail-closed: an unreadable or malformed `pyproject.toml`,
a missing `export` extra, a missing or unusable version, and a specifier that
cannot be parsed all stop the build before any distribution is built or
uploaded. It reads the tag from `--tag` or `RELEASE_TAG`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

CORE_PROJECT_FILE = "pyproject.toml"
FONTS_PROJECT_FILE = "packages/textflowkit-fonts/pyproject.toml"
FONTS_DISTRIBUTION = "textflowkit-fonts"
EXPORT_EXTRA = "export"
# `textflowkit-fonts>=0.1.5,<0.2` and anything else the extra declares it as; the
# requirement is matched by name so the specifier can be handed to pip's parser.
FONTS_REQUIREMENT = re.compile(r"\Atextflowkit-fonts\s*(?P<specifier>.*)\Z")


class VerificationError(Exception):
    """A release-blocking inconsistency, described for the operator."""


def _project_table(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerificationError(f"cannot read {path}: {exc}") from exc
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # Python < 3.11; the release runner is 3.12
        raise VerificationError(f"reading {path} requires Python 3.11+ (tomllib)") from exc
    try:
        project = tomllib.loads(raw)["project"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise VerificationError(f"{path} has no usable [project] table: {exc}") from exc
    if not isinstance(project, dict):
        raise VerificationError(f"{path} has no usable [project] table")
    return project


def _declared_version(path: Path, project: dict) -> str:
    version = project.get("version")
    if not isinstance(version, str):
        raise VerificationError(f"{path} declares no project version")
    return version


def _fonts_requirement(core: dict, core_path: Path) -> str:
    """The one requirement core's `export` extra places on the fonts package."""
    extra = core.get("optional-dependencies", {})
    declared = extra.get(EXPORT_EXTRA) if isinstance(extra, dict) else None
    if not isinstance(declared, list):
        raise VerificationError(f"{core_path} declares no {EXPORT_EXTRA!r} extra")
    requirements = [
        requirement for requirement in declared
        if isinstance(requirement, str)
        and FONTS_REQUIREMENT.match(requirement.split(";", 1)[0].strip())
    ]
    if len(requirements) != 1:
        raise VerificationError(
            f"{core_path}'s {EXPORT_EXTRA!r} extra must require {FONTS_DISTRIBUTION} exactly "
            f"once, found {len(requirements)}"
        )
    return requirements[0]


def check_versions(root: Path, tag: str) -> tuple[str, str, str]:
    """Return `(core version, fonts version, fonts requirement)`, or raise."""
    if not tag:
        raise VerificationError("no release tag was named; pass --tag or set RELEASE_TAG")
    if not tag.startswith("v"):
        raise VerificationError(f"release tag must start with v, got {tag!r}")
    try:
        version = str(Version(tag[1:]))
    except InvalidVersion as exc:
        raise VerificationError(f"release tag {tag!r} is not a version: {exc}") from exc
    if tag != f"v{version}":
        raise VerificationError(
            f"release tag {tag!r} is not a normalized version; use v{version}"
        )
    if Version(version).is_prerelease or Version(version).is_devrelease:
        raise VerificationError(f"PyPI release must be a final version, got {tag!r}")

    core_path = root / CORE_PROJECT_FILE
    fonts_path = root / FONTS_PROJECT_FILE
    core = _project_table(core_path)
    core_version = _declared_version(core_path, core)
    if core_version != version:
        raise VerificationError(f"{core_path}: {core_version} != {version}")

    fonts = _project_table(fonts_path)
    fonts_version = _declared_version(fonts_path, fonts)
    requirement = _fonts_requirement(core, core_path)
    specifier = FONTS_REQUIREMENT.match(requirement.split(";", 1)[0].strip()).group("specifier")
    try:
        installed = Version(fonts_version) in SpecifierSet(specifier)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise VerificationError(
            f"cannot check {fonts_path}'s version {fonts_version!r} against {requirement!r}: {exc}"
        ) from exc
    if not installed:
        raise VerificationError(
            f"{fonts_path} declares {fonts_version}, which the core "
            f"{EXPORT_EXTRA!r} extra cannot install: it requires {requirement!r}"
        )
    return version, fonts_version, requirement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the checkout to read the two projects from")
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""),
                        help="the release tag, defaults to $RELEASE_TAG")
    args = parser.parse_args(argv)
    try:
        core_version, fonts_version, requirement = check_versions(Path(args.root), args.tag)
    except VerificationError as exc:
        print(f"release version check failed: {exc}", file=sys.stderr)
        return 1
    print(f"release version check passed: core {core_version} at {args.tag}, fonts "
          f"{fonts_version} within {requirement!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
