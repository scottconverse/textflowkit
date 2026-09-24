"""Refuse to build a release while the README's primary release line is stale.

A distribution's long description is built from the README at the tagged commit
and frozen on upload. v0.1.5 was published with a long description that still
said "v0.1.4 release", and PyPI cannot rewrite an uploaded release's metadata in
place, so the mistake is permanent for that version (see the erratum in
`docs/user-manual.md`). The README's own status line is the only part of the
long description that makes a version claim, so this gate reads exactly that
line - the label a reader sees and the release URL it links to - and requires
both to name the version the tag publishes.

It fails closed. A missing line, a second one, an unreadable or non-UTF-8
README, or a line in any shape other than the documented one stops the release
instead of building a distribution with an unverifiable claim. A page that
mentions the new version somewhere else is not a pass: that is precisely the
state that published v0.1.5.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_README = "README.md"
RELEASE_TAG_URL = "https://github.com/scottconverse/textflowkit/releases/tag/v{version}"
STATUS_PREFIX = "Current release"

# The one line this gate accepts, in the shape the README documents:
#     **Current release: [v0.1.6](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.6).**
# Anything else that presents itself as the status line is a defect to fix, not
# a line to skip, so the match is anchored to the whole stripped line.
STATUS_LINE = re.compile(
    r"\A\*\*Current release: \[(?P<label>[^\]]+)\]\((?P<href>[^\s()]+)\)\.\*\*\Z"
)
# Case-insensitive so a near-miss (`**Current Release: ...**`) is caught as a
# malformed status line rather than ignored as prose.
STATUS_CANDIDATE = re.compile(r"\A[*_>\s]*Current release\b", re.IGNORECASE)
# A version that can be spelled inside the release URL. The publish workflow has
# already normalized the tag with `packaging`; this only keeps a malformed or
# empty version from being compared as if it were one.
VERSION = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z.+_-]*\Z")


class ReadmeReleaseError(Exception):
    """A release-blocking defect in the README's current-release claim."""


def _status_lines(readme_text: str) -> list[str]:
    """Return every line that presents itself as the primary status line."""
    return [line.strip() for line in readme_text.splitlines() if STATUS_CANDIDATE.match(line.strip())]


def verify_readme_release(readme_text: str, version: str) -> None:
    """Raise unless the README's primary status line and its target name `version`.

    `version` is the release version; the tag form (`v0.1.6`) is accepted too.
    """
    # No trimming: a version carrying whitespace or punctuation is not a release
    # version, and silently accepting `"0.1.6 "` would let a malformed tag name
    # a line it does not actually match.
    expected_version = version.removeprefix("v")
    if not VERSION.match(expected_version):
        raise ReadmeReleaseError(
            f"cannot check a README claim against the version {version!r}; "
            "expected a release version such as 0.1.6 or the tag v0.1.6"
        )
    expected_label = f"v{expected_version}"
    expected_href = RELEASE_TAG_URL.format(version=expected_version)

    lines = _status_lines(readme_text)
    if not lines:
        raise ReadmeReleaseError(
            f"no '{STATUS_PREFIX}' line in the README; it must carry exactly one "
            f"line of the form '**{STATUS_PREFIX}: [{expected_label}]({expected_href}).**'"
        )
    if len(lines) != 1:
        raise ReadmeReleaseError(
            f"expected one '{STATUS_PREFIX}' line in the README, found {len(lines)}: "
            f"{lines}"
        )
    status = lines[0]
    match = STATUS_LINE.match(status)
    if match is None:
        raise ReadmeReleaseError(
            f"the README's '{STATUS_PREFIX}' line is not in the documented shape: "
            f"{status!r}; expected '**{STATUS_PREFIX}: [{expected_label}]({expected_href}).**'"
        )
    label, href = match.group("label"), match.group("href")
    stale = [
        f"label {label!r} is not {expected_label!r}" if label != expected_label else None,
        f"target {href!r} is not {expected_href!r}" if href != expected_href else None,
    ]
    stale = [defect for defect in stale if defect is not None]
    if stale:
        raise ReadmeReleaseError(
            f"the README's '{STATUS_PREFIX}' line does not name the release being "
            f"published (v{expected_version}): " + "; ".join(stale)
        )


def check_readme_release(path: Path, version: str) -> None:
    """Read `path` and check it; an unreadable file is a release blocker."""
    try:
        readme_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReadmeReleaseError(f"cannot read {path}: {exc}") from exc
    verify_readme_release(readme_text, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readme", default=DEFAULT_README,
                        help="README whose current-release line must name the release")
    parser.add_argument("--version", default=os.environ.get("RELEASE_TAG", ""),
                        help="release version or tag, defaults to $RELEASE_TAG")
    args = parser.parse_args(argv)
    try:
        check_readme_release(Path(args.readme), args.version)
    except ReadmeReleaseError as exc:
        if not args.version:
            print("README release guard failed: no version to check; pass --version "
                  "or set RELEASE_TAG", file=sys.stderr)
        else:
            print(f"README release guard failed: {exc}", file=sys.stderr)
        return 1
    print(f"README current-release line names {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
