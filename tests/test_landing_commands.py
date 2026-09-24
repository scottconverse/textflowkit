"""The landing page's first-run commands have to run in CMD, PowerShell and POSIX sh.

UX-01: the site advertises native Windows, but its two command areas showed POSIX-only
syntax. The hero continued its command with a backslash, which CMD hands to the program
as a literal ``\\`` argument and which PowerShell ends the statement at (the next line
then fails to parse as ``--formats``). The quickstart quoted the pip extra with single
quotes, which CMD passes through verbatim, so pip rejects
``'textflowkit[mcp,http]'`` as an invalid requirement.

These tests read the command text out of ``docs/index.html`` itself, not out of a copy of
it, and they take the split between command and decoration from the page's own
stylesheet: inside a code block, an element the page declares ``user-select: none`` is
decoration (a prompt glyph, a comment, sample output) and the remaining text is what a
reader copies into a shell. Everything a reader can select has to be a command that CMD,
PowerShell and POSIX sh all accept unchanged.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "docs" / "index.html"
SOURCE = PAGE.read_text(encoding="utf-8")

STYLE = re.search(r"<style>(?P<css>.*?)</style>", SOURCE, re.DOTALL)
assert STYLE is not None, "the page must keep its inline stylesheet"

RULE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<declarations>[^{}]*)\}")
USER_SELECT_NONE = re.compile(r"(?<![-\w])user-select\s*:\s*none")

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


def _non_selectable_classes() -> set[str]:
    """Classes the page's own stylesheet declares unselectable, so they cannot be copied."""
    found: set[str] = set()
    for rule in RULE.finditer(STYLE.group("css")):
        if USER_SELECT_NONE.search(rule.group("declarations")):
            for selector in rule.group("selectors").split(","):
                found.update(re.findall(r"\.([A-Za-z][\w-]*)", selector))
    return found


class CodeBlocks(HTMLParser):
    """Copyable text of every ``<pre>``: the text that is not declared decoration."""

    def __init__(self, decoration: set[str]) -> None:
        super().__init__()
        self.decoration = decoration
        self.blocks: list[str] = []
        self.classes: set[str] = set()
        self.decorated_classes: set[str] = set()
        self._buf: list[str] = []
        self._in_pre = 0
        self._muted = 0
        self._stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "pre":
            self._in_pre += 1
            self._buf = []
            return
        if not self._in_pre:
            return
        self.classes |= classes
        muted = bool(classes & self.decoration)
        if muted:
            self.decorated_classes |= classes
        self._stack.append(muted)
        self._muted += muted

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_pre -= 1
            self.blocks.append("".join(self._buf))
        elif self._stack:
            self._muted -= self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_pre and not self._muted:
            self._buf.append(data)


@pytest.fixture(scope="module")
def page() -> CodeBlocks:
    parser = CodeBlocks(_non_selectable_classes())
    parser.feed(SOURCE)
    return parser


@pytest.fixture(scope="module")
def commands(page: CodeBlocks) -> list[str]:
    return [line for block in page.blocks for line in block.split("\n") if line.strip()]


def test_page_declares_every_code_block_element_unselectable(page: CodeBlocks) -> None:
    """A code block may show prompts, comments and output, but none of it may be copied."""
    assert page.classes, "the expected prompt/comment/output elements are missing"
    undeclared = sorted(page.classes - _non_selectable_classes())
    assert not undeclared, (
        f"elements inside a <pre> with classes {undeclared} are selectable, so they are "
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
