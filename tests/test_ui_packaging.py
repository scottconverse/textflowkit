"""The UI ships its workspace: packaging, entry point, and static assets.

An installed ``textflowkit-ui`` must serve a working shell, which means the
``ui/static`` folder must be in the built **wheel**, and the console script must
be declared. The wheel is built once and inspected. Two failure modes are kept
distinct: a genuinely **absent build tool** skips (the guarantee cannot be
checked here), while a **real build error** - or a build that reports success but
produces no wheel - fails loudly rather than being skipped, so a broken build
cannot hide behind a green skip.
"""

from __future__ import annotations

import glob
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _wheel() -> Path | None:
    """Build a wheel into a temp dir outside the repo and return its path.

    Skips only when the *build tool* is absent (nothing to check with). A build
    that fails, or that succeeds but yields no wheel, raises - that is a real
    packaging regression, not a reason to skip.
    """
    try:
        import build  # noqa: F401
        import hatchling  # noqa: F401
    except ImportError:
        return None
    out = Path(tempfile.mkdtemp(prefix="tfk-ui-wheel-"))
    proc = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (
        f"wheel build failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
    )
    wheels = sorted(glob.glob(str(out / "*.whl")))
    assert wheels, f"wheel build reported success but produced no .whl in {out}"
    return Path(wheels[0])


@pytest.fixture(scope="module")
def wheel_names() -> list[str]:
    path = _wheel()
    if path is None:
        pytest.skip("build/hatchling not installed")
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def test_wheel_carries_ui_static_assets(wheel_names):
    for asset in ("textflowkit/ui/static/index.html",
                  "textflowkit/ui/static/app.css",
                  "textflowkit/ui/static/app.js"):
        assert asset in wheel_names, f"{asset} missing from the wheel"


def test_wheel_carries_ui_modules(wheel_names):
    for module in ("textflowkit/ui/app.py", "textflowkit/ui/launcher.py",
                   "textflowkit/ui/security.py", "textflowkit/ui/uploads.py",
                   "textflowkit/ui/capabilities.py", "textflowkit/ui/paths.py"):
        assert module in wheel_names, f"{module} missing from the wheel"


def _project_version() -> str | None:
    """The ``version`` from the ``[project]`` table of ``pyproject.toml``.

    A deliberately **narrow, dependency-free** reader: it scans for the
    ``[project]`` table header and then the first ``version = "..."`` after it,
    which is all these tests assert. It is not a general TOML parser (the project
    requires none, and :mod:`tomllib` is stdlib only from 3.11), so the supported
    Python floor - 3.10 - is exercised rather than skipped.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "[project]")
    except StopIteration:
        return None
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped.startswith("[") and stripped != "[project]":
            break  # the [project] table ended
        match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
        if match:
            return match.group(1)
    return None


def _script_mapping(name: str) -> str | None:
    """The ``[project.scripts]`` value for ``name``, or ``None``.

    Same narrow, dependency-free approach as :func:`_project_version`: the
    ``[project.scripts]`` table is scanned for the one ``name = "target"`` line.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines) if ln.strip() == "[project.scripts]"
        )
    except StopIteration:
        return None
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped.startswith("["):
            break  # the table ended
        match = re.match(r'([A-Za-z0-9_.\-]+)\s*=\s*"([^"]+)"', stripped)
        if match and match.group(1) == name:
            return match.group(2)
    return None


def test_entry_point_declared():
    assert _script_mapping("textflowkit-ui") == "textflowkit.ui.launcher:main"


def test_declared_version_matches_runtime_version():
    """The UI ships inside the core package, so the two versions must agree.

    The project version lives in two places that a release can drift apart:
    ``pyproject.toml``'s ``[project].version`` (what PyPI, the wheel filename,
    and the release tag resolve) and ``textflowkit.__version__`` (what the UI
    runtime labels itself with, via the launcher's ``--version`` and the
    ``/ui/capabilities`` payload). They must be equal. This asserts *equality*,
    not a literal like ``"0.1.9"``: pinning a number would have to be edited on
    every future release (and would fail the moment the version moved), whereas
    the coupling between the declared and runtime versions is the invariant that
    actually protects the product.
    """
    from textflowkit import __version__

    assert _project_version() == __version__
    assert __version__  # never an empty string that would vacuously "match"


def test_static_assets_have_no_build_step():
    """The UI is plain HTML/CSS/JS: no bundler config, no node_modules.

    The live-streaming example and its AudioWorklet are the same kind of asset -
    hand-written HTML and a plain JS module the browser loads directly - so they
    are expected here, not a build output.
    """
    static = ROOT / "src" / "textflowkit" / "ui" / "static"
    files = {p.name for p in static.iterdir()}
    assert files == {
        "index.html",
        "app.css",
        "app.js",
        "streaming-example.html",
        "streaming-capture-worklet.js",
    }
    for stray in ("package.json", "webpack.config.js", "vite.config.js", "tsconfig.json"):
        assert not (static / stray).exists()
