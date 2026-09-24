"""The landing page's first-run commands have to run in CMD, PowerShell and POSIX sh.

UX-01: the site advertises native Windows, but its two command areas showed POSIX-only
syntax. The hero continued its command with a backslash, which CMD hands to the program
as a literal ``\\`` argument and which PowerShell ends the statement at (the next line
then fails to parse as ``--formats``). The quickstart quoted the pip extra with single
quotes, which CMD passes through verbatim, so pip rejects
``'textflowkit[mcp,http]'`` as an invalid requirement.

These tests read the command text out of ``docs/index.html`` itself, not out of a copy of
it, and they take the split between command and decoration from the page's own
stylesheet, matched against each block's real ancestors: an element the page declares
``user-select: none`` is decoration (a prompt glyph, a comment, sample output) and the
remaining text is what a reader copies into a shell. Everything a reader can select has
to be a command that CMD, PowerShell and POSIX sh all accept unchanged.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

import pytest

PAGE = Path(__file__).resolve().parents[1] / "docs" / "index.html"
SOURCE = PAGE.read_text(encoding="utf-8")

STYLE = re.search(r"<style>(?P<css>.*?)</style>", SOURCE, re.DOTALL)
assert STYLE is not None, "the page must keep its inline stylesheet"

RULE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<declarations>[^{}]*)\}")
USER_SELECT_NONE = re.compile(r"(?<![-\w])user-select\s*:\s*none")
CLASS = re.compile(r"\.([A-Za-z][\w-]*)")

# https://html.spec.whatwg.org/multipage/syntax.html#void-elements
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})

# The hero example and the quickstart example, as the page presents them.
TRANSCRIBE = re.compile(r"^textflowkit transcribe \S+ --formats [\w,]+$")
PIP_INSTALL = re.compile(r'^python -m pip install "(?P<requirement>textflowkit\[[^"\]]+\])"$')

# Syntax that only one of CMD, PowerShell and POSIX sh accepts. None of it may appear in
# text a reader can select out of a code block.
SHELL_HOSTILE = {
    "POSIX line continuation": re.compile(r"\\$"),
    "single-quoted argument": re.compile(r"'"),
    "prompt or shell-variable glyph": re.compile(r"\$"),
    "PowerShell escape character": re.compile(r"`"),
    "CMD prompt glyph": re.compile(r"C:\\>"),
}


class Rule(NamedTuple):
    """One `user-select: none` selector: its ancestor compounds and its subject classes."""

    ancestors: tuple[frozenset[str], ...]
    subjects: frozenset[str]


class Element(NamedTuple):
    """An element inside a code block, with the classes of the elements that contain it."""

    classes: frozenset[str]
    ancestors: list[frozenset[str]]


def _rules() -> list[Rule]:
    rules: list[Rule] = []
    for block in RULE.finditer(STYLE.group("css")):
        if not USER_SELECT_NONE.search(block.group("declarations")):
            continue
        for selector in block.group("selectors").split(","):
            compounds = selector.split()
            if not compounds:
                continue
            subjects = frozenset(CLASS.findall(compounds[-1]))
            if subjects:
                rules.append(
                    Rule(
                        ancestors=tuple(frozenset(CLASS.findall(c)) for c in compounds[:-1]),
                        subjects=subjects,
                    )
                )
    return rules


def _applies(rule: Rule, element: Element) -> bool:
    """Descendant match: every class-bearing ancestor compound must match, outermost first."""
    if not rule.subjects & element.classes:
        return False
    ancestors = [classes for classes in element.ancestors if classes]
    index = 0
    for needed in rule.ancestors:
        if not needed:
            continue
        while index < len(ancestors) and not needed <= ancestors[index]:
            index += 1
        if index == len(ancestors):
            return False
        index += 1
    return True


class CodeBlocks(HTMLParser):
    """Copyable text of every ``<pre>``, plus every element shown inside one."""

    def __init__(self, rules: list[Rule]) -> None:
        super().__init__()
        self.rules = rules
        self.blocks: list[str] = []
        self.elements: list[Element] = []
        self._buf: list[str] = []
        self._open: list[tuple[str, frozenset[str]]] = []
        self._in_pre = 0
        self._muted = 0
        self._mute_stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = frozenset((dict(attrs).get("class") or "").split())
        if tag == "pre":
            self._in_pre += 1
            self._buf = []
        elif self._in_pre:
            element = Element(classes, [known for _, known in self._open])
            self.elements.append(element)
            muted = any(_applies(rule, element) for rule in self.rules)
            self._mute_stack.append(muted)
            self._muted += muted
        if tag not in VOID_ELEMENTS:
            self._open.append((tag, classes))

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_pre -= 1
            self.blocks.append("".join(self._buf))
        elif self._in_pre and self._mute_stack:
            self._muted -= self._mute_stack.pop()
        if tag not in VOID_ELEMENTS and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._in_pre and not self._muted:
            self._buf.append(data)


@pytest.fixture(scope="module")
def page() -> CodeBlocks:
    parser = CodeBlocks(_rules())
    parser.feed(SOURCE)
    return parser


@pytest.fixture(scope="module")
def commands(page: CodeBlocks) -> list[str]:
    return [line for block in page.blocks for line in block.split("\n") if line.strip()]


def test_page_declares_every_code_block_element_unselectable(page: CodeBlocks) -> None:
    """A code block may show prompts, comments and output, but none of it may be copied."""
    assert page.elements, "the expected prompt/comment/output elements are missing"
    selectable = sorted(
        " ".join(sorted(element.classes))
        for element in page.elements
        if not any(_applies(rule, element) for rule in page.rules)
    )
    assert not selectable, (
        f"elements inside a <pre> with classes {selectable} are selectable, so they are "
        "copied along with the command; declare them 'user-select: none' or remove them"
    )


def test_the_page_still_shows_its_first_run_commands(commands: list[str]) -> None:
    """Guard against a rewrite that leaves nothing to copy."""
    assert "textflowkit doctor" in commands
    assert "textflowkit-mcp" in commands
    assert any(line.startswith("python -m pip install ") for line in commands), commands
    assert any(line.startswith("textflowkit transcribe ") for line in commands), commands


def test_copyable_command_text_is_accepted_unchanged_by_all_three_shells(
    commands: list[str],
) -> None:
    assert commands, "no copyable command text found in the landing page"
    for line in commands:
        for name, pattern in SHELL_HOSTILE.items():
            found = pattern.search(line)
            assert not found, (
                f"{name} at {found.group(0)!r} in copyable command text {line!r}; CMD, "
                "PowerShell and POSIX sh do not all accept it"
            )


def test_hero_transcription_command_is_a_single_portable_line(commands: list[str]) -> None:
    """No continuation: CMD and PowerShell both end the statement at the newline."""
    matched = [line for line in commands if TRANSCRIBE.match(line)]
    assert len(matched) == 1, (
        "the hero example must be one line that CMD, PowerShell and POSIX sh all accept, "
        f"found {matched}"
    )


def test_quickstart_pip_requirement_is_double_quoted(commands: list[str]) -> None:
    """Double quotes are the one form CMD, PowerShell and POSIX sh all strip to the same argv."""
    installs = [line for line in commands if line.startswith("python -m pip install")]
    assert len(installs) == 1, installs
    match = PIP_INSTALL.match(installs[0])
    assert match is not None, (
        f"the pip requirement in {installs[0]!r} is not double-quoted; CMD passes single "
        "quotes to pip verbatim"
    )
    assert match.group("requirement") == "textflowkit[mcp,http]"
