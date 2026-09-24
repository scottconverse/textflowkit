"""A tag must not publish while the README's primary release line is stale.

PyPI builds a release's long description from the README *at the tagged commit*
and freezes it: v0.1.5's PyPI page still says "v0.1.4 release" and cannot be
rewritten in place (see the erratum in `docs/user-manual.md`). The existing
`tests/test_release_surfaces.py` check is a bare substring test, so it stays
green while another README line names the new version and the displayed
"Current release" line and its link still point at the old tag. This guard reads
that one line as a claim with a target and refuses to build unless both name the
version being published.

The runtime behavior lives in `scripts/verify_readme_release.py`; the workflow
wiring tests at the bottom pin the step that makes it effective.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import verify_readme_release
from textflowkit import __version__

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PUBLISH = (ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
GUARD_SCRIPT = "scripts/verify_readme_release.py"
UPLOAD_ACTION = "pypa/gh-action-pypi-publish"

VERSION = "0.1.6"
OTHER_VERSION = "0.1.5"
TAG_URL = "https://github.com/scottconverse/textflowkit/releases/tag/v{version}"


def _status(label: str, href: str) -> str:
    """The primary release line, in the shape the README documents."""
    return f"**Current release: [{label}]({href}).**"


def _page(status: str | None, *extra: str) -> str:
    lines = ["# textflowkit", "", "Cross-platform media transcription toolkit.", ""]
    if status is not None:
        lines += [status, ""]
    lines += ["## What it does", "", *extra, ""]
    return "\n".join(lines)


def _current_status(version: str) -> str:
    return _status(f"v{version}", TAG_URL.format(version=version))


# --- the guard's contract -----------------------------------------------------


def test_accepts_a_claim_and_target_that_name_the_release_being_published() -> None:
    verify_readme_release.verify_readme_release(_page(_current_status(VERSION)), VERSION)


def test_accepts_the_release_tag_as_well_as_the_bare_version() -> None:
    page = _page(_current_status(VERSION))
    verify_readme_release.verify_readme_release(page, VERSION)
    verify_readme_release.verify_readme_release(page, f"v{VERSION}")


def test_rejects_a_stale_displayed_tag_even_when_another_line_names_the_release() -> None:
    """The hole this guard closes: the line the reader sees is not checked.

    This is the measured shape of the v0.1.5 mistake — one README line was
    updated, the displayed status line was not — so a bare substring test still
    passes on exactly this page.
    """
    page = _page(
        _current_status(OTHER_VERSION),
        f"**v{VERSION} release.** Core, CLI, MCP, and HTTP have automated checks.",
    )
    assert f"v{VERSION}" in page, "the substring check in test_release_surfaces.py would be green"
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert f"v{OTHER_VERSION}" in str(excinfo.value)


def test_rejects_a_stale_target_url_while_the_claim_names_the_release() -> None:
    page = _page(_status(f"v{VERSION}", TAG_URL.format(version=OTHER_VERSION)))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert TAG_URL.format(version=OTHER_VERSION) in str(excinfo.value)


def test_rejects_a_claim_for_a_newer_release_than_the_tag() -> None:
    page = _page(_status("v0.1.7", TAG_URL.format(version="0.1.7")))
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(page, VERSION)


def test_rejects_a_page_whose_primary_status_line_is_missing() -> None:
    """A page that names the release elsewhere is still missing the claim."""
    page = _page(None, f"**v{VERSION} release.** Everything is wired.")
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "Current release" in str(excinfo.value)


def test_rejects_two_primary_status_lines() -> None:
    page = _page(_current_status(VERSION), _current_status(OTHER_VERSION))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "2" in str(excinfo.value)


@pytest.mark.parametrize("status", [
    f"Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)}).",  # not bold
    f"**Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)})**",  # no period
    f"**Current release: v{VERSION}**",  # no link
    "**Current release: [v0.1.6]().**",  # empty target
    "**Current release: [v0.1.6](releases/tag/v0.1.6).**",  # relative target
    f"**Current release [v{VERSION}]({TAG_URL.format(version=VERSION)}).**",  # no colon
    f"**Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)}).** and more",  # trailing text
])
def test_rejects_a_malformed_primary_status_line(status: str) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(_page(status), VERSION)


def test_rejects_a_primary_status_line_with_no_version_in_its_target() -> None:
    page = _page(_status(f"v{VERSION}", "https://github.com/scottconverse/textflowkit/releases"))
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(page, VERSION)


@pytest.mark.parametrize("version", ["", "v", "0.1.6 ", "0.1.6)"])
def test_rejects_a_version_that_cannot_appear_in_a_release_url(version: str) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(_page(_current_status(VERSION)), version)


# --- reading the file ---------------------------------------------------------


def test_reads_the_shipped_readme_at_its_declared_version() -> None:
    """The repository itself must pass the guard at the version it declares."""
    verify_readme_release.check_readme_release(README, __version__)


def test_fails_closed_when_the_readme_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.check_readme_release(tmp_path / "absent-README.md", VERSION)


def test_fails_closed_when_the_readme_is_not_utf8(tmp_path: Path) -> None:
    page = tmp_path / "README.md"
    page.write_bytes(b"# textflowkit\n\n\xff\xfe not utf-8\n")
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.check_readme_release(page, VERSION)


# --- command line -------------------------------------------------------------


def _run(tmp_path: Path, page: str, argv: list[str], capsys):
    path = tmp_path / "README.md"
    path.write_text(page, encoding="utf-8", newline="\n")
    return verify_readme_release.main(["--readme", str(path), *argv]), capsys.readouterr()


def test_main_accepts_a_matching_page(tmp_path: Path, capsys) -> None:
    code, out = _run(tmp_path, _page(_current_status(VERSION)), ["--version", VERSION], capsys)
    assert code == 0
    assert VERSION in out.out
    assert out.err == ""


def test_main_reports_the_reason_and_fails(tmp_path: Path, capsys) -> None:
    code, out = _run(tmp_path, _page(_current_status(OTHER_VERSION)),
                     ["--version", VERSION], capsys)
    assert code == 1
    assert "README release guard failed" in out.err
    assert out.out == ""


def test_main_reads_the_release_tag_from_the_environment(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("RELEASE_TAG", f"v{VERSION}")
    code, out = _run(tmp_path, _page(_current_status(VERSION)), [], capsys)
    assert code == 0
    assert out.err == ""


def test_main_fails_closed_when_no_version_is_named(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    code, out = _run(tmp_path, _page(_current_status(VERSION)), [], capsys)
    assert code == 1
    assert "RELEASE_TAG" in out.err


# --- workflow wiring ----------------------------------------------------------


def _job(text: str, name: str) -> str:
    """Return the text of a top-level job block, without trailing siblings."""
    lines = text.splitlines()
    header = f"  {name}:"
    assert header in lines, f"no {name} job in the workflow"
    start = lines.index(header)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[index]):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def _steps(job_block: str) -> list[str]:
    marker = "\n      - "
    assert marker in job_block, "job has no steps"
    return ["      - " + chunk for chunk in job_block.split(marker)[1:]]


def test_the_build_job_runs_the_readme_guard_before_it_builds_anything() -> None:
    build = _job(PUBLISH, "build")
    assert GUARD_SCRIPT in build
    assert build.index(GUARD_SCRIPT) < build.index("Build distributions")


def test_only_the_tag_workflow_runs_the_guard() -> None:
    """Normal PR CI legitimately names the last public release, so it must not gate.

    Until a new version is staged the README's status line points at the last
    tag that was published; requiring a matching tag on every pull request would
    fail every run of the ordinary test matrix.
    """
    assert GUARD_SCRIPT not in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_every_upload_job_depends_on_the_guard_that_the_build_job_runs() -> None:
    build = _job(PUBLISH, "build")
    for job in ("publish-fonts", "publish-main"):
        assert UPLOAD_ACTION in _job(PUBLISH, job), job
    assert "needs: build" in _job(PUBLISH, "publish-fonts")
    assert "needs: publish-fonts" in _job(PUBLISH, "publish-main")
    assert build.index(GUARD_SCRIPT) < PUBLISH.index(UPLOAD_ACTION)


def test_the_guard_step_takes_the_tag_from_the_environment() -> None:
    steps = [step for step in _steps(_job(PUBLISH, "build")) if GUARD_SCRIPT in step]
    assert len(steps) == 1, "expected exactly one README guard step"
    step = steps[0]
    assert "RELEASE_TAG: ${{ github.ref_name }}" in step
    run_lines = [line for line in step.splitlines() if line.strip().startswith("run:")]
    assert run_lines
    assert all("token" not in line.lower() for line in run_lines)


# --- documentation ------------------------------------------------------------


def test_the_checklist_names_the_readme_guard_and_keeps_the_erratum_honest() -> None:
    checklist = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
    section = checklist[checklist.index("## PyPI publication"):]
    assert "verify_readme_release.py" in section
    assert "Current release" in section
    # The uploaded v0.1.5 long description cannot be repaired, and the guard has
    # never run against a live tag.
    assert "cannot be rewritten" in section or "immutable" in section
