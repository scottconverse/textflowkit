"""Do not publish a new version while public documentation still names the old one."""

from __future__ import annotations

import re
from pathlib import Path

from textflowkit import __version__

ROOT = Path(__file__).resolve().parents[1]


def _declared_version(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', source, re.MULTILINE)
    assert match is not None, f"missing version in {path}"
    return match.group(1)


def test_release_version_matches_both_packages_and_current_public_surfaces() -> None:
    version = __version__
    assert _declared_version(ROOT / "pyproject.toml") == version
    assert _declared_version(ROOT / "packages/textflowkit-fonts/pyproject.toml") == version

    for path in (
        ROOT / "README.md",
        ROOT / "docs/index.html",
        ROOT / "docs/install.md",
        ROOT / "docs/adapters.md",
        ROOT / "docs/user-manual.md",
    ):
        assert f"v{version}" in path.read_text(encoding="utf-8"), path

    roadmap = (ROOT / "docs/roadmap.md").read_text(encoding="utf-8")
    assert f"- [x] Ship v{version} review follow-ups" in roadmap
