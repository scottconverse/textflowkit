"""Do not publish a new version while public documentation still names the old one."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from textflowkit import __version__

ROOT = Path(__file__).resolve().parents[1]

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


def test_release_version_matches_both_packages_and_current_public_surfaces() -> None:
    version = __version__
    assert _declared_version(ROOT / "pyproject.toml") == version
    assert _declared_version(ROOT / "packages/textflowkit-fonts/pyproject.toml") == version

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
