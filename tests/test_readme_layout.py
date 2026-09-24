"""The README's first screen must explain the product before it disclaims it.

Outside review B5: the README opened with the NO WARRANTY box (`README.md:22`),
above "What it does" and "Install". The PyPI project page renders this README as
the package's long description, so a first-time reader - there and on GitHub -
met a disclaimer before learning what the package does or how to run it.

The fix is layout, not law: keep a one-line description, a short Quickstart with
the install command and a local-file example, and move the existing warranty box
unchanged to just before `## License`. The warranty's wording is covered
elsewhere (`tests/test_legal_summary.py` reads `LEGAL.md` and `LICENSE`); these
tests only require that it survives the move, so they name short stable markers
rather than restating the disclaimer text they protect.

Order, not line numbers: every check below locates a section through its heading
and compares positions, so prose can grow without breaking the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

TEXT = README.read_text(encoding="utf-8")
LINES = TEXT.splitlines()

# ``## Install`` and friends, but not ``> ## NO WARRANTY`` inside the blockquote:
# a heading marker only counts at the start of a line.
HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.+?)[ \t]*$")
# A fence line: ```` ``` ```` or ```` ```bash ````. Shell comments inside a code
# block start with `#`, so the heading scan has to skip fenced code or it reads
# `# transcribe a URL or a local file` as a section and truncates `## Usage`.
FENCE = re.compile(r"^\s*(?:```|~~~)")
# The warranty box's own heading, one level down inside a blockquote.
WARRANTY_HEADING = re.compile(
    r"^\s*>\s*#{1,6}\s+(?P<title>.*\bNO WARRANTY\b.*)$", re.IGNORECASE
)
# A line of the blockquote that carries the box.
QUOTE = re.compile(r"^\s*>")
RULE = re.compile(r"^\s*[-*_]{3,}\s*$")

# What a one-line product explanation may call the thing it does. Deliberately a
# family of words: the sentence may be reworded, it may not go away.
PRODUCT_MARKERS = ("transcription", "transcribe", "transcript", "subtitle", "speech-to-text")
# The plain install the rest of the project documents (docs/install.md:15).
PLAIN_INSTALL = "python -m pip install textflowkit"
# ``textflowkit transcribe <source>``, with the source as the first argument.
TRANSCRIBE = re.compile(r"^textflowkit[ \t]+transcribe[ \t]+(?P<source>\S+)")


def _headings() -> list[tuple[int, str]]:
    """Line index and title of every Markdown heading outside fenced code."""
    headings: list[tuple[int, str]] = []
    fenced = False
    for index, line in enumerate(LINES):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and (match := HEADING.match(line)):
            headings.append((index, match.group("title")))
    return headings


def _section(title: str) -> list[str]:
    """The body lines of the one `## title` section (heading through its body)."""
    found = [index for index, name in _headings() if name == title]
    assert len(found) == 1, f"expected one '## {title}' section, found {len(found)}"
    start = found[0]
    end = len(LINES)
    for index, _ in _headings():
        if index > start:
            end = index
            break
    return LINES[start:end]


def _index(pattern: re.Pattern[str]) -> int:
    """The one line index matching `pattern`; a duplicate or an absence is a defect."""
    found = [index for index, line in enumerate(LINES) if pattern.match(line)]
    assert len(found) == 1, f"expected one line matching {pattern.pattern!r}, found {len(found)}"
    return found[0]


def _warranty_lines() -> list[str]:
    """The contiguous blockquote lines of the one NO WARRANTY box."""
    start = _index(WARRANTY_HEADING)
    end = start
    while end + 1 < len(LINES) and QUOTE.match(LINES[end + 1]):
        end += 1
    return LINES[start : end + 1]


def _first_body_line() -> str:
    """The first line of prose after the `# textflowkit` title."""
    title = _headings()[0]
    assert title[1] == "textflowkit", f"the first heading is {title[1]!r}, not the title"
    for line in LINES[title[0] + 1 :]:
        stripped = line.strip()
        if stripped and not HEADING.match(line) and not QUOTE.match(line) and not RULE.match(line):
            return stripped
    raise AssertionError("the README has no prose after its title")


def _local_sources(section: list[str]) -> list[str]:
    """The sources of the section's ``textflowkit transcribe`` examples, unquoted."""
    return [
        match.group("source").strip("\"'")
        for line in section
        if (match := TRANSCRIBE.match(line.strip()))
    ]


# --- what a reader meets first -------------------------------------------------


