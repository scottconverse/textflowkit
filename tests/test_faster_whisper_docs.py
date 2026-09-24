"""The opt-in faster-whisper claims must stay true, scoped, and shell-portable.

Audit U42 found two factual problems in the first pass of this documentation:

- It promised the extra "cannot disturb"/"cannot replace" an existing ROCm torch
  build. CTranslate2 has no torch dependency, but installing
  ``textflowkit[faster-whisper]`` resolves the *whole project*, base
  ``openai-whisper`` included, so an existing ROCm environment can be affected by
  resolution. Nobody measured a combined faster-whisper + ROCm install, so the
  guarantee is removed rather than reworded.
- The install snippet quoted the extra with single quotes, which CMD passes
  through literally. Double quotes work in PowerShell, CMD, and POSIX shells.

The tests below name short stable markers and scope themselves to the
faster-whisper section (or to the one requirement string), so unrelated ROCm
prose elsewhere in the same files is untouched by them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "docs" / "install.md"
USER_MANUAL = ROOT / "docs" / "user-manual.md"
PYPROJECT = ROOT / "pyproject.toml"

FASTER_HEADING = "## Optional CPU/Mac engine: faster-whisper"

#: The one spelling every shell accepts. Single quotes are literal in CMD.
QUOTED_FOR_EVERY_SHELL = '"textflowkit[faster-whisper]"'
QUOTED_FOR_POWERSHELL_ONLY = "'textflowkit[faster-whisper]'"

#: Phrases that assert an unmeasured guarantee about an existing ROCm install.
UNMEASURED_GUARANTEES = (
    "cannot disturb",
    "cannot replace",
    "will not disturb",
    "will not replace",
)


def _section(text: str, heading: str) -> str:
    """One ``## `` section body, up to the next ``## `` heading."""
    start = text.index(heading)
    body = text[start + len(heading):]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def _faster_section() -> str:
    return _section(INSTALL.read_text(encoding="utf-8"), FASTER_HEADING)


@pytest.mark.parametrize("path", [INSTALL, USER_MANUAL], ids=["install", "user-manual"])
def test_the_extra_is_quoted_so_cmd_installs_it(path: Path):
    text = path.read_text(encoding="utf-8")
    assert QUOTED_FOR_EVERY_SHELL in text
    assert QUOTED_FOR_POWERSHELL_ONLY not in text


@pytest.mark.parametrize("phrase", UNMEASURED_GUARANTEES)
def test_no_unmeasured_guarantee_about_an_existing_rocm_torch(phrase: str):
    assert phrase not in _faster_section().lower(), (
        "installing the extra re-resolves the whole project, base openai-whisper "
        "included; no combined faster-whisper + ROCm install was measured"
    )


def test_existing_rocm_users_are_told_how_to_keep_their_torch():
    """The honest replacement for the guarantee: point at the preserved install."""
    section = _faster_section()
    assert "--no-deps" in section


def test_the_missing_extra_is_refused_before_acquisition_on_every_surface():
    """U43 removed the CLI-only scope this test used to pin.

    The command line was the only surface that preflighted the extra; the engine
    then reached its error at load, after acquisition inside the pipeline. Every
    surface - command line, Python API, MCP, and HTTP - now refuses before any
    media is fetched, so the old wording is wrong rather than merely stale.
    """
    low = " ".join(_faster_section().lower().split())
    assert "before any media is fetched" in low
    for surface in ("command line", "python api", "mcp", "http"):
        assert surface in low, surface
    assert "engine load" not in low


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11")
def test_the_extra_floor_is_the_release_that_was_actually_verified():
    """The floor is the measured 1.2.1, not the whole 1.x line."""
    import tomllib

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    (requirement,) = project["optional-dependencies"]["faster-whisper"]
    assert requirement.startswith("faster-whisper>=1.2.1")
    assert "<2" in requirement


def test_the_extra_comment_makes_no_unmeasured_guarantee():
    text = PYPROJECT.read_text(encoding="utf-8").lower()
    for phrase in UNMEASURED_GUARANTEES:
        assert phrase not in text, phrase
