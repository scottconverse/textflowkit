"""A local sdist ships the project, not whatever is lying around the checkout.

Outside-review D3: hatchling's sdist target selects every file the VCS ignore
files do not exclude, so an ordinary `python -m build --sdist` on a maintainer
machine copied the reviewer's `.v` environment folder into
`textflowkit-0.1.5.tar.gz`. `.gitignore` is a blocklist and cannot be trusted to
name every stray file, so the target carries an explicit include allowlist.

These tests build a real sdist from a copy of this checkout with untracked
debris planted in it, then pin both halves of the promise: the debris stays out
of the archive, and everything an sdist build/install or a contributor needs
stays in.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from textflowkit import __version__

ROOT = Path(__file__).resolve().parents[1]

# Untracked local state planted in the copy, one entry per kind the brief names.
# `.gitignore` does not name the first two, which is exactly why hatchling's
# default "everything the ignore files do not exclude" rule published them - the
# `.v` folder is the shape the reviewer reported. The last four `.gitignore`
# does name, so they were safe before and must stay safe now that the rule is an
# allowlist rather than a blocklist.
DEBRIS = (
    ".v/sentinel.txt",            # scratch env folder, not named by .gitignore
    "stray-local-notes.txt",      # loose stray file, not named by .gitignore
    "out/scrap.txt",              # build output
    ".env",                       # local credentials
    ".venv/marker.txt",           # local environment
    ".pytest_cache/marker.txt",   # cache
)

# Not copied. A `.git` directory would make the copy a second repository, and
# the maintainer's own environment, build output, and caches must not decide the
# outcome; the debris kinds above are planted fresh in the copy instead, so the
# assertions are about the configuration rather than about this machine.
COPY_SKIP = (
    ".git", ".venv", "venv", "__pycache__", "build", "dist", "out", "outputs",
    "work", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".env",
)

# Everything an sdist install/build or a contributor's test run needs. This is
# the brief's list - application, tests, docs, scripts, and the top-level
# user/legal/contribution documents - named explicitly so a future allowlist
# edit cannot quietly drop one of them.
REQUIRED = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "LEGAL.md",
    "SECURITY.md",
    "src/textflowkit/__init__.py",
    "src/textflowkit/cli.py",
    "src/textflowkit/core/pipeline.py",
    "src/textflowkit/render/pdf.py",
    "src/textflowkit/adapters/mcp_server.py",
    "src/textflowkit/assets/selftest-speech.wav",
    "tests/__init__.py",
    "tests/ollama_stub.py",
    "tests/test_cli.py",
    "docs/install.md",
    "docs/.nojekyll",
    "scripts/smoke_installed_wheel.py",
    "scripts/smoke_live_youtube.py",
    "scripts/verify_release_ci.py",
    "scripts/write_release_manifest.py",
)

FONTS_PREFIX = "packages/textflowkit-fonts/"


@dataclass(frozen=True, slots=True)
class BuiltSdist:
    """A real sdist built from a debris-planted copy of this checkout."""

    members: frozenset[str]
    wheel: Path
    target: Path


def _build(*arguments: str) -> None:
    """Run the same builder the release workflow uses, without build isolation.

    Isolation would fetch the backend from PyPI; the test must stay offline and
    deterministic, so `dev` declares `build` and `hatchling` and the installed
    pair is used directly.
    """
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", *arguments],
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, f"build failed\n{result.stdout}\n{result.stderr}"


def _only(paths: object) -> Path:
    found = sorted(paths)
    assert len(found) == 1, found
    return found[0]


def _archive_members(archive: Path) -> set[str]:
    """The sdist's files, relative to its single top-level directory."""
    with tarfile.open(archive) as tar:
        names = [name for name in tar.getnames() if not name.endswith("/")]
    prefixes = {name.split("/", 1)[0] for name in names}
    assert len(prefixes) == 1, prefixes
    prefix = f"{prefixes.pop()}/"
    return {name[len(prefix):] for name in names}


