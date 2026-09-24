"""The plain-language liability summary has to match the license it summarizes.

LEG-01: ``LEGAL.md`` summarized Apache-2.0 §8 as an unconditional rule — "in no
event shall any contributor be liable" — and §7 in the same absolute voice, while
the shipped ``LICENSE`` opens both sections with an express carve-out ("Unless
required by applicable law or agreed to in writing" / "unless required by
applicable law ... or agreed to in writing").

These tests read the exceptions out of the shipped ``LICENSE`` rather than
hard-coding them, then require each summary bullet to carry the same qualifier.
They locate the bullets through their ``§7``/``§8`` labels, not line numbers, so
the prose can move without breaking them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "LEGAL.md"
LICENSE = ROOT / "LICENSE"

# ``   7. Disclaimer of Warranty. ...`` — the numbered sections of the license text.
LICENSE_SECTION_RE = re.compile(r"^ {2,6}(?P<number>\d{1,2})\.\s", re.MULTILINE)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
# A summary bullet labelled with its license section, e.g. ``- **§8 — Limitation ...``.
SUMMARY_BULLET_RE = re.compile(
    r"^[ \t]*-[ \t]+\*\*§(?P<number>\d).*?(?=\n[ \t]*-[ \t]|\n[ \t]*\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Both carve-outs the license states, with room for plain-English rewording.
LAW_EXCEPTION_RE = re.compile(r"\b(?:applicable|the) law\b|\bby law\b", re.IGNORECASE)
WRITING_EXCEPTION_RE = re.compile(
    r"\bagree(?:d|ment)\b[^.\n]{0,40}\bin writing\b|\bwritten agreement\b",
    re.IGNORECASE,
)


def _license_sections(text: str) -> dict[int, str]:
    """The numbered sections of the license, keyed by number, in file order."""
    matches = list(LICENSE_SECTION_RE.finditer(text))
    assert matches, "no numbered sections found in the license text"
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[int(match.group("number"))] = text[match.start():end]
    return sections


def _section(text: str, title: str) -> str:
    """Text from the heading ``title`` to the next heading of the same or higher level."""
    for match in HEADING_RE.finditer(text):
        if match.group("title") != title:
            continue
        level = len(match.group("hashes"))
        end = len(text)
        for later in HEADING_RE.finditer(text, match.end()):
            if len(later.group("hashes")) <= level:
                end = later.start()
                break
        return text[match.end():end]
    raise AssertionError(f"heading {title!r} not found")


@pytest.fixture(scope="module")
def license_text() -> str:
    return LICENSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def license_sections(license_text: str) -> dict[int, str]:
    return _license_sections(license_text)


@pytest.fixture(scope="module")
def no_warranty_section() -> str:
    return _section(LEGAL.read_text(encoding="utf-8"), "No warranty")


@pytest.mark.parametrize("number", [7, 8])
def test_shipped_license_states_the_law_and_written_agreement_exceptions(
    license_sections, number
):
    """Anchor: the exceptions the summary must mirror really are in the shipped license."""
    section = license_sections[number]
    assert LAW_EXCEPTION_RE.search(section), (
        f"LICENSE §{number} no longer mentions an applicable-law exception; the summary "
        "rule in this file is anchored on the shipped text and needs revisiting"
    )
    assert WRITING_EXCEPTION_RE.search(section), (
        f"LICENSE §{number} no longer mentions an agreement in writing; the summary rule "
        "in this file is anchored on the shipped text and needs revisiting"
    )


@pytest.mark.parametrize("number", [7, 8], ids=["section-7", "section-8"])
def test_no_warranty_summary_carries_the_license_exceptions(no_warranty_section, number):
    """Each summary bullet must keep the carve-out its license section states."""
    bullets = {int(m.group("number")): m.group(0) for m in SUMMARY_BULLET_RE.finditer(no_warranty_section)}
    assert number in bullets, (
        f"the 'No warranty' section no longer summarizes §{number}; labels found: "
        f"{sorted(bullets)}"
    )
    bullet = bullets[number]
    assert LAW_EXCEPTION_RE.search(bullet), (
        f"the §{number} summary is unqualified: it omits the license's "
        "'unless required by applicable law' exception.\nSummary was: " + " ".join(bullet.split())
    )
    assert WRITING_EXCEPTION_RE.search(bullet), (
        f"the §{number} summary is unqualified: it omits the license's "
        "'or agreed to in writing' exception.\nSummary was: " + " ".join(bullet.split())
    )


def test_no_warranty_section_points_at_the_license_for_controlling_language(no_warranty_section):
    """A summary of a license has to say the license itself governs."""
    assert re.search(r"\bLICENSE\b", no_warranty_section), (
        "the 'No warranty' section does not refer the reader to the LICENSE file for "
        "the controlling terms"
    )


def test_no_warranty_section_keeps_the_accuracy_and_human_review_caution(no_warranty_section):
    """The summary's own warnings must survive the correction, whatever its wording."""
    assert re.search(r"warrant\w*\s+of\s+accuracy", no_warranty_section, re.IGNORECASE), (
        "the 'No warranty' section no longer disclaims a warranty of accuracy"
    )
    assert re.search(r"human review", no_warranty_section, re.IGNORECASE), (
        "the 'No warranty' section no longer asks for human review before relying on output"
    )
