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

POWERSHELL_LANGS = {"powershell", "ps1", "pwsh"}
FORMS = {
    "ps": (POWERSHELL_ASSIGN, "current-process PowerShell ($env:HF_TOKEN = 'hf_xxxxxxxx')"),
    "posix": (POSIX_ASSIGN, "POSIX (export HF_TOKEN=..., or the VAR=value command prefix)"),
}

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


def _required_form(lang: str) -> str:
    return "ps" if lang in POWERSHELL_LANGS else "posix"


def _assert_examples_set_the_token_first(section: str, form: str) -> None:
    """Each example that needs the token must set it, in its own shell's syntax.

    Coverage comes from an assignment in an earlier example, or from one inside
    the example ahead of the command (the ``VAR=value command`` prefix).
    """
    assignment, name = FORMS[form]
    checked = 0
    assigned_earlier = False
    for index, block in enumerate(_blocks(section)):
        match = assignment.search(block.body)
        offset = _use_offset(block.body)
        if offset is not None and _required_form(block.lang) == form:
            checked += 1
            inside = match is not None and match.start() < offset
            if not (assigned_earlier or inside):
                raise AssertionError(
                    f"the fenced example in block {index} consumes HF_TOKEN without the "
                    f"token set in the {name} form before it"
                )
        assigned_earlier = assigned_earlier or match is not None
    assert checked, (
        f"no {name} example in this section consumes HF_TOKEN, so the ordering rule "
        "cannot be checked"
    )


def _assert_both_shells_cover_the_first_use(section: str) -> None:
    """Install is one linear walkthrough: set the token in your shell *before* step one."""
    blocks = _blocks(section)
    first = next((i for i, b in enumerate(blocks) if _use_offset(b.body) is not None), None)
    assert first is not None, (
        "no fenced example in this section runs a diarized transcription or verifies "
        "the token, so the ordering rule cannot be checked"
    )
    for form in ("ps", "posix"):
        assignment, name = FORMS[form]
        assert any(assignment.search(block.body) for block in blocks[:first]), (
            f"the first example that consumes HF_TOKEN (block {first}) is not preceded by "
            f"a token assignment in the {name} form"
        )


@pytest.fixture(scope="module")
def install_diarization() -> str:
    return _section(INSTALL.read_text(encoding="utf-8"), "Diarization")


@pytest.fixture(scope="module")
def adapters_speaker_labels() -> str:
    return _section(ADAPTERS.read_text(encoding="utf-8"), "Speaker labels")


def test_install_sets_the_token_for_the_current_powershell_process(install_diarization):
    """``setx`` only reaches shells started later; the guide must set this one first."""
    _assert_both_shells_cover_the_first_use(install_diarization)
    blocks = [b for b in _blocks(install_diarization) if "$env:HF_TOKEN" in b.body]
    assert any(b.lang in POWERSHELL_LANGS for b in blocks), (
        "the PowerShell assignment must sit in a PowerShell-labelled fence, not one "
        f"labelled {[b.lang for b in blocks]!r}"
    )


def test_install_sets_the_token_for_posix_shells(install_diarization):
    _assert_examples_set_the_token_first(install_diarization, "posix")


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
    _assert_examples_set_the_token_first(adapters_speaker_labels, "posix")


def test_adapters_sets_the_token_for_the_current_powershell_process(adapters_speaker_labels):
    _assert_examples_set_the_token_first(adapters_speaker_labels, "ps")
    blocks = [b for b in _blocks(adapters_speaker_labels) if "$env:HF_TOKEN" in b.body]
    assert any(b.lang in POWERSHELL_LANGS for b in blocks), (
        "the PowerShell assignment must sit in a PowerShell-labelled fence, not one "
        f"labelled {[b.lang for b in blocks]!r}"
    )


@pytest.mark.parametrize("path", [INSTALL, ADAPTERS], ids=["install", "adapters"])
def test_docs_never_carry_a_real_looking_token(path):
    literals = {m.group(0) for m in TOKEN_LITERAL.finditer(path.read_text(encoding="utf-8"))}
    assert all(PLACEHOLDER_TOKEN.match(literal) for literal in literals), sorted(literals)