def test_a_one_line_product_explanation_opens_the_readme() -> None:
    """The reader learns what the package is before the warranty box."""
    line = _first_body_line()
    assert len(line) <= 200, f"the opening line is not one line of prose: {line!r}"
    assert any(marker in line.lower() for marker in PRODUCT_MARKERS), (
        f"the first line after the title does not say what the product does: {line!r}"
    )
    assert line != "", "empty opening line"


def test_a_quickstart_comes_before_the_warranty() -> None:
    section = _section("Quickstart")
    assert _index(WARRANTY_HEADING) > LINES.index(section[0]), (
        "the NO WARRANTY box still precedes the Quickstart"
    )


def test_the_quickstart_states_the_prerequisites() -> None:
    body = "\n".join(_section("Quickstart"))
    assert "Python" in body and "3.10" in body, (
        "the Quickstart does not state the Python version requirement"
    )
    assert "ffmpeg" in body, "the Quickstart does not state the ffmpeg requirement"


def test_the_quickstart_shows_the_plain_install_command() -> None:
    commands = [line.strip() for line in _section("Quickstart")]
    assert PLAIN_INSTALL in commands, (
        f"the Quickstart does not show the documented plain install {PLAIN_INSTALL!r}; "
        "reuse the command the install guide verifies rather than inventing one"
    )


def test_the_quickstart_example_transcribes_a_local_file_on_both_shells() -> None:
    """A local file, not a URL: no platform's availability is promised here."""
    sources = _local_sources(_section("Quickstart"))
    assert sources, "the Quickstart has no 'textflowkit transcribe' example"
    assert all(not source.startswith(("http://", "https://")) for source in sources), (
        f"the Quickstart example fetches a URL, which may be unavailable: {sources}"
    )
    windows = [source for source in sources if source.startswith(".\\")]
    posix = [source for source in sources if source.startswith("./")]
    assert windows, f"the Quickstart has no Windows-style example: {sources}"
    assert posix, f"the Quickstart has no POSIX-style example: {sources}"


# --- where the warranty box sits ----------------------------------------------


def test_the_warranty_sits_between_the_quickstart_and_the_license() -> None:
    warranty = _index(WARRANTY_HEADING)
    quickstart = LINES.index(_section("Quickstart")[0])
    license_index = LINES.index(_section("License")[0])
    assert quickstart < warranty, "the warranty box is still above the Quickstart"
    assert warranty < license_index, "the warranty box no longer precedes '## License'"


def test_nothing_but_a_rule_stands_between_the_warranty_and_the_license() -> None:
    """`## License` follows the box immediately, so the two cannot be separated."""
    block = _warranty_lines()
    start = LINES.index(block[0])
    license_index = LINES.index(_section("License")[0])
    between = LINES[start + len(block) : license_index]
    stray = [line for line in between if line.strip() and not RULE.match(line)]
    assert len(stray) == 0, (
        f"{len(stray)} line(s) separate the warranty box from '## License'; "
        f"first is {stray[0][:60]!r}"
    )


def test_the_warranty_box_keeps_its_disclaimer_and_responsibility_notice() -> None:
    """Moved, not weakened: the AS IS disclaimer and the third-party notice stay."""
    block = "\n".join(_warranty_lines())
    assert '"AS IS"' in block, "the AS IS disclaimer is gone from the warranty box"
    assert "WITHOUT WARRANTY OF ANY KIND" in block, (
        "the express warranty disclaimer is gone from the warranty box"
    )
    assert re.search(r"\[LICENSE\]\([^)]*LICENSE\)", block), (
        "the warranty box no longer links LICENSE"
    )
    assert "LEGAL.md" in block, "the warranty box no longer links LEGAL.md"
    assert re.search(r"\bresponsible\b", block, re.IGNORECASE), (
        "the third-party-media responsibility notice is gone from the warranty box"
    )


# --- what the move must not cost ----------------------------------------------


def test_the_detailed_install_and_usage_sections_survive() -> None:
    """The compact Quickstart is an addition; the reference material stays."""
    install = "\n".join(_section("Install"))
    assert "textflowkit[mcp]" in install, "the Install section lost the extras"
    assert "ffmpeg" in install, "the Install section lost the requirement note"

    usage = _local_sources(_section("Usage"))
    assert any(source.startswith(("http://", "https://")) for source in usage), (
        "the Usage section lost its URL example"
    )
    assert any(source.startswith("./") for source in usage), (
        "the Usage section lost its local-file example"
    )
