"""Refuse to build a release while the README still claims an older one.

A distribution's long description is built from the README at the tagged commit
and frozen on upload. v0.1.5 was published with a long description that still
said "v0.1.4 release", and PyPI cannot rewrite an uploaded release's metadata in
place, so the mistake is permanent for that version (see the erratum in
`docs/user-manual.md`). That bad text is the `## Status` section's opening
paragraph (`git show v0.1.5:README.md` line 247), not the displayed release
link, and the two are separate claims, so this gate checks both:

- the one primary `**Current release: [vX.Y.Z](...).**` line, label and target
  URL together;
- the `## Status` section's opening `**vX.Y.Z release.**` claim.

Both must name the version the tag publishes. A guard that reads only the link
accepts exactly the page that was published as v0.1.5, whose link was right and
whose Status paragraph was stale.

It fails closed. A missing claim, a second one, an ambiguous or malformed one, an
unreadable or non-UTF-8 README, or a section that is absent or duplicated stops
the release instead of building a distribution with an unverifiable claim. A
page that mentions the new version somewhere else is not a pass: `v0.1.5` is
named in several places on the page that must not ship.
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
STATUS_SECTION = "## Status"

# The one release-link line this gate accepts, in the shape the README documents:
#     **Current release: [v0.1.6](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.6).**
# Anything else that presents itself as that line is a defect to fix, not a line
# to skip, so the match is anchored to the whole stripped line.
STATUS_LINE = re.compile(
    r"\A\*\*Current release: \[(?P<label>[^\]]+)\]\((?P<href>[^\s()]+)\)\.\*\*\Z"
)
# Case-insensitive so a near-miss (`**Current Release: ...**`) is caught as a
# malformed link line rather than ignored as prose.
STATUS_CANDIDATE = re.compile(r"\A[*_>\s]*Current release\b", re.IGNORECASE)

# The `## Status` section's opening claim, in the shape the README documents:
#     **v0.1.6 release.** Core, CLI, MCP, and HTTP have automated coverage.
# This is the claim the published v0.1.5 long description got wrong. The rest of
# the opening sentence follows on the same line, so only the bold lead-in is
# matched; an exact `## Status` heading delimits the section.
SECTION_HEADING = re.compile(r"\A#{1,2}[ \t]")
STATUS_HEADING = re.compile(r"\A##[ \t]+Status[ \t]*\Z")
RELEASE_CLAIM = re.compile(r"\A\*\*v(?P<version>[0-9][0-9A-Za-z.+-]*) release\.\*\*")
# What a reader would read as a release claim, whether or not it is well formed:
# a line opening a bold span that mentions "release". A bold-only lead-in that
# does not match RELEASE_CLAIM is a malformed claim to fix, not prose to skip.
CLAIM_CANDIDATE = re.compile(r"\A\*\*[^*]*\brelease\b", re.IGNORECASE)
# A version that can be spelled inside the release URL. The publish workflow has
# already normalized the tag with `packaging`; this only keeps a malformed or
# empty version from being compared as if it were one.
VERSION = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z.+_-]*\Z")


class ReadmeReleaseError(Exception):
    """A release-blocking defect in the README's current-release claim."""


def _status_lines(readme_text: str) -> list[str]:
    """Return every line that presents itself as the primary release link."""
    return [line.strip() for line in readme_text.splitlines() if STATUS_CANDIDATE.match(line.strip())]


def _status_section(readme_text: str) -> list[str]:
    """Return the stripped body lines of the one `## Status` section."""
    lines = readme_text.splitlines()
    headings = [index for index, line in enumerate(lines) if STATUS_HEADING.match(line.strip())]
    if not headings:
        raise ReadmeReleaseError(
            f"no '{STATUS_SECTION}' section in the README; its opening paragraph must "
            "claim the release being published"
        )
    if len(headings) != 1:
        raise ReadmeReleaseError(
            f"expected one '{STATUS_SECTION}' section in the README, found "
            f"{len(headings)}; the displayed release claim is ambiguous"
        )
    start = headings[0] + 1
    for index in range(start, len(lines)):
        if SECTION_HEADING.match(lines[index].strip()):
            return [line.strip() for line in lines[start:index]]
    return [line.strip() for line in lines[start:]]


def _verify_status_claim(readme_text: str, expected_version: str) -> None:
    """Raise unless the `## Status` opening claim names `expected_version`."""
    expected_claim = f"v{expected_version} release."
    body = [line for line in _status_section(readme_text) if line]
    if not body:
        raise ReadmeReleaseError(
            f"the '{STATUS_SECTION}' section is empty; its opening paragraph must claim "
            f"the release being published, as '**{expected_claim}** ...'"
        )
    claims = [line for line in body if CLAIM_CANDIDATE.match(line)]
    if not claims:
        raise ReadmeReleaseError(
            f"no release claim in the '{STATUS_SECTION}' section; it must open with "
            f"'**{expected_claim}** ...'"
        )
    if len(claims) != 1:
        raise ReadmeReleaseError(
            f"expected one release claim in the '{STATUS_SECTION}' section, found "
            f"{len(claims)}: {claims}"
        )
    if claims[0] != body[0]:
        raise ReadmeReleaseError(
            f"the release claim in the '{STATUS_SECTION}' section is not its opening "
            f"paragraph: {claims[0]!r}; expected '**{expected_claim}** ...' first"
        )
    match = RELEASE_CLAIM.match(claims[0])
    if match is None:
        raise ReadmeReleaseError(
            f"the '{STATUS_SECTION}' opening claim is not in the documented shape: "
            f"{claims[0]!r}; expected '**{expected_claim}** ...'"
        )
    claimed = match.group("version")
    if claimed != expected_version:
        raise ReadmeReleaseError(
            f"the '{STATUS_SECTION}' section still claims v{claimed}, not the release "
            f"being published (v{expected_version})"
        )


def verify_readme_release(readme_text: str, version: str) -> None:
    """Raise unless both README release claims name `version`.

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
    # The link is one claim; the section a reader scrolls to is another, and the
    # published v0.1.5 long description got that one wrong while its link was
    # right, so checking the link alone would not have stopped it.
    _verify_status_claim(readme_text, expected_version)


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
                        help="README whose release claims must name the release")
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