def _tracked_files() -> set[str]:
    """This checkout's tracked files, or a skip when it is not the repository root.

    An unpacked sdist or a checkout nested in another repository has no usable
    `ls-files` answer for this root, and a comparison against the wrong list
    would fail for a reason that has nothing to do with the packaging config.
    """
    top = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT:
        pytest.skip(f"not the root of a git checkout: {top.stdout.strip() or top.stderr.strip()}")
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    return {path for path in result.stdout.split("\0") if path}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> BuiltSdist:
    workspace = tmp_path_factory.mktemp("sdist-allowlist")
    copy = workspace / "checkout"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(*COPY_SKIP))
    for relative in DEBRIS:
        planted = copy / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("local debris that must never be published\n", encoding="utf-8")
        assert planted.is_file(), f"fixture failed to plant {relative}"

    distributions = workspace / "dist"
    _build("--sdist", "--outdir", str(distributions), str(copy))
    archive = _only(distributions.glob("textflowkit-*.tar.gz"))

    wheels = workspace / "wheel"
    _build("--wheel", "--outdir", str(wheels), str(archive))
    wheel = _only(wheels.glob("textflowkit-*.whl"))

    target = workspace / "installed"
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--no-deps", "--no-index", "--target", str(target), str(wheel)],
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert install.returncode == 0, f"{install.stdout}\n{install.stderr}"
    return BuiltSdist(members=frozenset(_archive_members(archive)), wheel=wheel, target=target)


def test_untracked_local_debris_never_reaches_the_archive(built: BuiltSdist) -> None:
    """D3: `.gitignore` does not name these, so the ignore-everything default shipped them."""
    leaked = sorted(path for path in DEBRIS if path in built.members)
    assert not leaked, f"untracked local debris was published in the sdist: {leaked}"


def test_the_archive_still_carries_everything_an_sdist_needs(built: BuiltSdist) -> None:
    missing = sorted(path for path in REQUIRED if path not in built.members)
    assert not missing, f"files dropped from the sdist: {missing}"


def test_the_fonts_companion_package_is_not_folded_into_the_core_archive(
    built: BuiltSdist,
) -> None:
    """It is published from its own directory by its own `python -m build`."""
    folded = sorted(name for name in built.members if name.startswith("packages/"))
    assert not folded, f"core sdist absorbed the fonts package: {folded}"


def test_the_archive_matches_a_clean_checkout_exactly(built: BuiltSdist) -> None:
    """The release workflow builds from a clean checkout; that set must not change.

    Every tracked file except the separately published fonts package is in the
    archive, and the archive invents nothing. This is the stronger half of the
    guarantee: the allowlist drops the debris without dropping a single tracked
    file. `.github/` and the repository-level dotfiles are included deliberately
    - the release-workflow and manifest tests read them from the checkout root.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required to enumerate the tracked checkout")
    expected = {path for path in _tracked_files() if not path.startswith(FONTS_PREFIX)}
    expected.add("PKG-INFO")  # generated by the builder, not a file in the checkout
    published_but_untracked = sorted(built.members - expected)
    tracked_but_unpublished = sorted(expected - built.members)
    assert not published_but_untracked and not tracked_but_unpublished, (
        f"published but not in the tracked checkout: {published_but_untracked}; "
        f"tracked but not published: {tracked_but_unpublished}"
    )


def test_a_wheel_rebuilt_from_the_archive_installs_and_imports(built: BuiltSdist) -> None:
    with zipfile.ZipFile(built.wheel) as wheel:
        names = set(wheel.namelist())
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points) == 1, entry_points
        declared = wheel.read(entry_points[0]).decode("utf-8")
    assert "textflowkit/__init__.py" in names
    assert "textflowkit/assets/selftest-speech.wav" in names
    assert "textflowkit = textflowkit.cli:main" in declared

    # `-ES` drops site-packages and PYTHONPATH, so the only importable
    # `textflowkit` is the one that came out of the rebuilt archive.
    probe = subprocess.run(
        [sys.executable, "-ES", "-c",
         ("import sys; sys.path.insert(0, sys.argv[1]); import textflowkit; "
          "print(textflowkit.__version__, textflowkit.__file__, sep='\\n')"),
         str(built.target)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"
    installed_version, location = probe.stdout.splitlines()
    assert installed_version == __version__
    assert Path(location).is_relative_to(built.target), location
