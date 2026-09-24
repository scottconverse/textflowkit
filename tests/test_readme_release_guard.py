"""A tag must not publish while the README still claims an older release.

PyPI builds a release's long description from the README *at the tagged commit*
and freezes it: v0.1.5's PyPI page still says "v0.1.4 release" and cannot be
rewritten in place (see the erratum in `docs/user-manual.md`).

That bad text came from the `## Status` section's first paragraph
(`git show v0.1.5:README.md`, line 247: `**v0.1.4 release.**`). The README also
carries a displayed current-release link, and the two are separate claims: the
first guard for this unit checked only the link, and the measured mutation -
Status moved back to `**v0.1.4 release.**` while the link stayed correct - was
accepted. Both claims are checked here.

`tests/test_release_surfaces.py` only asks whether `v{version}` appears
somewhere in the README, so it stays green on both mistakes. Every test below is
deterministic: synthetic pages and the repository's own files, never GitHub or
PyPI.
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


def _previous(version: str) -> str:
    """The version a stale claim would still name, one patch release behind."""
    major, minor, patch = version.split(".", 2)
    return f"{major}.{minor}.{int(patch) - 1}"


def _link(version: str) -> str:
    """The primary release link line, in the shape the README documents."""
    return f"**Current release: [v{version}]({TAG_URL.format(version=version)}).**"


def _claim(version: str) -> str:
    """The `## Status` opening claim, in the shape the README documents."""
    return f"**v{version} release.** Core, CLI, MCP, and HTTP have automated coverage."


GOOD_LINK = _link(VERSION)
GOOD_CLAIM = _claim(VERSION)


def _page(
    link: str | None = GOOD_LINK,
    claim: str | None = GOOD_CLAIM,
    extra: tuple[str, ...] = (),
    status_extra: tuple[str, ...] = (),
) -> str:
    """A README-shaped page; `None` omits that claim entirely."""
    lines = ["# textflowkit", "", "Cross-platform media transcription toolkit.", ""]
    if link is not None:
        lines += [link, ""]
    lines += ["## What it does", "", *extra, ""]
    if claim is not None:
        lines += ["## Status", "", claim, "", *status_extra, ""]
    lines += ["## License", "", "Apache-2.0.", ""]
    return "\n".join(lines)


# --- the Current release link -------------------------------------------------


def test_accepts_a_link_whose_claim_and_target_name_the_release_being_published() -> None:
    verify_readme_release.verify_readme_release(_page(), VERSION)


def test_accepts_the_release_tag_as_well_as_the_bare_version() -> None:
    page = _page()
    verify_readme_release.verify_readme_release(page, VERSION)
    verify_readme_release.verify_readme_release(page, f"v{VERSION}")


def test_rejects_a_stale_displayed_link_tag_even_when_another_line_names_the_release() -> None:
    """The link is not the only place a version is named, so a substring test misses it."""
    page = _page(
        link=_link(OTHER_VERSION),
        extra=(f"**v{VERSION} release.** Core, CLI, MCP, and HTTP have automated checks.",),
    )
    assert f"v{VERSION}" in page, "the substring check in test_release_surfaces.py would be green"
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert f"v{OTHER_VERSION}" in str(excinfo.value)


def test_rejects_a_stale_link_target_while_its_label_names_the_release() -> None:
    page = _page(link=f"**Current release: [v{VERSION}]({TAG_URL.format(version=OTHER_VERSION)}).**")
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert TAG_URL.format(version=OTHER_VERSION) in str(excinfo.value)


def test_rejects_a_link_claim_for_a_newer_release_than_the_tag() -> None:
    page = _page(link=_link("0.1.7"))
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(page, VERSION)


def test_rejects_a_page_whose_primary_release_link_is_missing() -> None:
    """A page that names the release elsewhere is still missing the link."""
    page = _page(link=None, extra=(f"**v{VERSION} release.** Everything is wired.",))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "Current release" in str(excinfo.value)


def test_rejects_two_primary_release_links() -> None:
    page = _page(link=GOOD_LINK, extra=(_link(OTHER_VERSION),))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "2" in str(excinfo.value)


@pytest.mark.parametrize("link", [
    f"Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)}).",  # not bold
    f"**Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)})**",  # no period
    f"**Current release: v{VERSION}**",  # no link
    "**Current release: [v0.1.6]().**",  # empty target
    "**Current release: [v0.1.6](releases/tag/v0.1.6).**",  # relative target
    f"**Current release [v{VERSION}]({TAG_URL.format(version=VERSION)}).**",  # no colon
    f"**Current release: [v{VERSION}]({TAG_URL.format(version=VERSION)}).** and more",  # trailing text
])
def test_rejects_a_malformed_primary_release_link(link: str) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(_page(link=link), VERSION)


def test_rejects_a_release_link_with_no_version_in_its_target() -> None:
    page = _page(link=f"**Current release: [v{VERSION}](https://github.com/scottconverse/textflowkit/releases).**")
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(page, VERSION)


@pytest.mark.parametrize("version", ["", "v", "0.1.6 ", "0.1.6)"])
def test_rejects_a_version_that_cannot_appear_in_a_release_url(version: str) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(_page(), version)


# --- the `## Status` claim ----------------------------------------------------


def test_accepts_a_status_claim_that_names_the_release_being_published() -> None:
    verify_readme_release.verify_readme_release(_page(claim=_claim(VERSION)), VERSION)


def test_rejects_a_stale_status_claim_while_the_release_link_is_correct() -> None:
    """The v0.1.5 defect exactly: the Status paragraph lagged, the link did not.

    The published v0.1.5 long description says `**v0.1.4 release.**` while its
    current-release link was the right one, so a guard that reads only the link
    accepts precisely the page that must never be published.
    """
    page = _page(link=GOOD_LINK, claim=_claim(OTHER_VERSION))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert f"v{OTHER_VERSION}" in str(excinfo.value)


def test_rejects_a_stale_status_claim_in_the_shipped_readme() -> None:
    """The coordinator's measured mutation, against the repository's own README.

    Only the `## Status` opening claim moves back one release; the current-release
    link and every other mention stay as they are.
    """
    page = README.read_text(encoding="utf-8")
    stale_claim = f"**v{_previous(__version__)} release.**"
    current_claim = f"**v{__version__} release.**"
    assert page.count(current_claim) == 1, f"expected one {current_claim!r} in the README"
    assert stale_claim not in page
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(
            page.replace(current_claim, stale_claim), __version__
        )
    assert f"v{_previous(__version__)}" in str(excinfo.value)


def test_rejects_a_page_with_no_status_section() -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(_page(claim=None), VERSION)
    assert "Status" in str(excinfo.value)


def test_rejects_two_status_sections() -> None:
    """Two sections make the displayed claim ambiguous, so neither can be trusted."""
    page = _page(claim=GOOD_CLAIM, status_extra=("## Status", "", _claim(VERSION), ""))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "Status" in str(excinfo.value)


def test_rejects_a_second_release_claim_inside_the_status_section() -> None:
    page = _page(claim=GOOD_CLAIM, status_extra=("Earlier notes:", "", _claim(OTHER_VERSION), ""))
    with pytest.raises(verify_readme_release.ReadmeReleaseError) as excinfo:
        verify_readme_release.verify_readme_release(page, VERSION)
    assert "2" in str(excinfo.value)


def test_rejects_a_status_claim_that_is_not_the_opening_paragraph() -> None:
    page = _page(claim=None, status_extra=("Some preamble text.", "", _claim(VERSION), ""))
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(page, VERSION)


@pytest.mark.parametrize("claim", [
    f"v{VERSION} release. Core, CLI, MCP, and HTTP are covered.",  # not bold
    f"**v{VERSION} release** Core, CLI, MCP, and HTTP are covered.",  # no period
    f"**release v{VERSION}.** Core, CLI, MCP, and HTTP are covered.",  # words reversed
    f"**v{VERSION}.** Core, CLI, MCP, and HTTP are covered.",  # no word 'release'
    f"**v{VERSION} patch release.** Core, CLI, MCP, and HTTP are covered.",  # extra word
])
def test_rejects_a_malformed_status_claim(claim: str) -> None:
    with pytest.raises(verify_readme_release.ReadmeReleaseError):
        verify_readme_release.verify_readme_release(_page(claim=claim), VERSION)


def test_accepts_bold_text_after_the_status_claim() -> None:
    """Only the opening claim is the claim; later emphasis in the section is prose."""
    page = _page(status_extra=("The **release** notes are generated; see the roadmap.", ""))
    verify_readme_release.verify_readme_release(page, VERSION)


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
    code, out = _run(tmp_path, _page(), ["--version", VERSION], capsys)
    assert code == 0
    assert VERSION in out.out
    assert out.err == ""


def test_main_reports_the_reason_and_fails(tmp_path: Path, capsys) -> None:
    code, out = _run(tmp_path, _page(link=_link(OTHER_VERSION)),
                     ["--version", VERSION], capsys)
    assert code == 1
    assert "README release guard failed" in out.err
    assert out.out == ""


def test_main_reports_a_stale_status_claim_and_fails(tmp_path: Path, capsys) -> None:
    code, out = _run(tmp_path, _page(claim=_claim(OTHER_VERSION)),
                     ["--version", VERSION], capsys)
    assert code == 1
    assert "README release guard failed" in out.err
    assert out.out == ""


def test_main_reads_the_release_tag_from_the_environment(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("RELEASE_TAG", f"v{VERSION}")
    code, out = _run(tmp_path, _page(), [], capsys)
    assert code == 0
    assert out.err == ""


def test_main_fails_closed_when_no_version_is_named(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    code, out = _run(tmp_path, _page(), [], capsys)
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

    Until a new version is staged the README's claims point at the last tag that
    was published; requiring a matching tag on every pull request would fail
    every run of the ordinary test matrix.
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


def test_the_checklist_names_both_claims_the_guard_checks() -> None:
    checklist = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
    section = checklist[checklist.index("## PyPI publication"):]
    assert "verify_readme_release.py" in section
    assert "Current release" in section
    assert "## Status" in section


def test_the_checklist_keeps_the_erratum_honest() -> None:
    """The uploaded v0.1.5 long description cannot be repaired, and the guard
    has never run against a live tag."""
    checklist = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
    section = checklist[checklist.index("## PyPI publication"):]
    assert "immutable" in section or "cannot be rewritten" in section
