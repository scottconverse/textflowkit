"""Keep the static GitHub Pages entry point and its local navigation intact."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "docs" / "index.html"


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "id" in attributes and attributes["id"] is not None:
            self.ids.append(attributes["id"])
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag == "script":
            self.scripts.append(attributes.get("src") or "inline")


def test_github_pages_landing_has_working_local_navigation() -> None:
    page = PageLinks()
    source = PAGE.read_text(encoding="utf-8")
    page.feed(source)

    assert "<title>TextFlowKit" in source
    assert len(page.ids) == len(set(page.ids)), "duplicate HTML id"
    assert all(link[1:] in page.ids for link in page.links if link.startswith("#"))
    assert not page.scripts, "the landing page should remain dependency-free"
    assert (PAGE.parent / ".nojekyll").is_file()
    assert "https://github.com/scottconverse/textflowkit" in page.links
