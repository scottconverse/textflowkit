"""The first-run Hugging Face token instructions have to be executable as written.

DOC-02: the diarization pages set ``HF_TOKEN`` with ``setx`` (a persistent Windows
command that does **not** reach the process already running) and, on the adapters
page, set the token *after* the command that consumes it. A newcomer following
either page got a run with no token.

These tests read the fenced examples in the diarization sections rather than line
numbers, so the prose can move without breaking them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "docs/install.md"
ADAPTERS = ROOT / "docs/adapters.md"

FENCE_RE = re.compile(
    r"^```(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*\r?\n(?P<body>.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)

# A fenced example that needs the token: either the three-repo verification snippet
# or the diarized transcription itself.
USE_MARKERS = ("hf_hub_download", "--diarize")

POWERSHELL_ASSIGN = re.compile(r"\$env:HF_TOKEN\s*=")
# ``(?<!:)`` keeps ``$env:HF_TOKEN =`` from counting as a POSIX assignment.
POSIX_ASSIGN = re.compile(r"(?<!:)\bHF_TOKEN\s*=")

TOKEN_LITERAL = re.compile(r"hf_(?!hub_)[A-Za-z0-9_.-]+")
PLACEHOLDER_TOKEN = re.compile(r"^hf_(?:x{4,}|\.{3,})$", re.IGNORECASE)


class _Block(NamedTuple):
    lang: str
    body: str
    start: int


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


def _blocks(section: str) -> list[_Block]:
    return [
        _Block(lang=m.group("lang").lower(), body=m.group("body"), start=m.start())
        for m in FENCE_RE.finditer(section)
    ]


def _use_offset(body: str) -> int | None:
    offsets = [body.find(marker) for marker in USE_MARKERS if marker in body]
    return min(offsets) if offsets else None


def _assert_assignment_precedes_use(section: str, assignment, form: str) -> None:
    """``form`` must be set before the first fenced example that needs the token."""
    blocks = _blocks(section)
    uses = [(i, _use_offset(b.body)) for i, b in enumerate(blocks)]
    uses = [(i, off) for i, off in uses if off is not None]
    assert uses, (
        "no fenced example in this section runs a diarized transcription or "
        "verifies the token, so the ordering rule cannot be checked"
    )
    first_use, _ = min(uses)

    for index, block in enumerate(blocks):
        match = assignment.search(block.body)
        if match is None:
            continue
        if index < first_use:
            return
        # A command-prefixed assignment lives in the same block as its use; it
        # still has to come first inside that block.
        if index == first_use and match.start() < _use_offset(block.body):
            return

    raise AssertionError(
        f"the diarization section never sets the token in the {form} form before "
        f"the first example that consumes HF_TOKEN (block {first_use})"
    )


@pytest.fixture(scope="module")
def install_diarization() -> str:
    return _section(INSTALL.read_text(encoding="utf-8"), "Diarization")


@pytest.fixture(scope="module")
def adapters_speaker_labels() -> str:
    return _section(ADAPTERS.read_text(encoding="utf-8"), "Speaker labels")


def test_install_sets_the_token_for_the_current_powershell_process(install_diarization):
    """``setx`` only reaches shells started later; the guide must set this one."""
    _assert_assignment_precedes_use(
        install_diarization, POWERSHELL_ASSIGN, "current-process PowerShell ($env:HF_TOKEN)"
    )
    blocks = [b for b in _blocks(install_diarization) if "$env:HF_TOKEN" in b.body]
    assert any(b.lang in {"powershell", "ps1", "pwsh"} for b in blocks), (
        "the PowerShell assignment must sit in a PowerShell-labelled fence, not one "
        f"labelled {[b.lang for b in blocks]!r}"
    )


def test_install_sets_the_token_for_posix_shells(install_diarization):
    _assert_assignment_precedes_use(install_diarization, POSIX_ASSIGN, "POSIX (export HF_TOKEN=)")


def test_install_keeps_setx_out_of_the_runnable_examples(install_diarization):
    fenced = [b for b in _blocks(install_diarization) if "setx" in b.body]
    assert not fenced, (
        "setx is a persistent, new-shells-only command; it must not be presented as "
        "the step that makes the next command work"
    )
    if "setx" in install_diarization:
        assert re.search(r"new shells", install_diarization, re.IGNORECASE), (
            "if persistent Windows setup is mentioned at all, it has to say it applies "
            "to new shells only"
        )


def test_adapters_sets_the_token_before_invoking_diarization(adapters_speaker_labels):
    _assert_assignment_precedes_use(
        adapters_speaker_labels, POSIX_ASSIGN, "POSIX (export HF_TOKEN=)"
    )


def test_adapters_sets_the_token_for_the_current_powershell_process(adapters_speaker_labels):
    _assert_assignment_precedes_use(
        adapters_speaker_labels,
        POWERSHELL_ASSIGN,
        "current-process PowerShell ($env:HF_TOKEN)",
    )


@pytest.mark.parametrize("path", [INSTALL, ADAPTERS], ids=["install", "adapters"])
def test_docs_never_carry_a_real_looking_token(path):
    literals = {m.group(0) for m in TOKEN_LITERAL.finditer(path.read_text(encoding="utf-8"))}
    assert all(PLACEHOLDER_TOKEN.match(literal) for literal in literals), sorted(literals)
