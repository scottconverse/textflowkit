"""Unknown site paths must answer with a distinct, useful not-found page.

UX-02: the site is served by Cloudflare Pages from ``docs/``. Cloudflare Pages returns a
top-level ``404.html`` for missing files; without that file it assumes single-page
application routing and matches every unmatched path to ``/``, so an unknown URL returns
the landing page with HTTP 200. The deployment notes in ``docs/site-deployment.md`` record
the post-deploy check for this.

These are static checks of the Pages build output and of the deployment notes. A local file
server is not Cloudflare Pages, so these tests deliberately do not claim to observe a live
HTTP 404; the status can only be confirmed after a deployment.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
NOT_FOUND = DOCS / "404.html"
LANDING = DOCS / "index.html"
DEPLOYMENT = DOCS / "site-deployment.md"

# The canonical home, in the two absolute forms a 404 document may link to. A relative
# link is not acceptable here: the browser resolves it against the unknown path that
# triggered the 404, not against the 404 document's own location.
HOME_URLS = frozenset({"https://www.textflowkit.org/", "/"})

# A not-found document has to say that the requested page is missing.
MISSING_PAGE_WORDS = re.compile(
    r"could\s*n[o']?t find|can\s*not find|can't find|not found|does\s*n[o']?t exist|"
    r"no such page|no page",
    re.IGNORECASE,
)

# Resource-bearing tags and the attribute that names their URL.
RESOURCE_ATTRS = {"link": "href", "img": "src", "iframe": "src", "object": "data",
                  "embed": "src", "source": "src", "video": "src", "audio": "src"}


class NotFoundPage(HTMLParser):
    """The pieces of a 404 document the site policy cares about."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.external_resources: list[str] = []
        self.forms = 0
        self.file_inputs = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "a" and attributes.get("href"):
            self.links.append(str(attributes["href"]))
        elif tag == "script":
            self.scripts.append(attributes.get("src") or "inline")
        elif tag == "form":
            self.forms += 1
        elif tag == "input" and (attributes.get("type") or "").lower() == "file":
            self.file_inputs += 1
        if tag in RESOURCE_ATTRS:
            url = attributes.get(RESOURCE_ATTRS[tag]) or ""
            if url.startswith(("http://", "https://", "//")):
                self.external_resources.append(url)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._in_title:
            self.title += data


def _source() -> str:
    assert NOT_FOUND.is_file(), (
        "docs/404.html is missing; without a top-level 404 page Cloudflare Pages assumes "
        "single-page-application routing and serves the landing page with HTTP 200 for "
        "unknown paths"
    )
    return NOT_FOUND.read_text(encoding="utf-8")


def _parse() -> NotFoundPage:
    page = NotFoundPage()
    page.feed(_source())
    return page


def test_a_not_found_page_exists_at_the_pages_output_root() -> None:
    assert NOT_FOUND.is_file(), (
        "Cloudflare Pages looks for a top-level 404.html for missing files; a local HTTP "
        "file server cannot stand in for this"
    )


def test_not_found_page_has_a_useful_title_and_message() -> None:
    page = _parse()
    body = " ".join(page.text)
    assert "TextFlowKit" in page.title, f"unhelpful title {page.title!r}"
    assert re.search(r"404|not found", page.title, re.IGNORECASE), (
        f"the title {page.title!r} does not identify a missing page"
    )
    assert MISSING_PAGE_WORDS.search(body), (
        "the page must tell the reader the requested page is missing; body text was "
        f"{body.strip()[:200]!r}"
    )


def test_not_found_page_has_a_clear_canonical_home_link() -> None:
    page = _parse()
    assert page.links, "the page offers no way back to the site"
    home = [link for link in page.links if link in HOME_URLS]
    assert home, (
        f"no link to the canonical home {sorted(HOME_URLS)}; found {page.links}"
    )
    relative = [link for link in page.links if not link.startswith(("/", "http"))]
    assert not relative, (
        f"relative links {relative} resolve against the unknown requested path, not "
        "against the 404 document's own location"
    )


def test_not_found_page_is_distinct_from_the_landing_page() -> None:
    landing = LANDING.read_text(encoding="utf-8")
    source = _source()
    assert source != landing, "the 404 page is the landing page"
    assert _parse().title != re.search(r"<title>(.*?)</title>", landing, re.DOTALL).group(1), (
        "the 404 page reuses the landing page title, so the two are not distinguishable"
    )


def test_not_found_page_stays_dependency_free() -> None:
    page = _parse()
    assert not page.scripts, "the 404 page must remain dependency-free (no scripts)"
    assert not page.external_resources, (
        f"the 404 page pulls external resources {page.external_resources}"
    )


def test_not_found_page_does_not_suggest_a_hosted_transcription_service() -> None:
    page = _parse()
    assert page.forms == 0, "the 404 page must not offer a form"
    assert page.file_inputs == 0, "the 404 page must not offer a file upload"


def test_deployment_notes_record_the_pending_post_deploy_not_found_check() -> None:
    notes = DEPLOYMENT.read_text(encoding="utf-8")
    assert "404.html" in notes, "the deployment notes do not mention the 404 page"
    assert "404" in notes, "the deployment notes do not mention the 404 status"
    assert re.search(r"pending|until|after the next deployment|next deployment", notes,
                     re.IGNORECASE), (
        "the deployment notes must mark the live 404 check as pending until a deployment"
    )
