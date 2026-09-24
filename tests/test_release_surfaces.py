"""Do not publish a new version while public documentation still names the old one.

The core package and the fonts companion package have separate release
contracts (issue #15, D1). The *core* version must still match every public
surface: `pyproject.toml`, the README, the install guide, the adapters doc, the
user manual, the roadmap entry, and the website's primary release link. The
fonts package may be published at its own version - what has to hold is that
the declared fonts version is one core can install, i.e. that it satisfies the
`export` extra's requirement in `pyproject.toml`.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from textflowkit import __version__

ROOT = Path(__file__).resolve().parents[1]
FONTS_PYPROJECT = ROOT / "packages/textflowkit-fonts/pyproject.toml"
# The one requirement string that says which fonts builds core can be installed
# with. Matching the requirement rather than the whole dependency line keeps the
# check meaningful if the line's other contents change.
FONTS_REQUIREMENT = re.compile(r'"textflowkit-fonts([^"]*)"')

# Anchor for the website's primary release link. Matching on this copy rather than on the
# version string keeps the guard meaningful: the page mentions the version in several other
# places, so a bare substring test stays green while the link itself points at a stale tag.
RELEASE_MARKER = "latest verified release"
RELEASE_TAG_URL = "https://github.com/scottconverse/textflowkit/releases/tag/v{version}"


def _declared_version(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', source, re.MULTILINE)
    assert match is not None, f"missing version in {path}"
    return match.group(1)


class _ParagraphAnchors(HTMLParser):
    """Group each paragraph's visible text with the anchors it contains."""

    def __init__(self) -> None:
        super().__init__()
        self.paragraphs: list[tuple[str, list[tuple[str | None, str]]]] = []
        self._text: list[str] = []
        self._anchors: list[tuple[str | None, str]] = []
        self._in_paragraph = False
        self._in_anchor = False
        self._href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._in_paragraph = True
            self._text = []
            self._anchors = []
        elif tag == "a" and self._in_paragraph:
            self._in_anchor = True
            self._href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_anchor:
            self._anchors.append((self._href, "".join(self._anchor_text).strip()))
            self._in_anchor = False
        elif tag == "p" and self._in_paragraph:
            self.paragraphs.append(("".join(self._text).strip(), self._anchors))
            self._in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self._in_paragraph:
            self._text.append(data)
            if self._in_anchor:
                self._anchor_text.append(data)


def _primary_release_paragraph(page: Path) -> list[tuple[str | None, str]]:
    parser = _ParagraphAnchors()
    parser.feed(page.read_text(encoding="utf-8"))
    marked = [
        anchors
        for text, anchors in parser.paragraphs
        if text.casefold().startswith(RELEASE_MARKER)
    ]
    assert len(marked) == 1, f"expected one 'Latest verified release' line in {page}, found {len(marked)}"
    return marked[0]


def test_website_primary_release_link_shows_and_targets_the_current_version() -> None:
    version = __version__
    expected_href = RELEASE_TAG_URL.format(version=version)
    anchors = _primary_release_paragraph(ROOT / "docs/index.html")

    matching = [anchor for anchor in anchors if anchor[0] == expected_href]
    assert len(matching) == 1, f"expected the primary release link to target {expected_href}, found {[a[0] for a in anchors]}"
    assert matching[0][1] == f"v{version}", f"primary release link displays {matching[0][1]!r}, expected 'v{version}'"

    labels = [text for _, text in anchors if text == f"v{version}"]
    assert len(labels) == 1, f"expected exactly one 'v{version}' release label, found {labels}"


def test_release_version_matches_the_core_package_and_current_public_surfaces() -> None:
    version = __version__
    assert _declared_version(ROOT / "pyproject.toml") == version
    # The fonts package is checked separately, against the version range core
    # can install: requiring it to equal the core version is the coupling D1
    # removes, and it is not what keeps a release honest.

    # docs/index.html is covered semantically by
    # test_website_primary_release_link_shows_and_targets_the_current_version: a substring
    # check there stayed green even when the primary release link pointed at a stale tag.
    for path in (
        ROOT / "README.md",
        ROOT / "docs/install.md",
        ROOT / "docs/adapters.md",
        ROOT / "docs/user-manual.md",
    ):
        assert f"v{version}" in path.read_text(encoding="utf-8"), path

    roadmap = (ROOT / "docs/roadmap.md").read_text(encoding="utf-8")
    assert f"- [x] Ship v{version} review follow-ups" in roadmap


def _core_fonts_requirement() -> SpecifierSet:
    """The version constraint core's `export` extra places on the fonts package.

    `pyproject.toml` is the one place that says which fonts builds core can be
    installed with. A declared fonts version outside it would be a release set
    pip could not resolve, however well the two versions are pinned elsewhere.
    """
    match = FONTS_REQUIREMENT.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert match is not None, "core pyproject.toml must require textflowkit-fonts in `export`"
    return SpecifierSet(match.group(1))


def test_the_declared_fonts_version_is_inside_the_core_export_requirement() -> None:
    fonts_version = _declared_version(FONTS_PYPROJECT)

    assert Version(fonts_version) in _core_fonts_requirement()


def test_a_core_release_may_reuse_an_earlier_fonts_release() -> None:
    """Core 0.1.6 published with the fonts package still at 0.1.5 must install.

    That reuse is the point of D1, and it is what the current 0.1.5 has to keep
    satisfying: the range, not an equality with the core version, is the
    contract. `pyproject.toml`'s export spec is unchanged by this unit.
    """
    assert Version("0.1.5") in _core_fonts_requirement()
