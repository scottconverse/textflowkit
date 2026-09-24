"""Commands shown in the docs must spell the option the parser declares.

DOC-01 (minor): the install guide's diarization walkthrough ran

    textflowkit transcribe clip.wav --diarize --format json

``transcribe`` declares ``--formats``; it has no ``--format`` of its own
(``--stdout-format`` is a different option). The example only ran because
argparse accepts an unambiguous prefix of a long option, so it would break the
day a second ``--format...`` option is added to ``transcribe``. The example is
not wrong today - it is fragile, which is what the review retracted its failure
claim to.

The rule below is per-subcommand and derived from the parser itself, because
``export`` really does declare ``--format``: the ``export`` examples in the user
manual and README are correct as written and must not be flagged.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from textflowkit import cli as cli_mod

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "docs/install.md"
USER_MANUAL = ROOT / "docs/user-manual.md"

FENCE_RE = re.compile(
    r"^```(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*\r?\n(?P<body>.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)

# An invoked example line, with or without a shell prompt.
COMMAND_RE = re.compile(r"^(?:\$ |PS> )?textflowkit[ \t]+(?P<command>[a-z][a-z-]*)\b")

# A long option shaped like the format selector, including `--formats` itself.
# ``(?<![\w-])`` keeps ``--stdout-format`` from matching as ``-format``.
FORMAT_OPTION_RE = re.compile(r"(?<![\w-])--format\w*")


def _section(path: Path, title: str) -> str:
    """Text from the heading ``title`` to the next heading of the same or higher level."""
    text = path.read_text(encoding="utf-8")
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
    raise AssertionError(f"heading {title!r} not found in {path.name}")


def _command_lines(section: str) -> list[str]:
    """Every line inside a fenced block that invokes the CLI."""
    lines: list[str] = []
    for block in FENCE_RE.finditer(section):
        for raw in block.group("body").splitlines():
            line = raw.strip()
            if COMMAND_RE.match(line):
                lines.append(line)
    return lines


def _declared_long_options(command: str) -> set[str]:
    """Long option strings the parser declares for ``command``."""
    parser = cli_mod._build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    child = subparsers.choices[command]
    return {
        option
        for action in child._actions
        for option in action.option_strings
        if option.startswith("--")
    }


def _format_option_offenders(lines: list[str]) -> list[str]:
    offenders: list[str] = []
    for line in lines:
        command = COMMAND_RE.match(line).group("command")
        declared = _declared_long_options(command)
        for option in FORMAT_OPTION_RE.findall(line):
            if option not in declared:
                offenders.append(
                    f"{option!r} in {line!r} is not an option {command!r} declares "
                    f"({', '.join(sorted(declared))})"
                )
    return offenders


@pytest.fixture(scope="module")
def install_diarization() -> str:
    return _section(INSTALL, "Diarization")


def test_install_diarization_example_spells_the_declared_format_option(install_diarization):
    """The walkthrough must not lean on argparse's long-option abbreviation."""
    lines = _command_lines(install_diarization)
    assert any(FORMAT_OPTION_RE.search(line) for line in lines), (
        "the diarization walkthrough no longer shows a format option, so this check "
        f"would be vacuous; commands found: {lines}"
    )
    assert not _format_option_offenders(lines), _format_option_offenders(lines)


def test_transcribe_declares_formats_and_not_a_bare_format_option():
    """Why the example has to say ``--formats``: there is nothing else to abbreviate to."""
    declared = _declared_long_options("transcribe")
    assert "--formats" in declared
    assert "--format" not in declared


def test_export_declares_format_so_its_examples_stay_valid():
    """``export`` is the one command whose format selector is ``--format``."""
    assert "--format" in _declared_long_options("export")


def test_the_rule_leaves_the_export_examples_alone():
    """The per-command rule must not turn into a blanket ban on ``--format``."""
    lines = [
        line
        for line in _command_lines(_section(USER_MANUAL, "4. Export or inspect a saved transcript"))
        if "--format" in line
    ]
    assert lines, "the user manual no longer shows an export --format example"
    assert not _format_option_offenders(lines), _format_option_offenders(lines)
