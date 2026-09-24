"""The publish workflow must reuse a published fonts release, not rebuild it.

`tests/test_fetch_published_fonts.py` pins the behavior of the resolution script
and `tests/test_verify_release_versions.py` pins the version contract. This file
pins the wiring that makes them release gates: the build job resolves the fonts
mode before it builds anything, only a *new* fonts version is built and
uploaded, a reused one is never offered to PyPI again - PyPI rejects a duplicate
filename, and this workflow deliberately does not skip it - and both versions
reach the manifest that is attached to the release.

The workflow text is parsed line by line instead of with a YAML library: PyYAML
is not a dependency of the published package or of the `dev` test extra, so a
parser import could fail on a CI matrix leg that has no diarization stack.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = (ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
CHECKLIST = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
FETCH_SCRIPT = "scripts/fetch_published_fonts.py"
VERSIONS_SCRIPT = "scripts/verify_release_versions.py"
MANIFEST_SCRIPT = "scripts/write_release_manifest.py"
MANIFEST = "SHA256SUMS"
UPLOAD_ACTION = "pypa/gh-action-pypi-publish"
FONTS_PACKAGE = "packages/textflowkit-fonts"
FONTS_PYPROJECT = "packages/textflowkit-fonts/pyproject.toml"
MODE_OUTPUT = "steps.fonts_release.outputs.fonts_mode"


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
    """Split a job block into its step texts."""
    marker = "\n      - "
    assert marker in job_block, "job has no steps"
    return ["      - " + chunk for chunk in job_block.split(marker)[1:]]


def _single_step(job_block: str, needle: str) -> str:
    steps = [step for step in _steps(job_block) if needle in step]
    assert len(steps) == 1, f"expected exactly one step mentioning {needle}"
    return steps[0]


def _branch(step: str, label: str) -> str:
    """Return the body of one `case` branch, without its `;;` terminator."""
    lines = step.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == f"{label})"), None
    )
    assert start is not None, f"the build step has no {label}) branch"
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == ";;":
            return "\n".join(lines[start + 1:index])
    raise AssertionError(f"the {label}) branch has no ;; terminator")


# --- the build job resolves the fonts mode ------------------------------------


def test_the_build_job_resolves_the_fonts_release_before_it_builds_anything() -> None:
    build = _job(PUBLISH, "build")

    assert build.index(FETCH_SCRIPT) < build.index("Build distributions")


def test_the_version_gate_and_the_readme_guard_run_before_the_fonts_resolution() -> None:
    """The cheap local gates come first; only then does the job read the index."""
    build = _job(PUBLISH, "build")

    assert build.index(VERSIONS_SCRIPT) < build.index(FETCH_SCRIPT)
    assert build.index("scripts/verify_readme_release.py") < build.index(FETCH_SCRIPT)
    assert build.index("scripts/verify_release_ci.py") < build.index(FETCH_SCRIPT)


def test_the_fonts_resolution_reads_the_declared_package_and_reports_the_mode() -> None:
    step = _single_step(_job(PUBLISH, "build"), FETCH_SCRIPT)

    assert f"--package-dir {FONTS_PACKAGE}" in step
    assert "--out-dir dist/fonts" in step
    assert '--github-output "$GITHUB_OUTPUT"' in step
    assert "id: fonts_release" in step


def test_the_build_job_publishes_the_fonts_mode_to_the_upload_jobs() -> None:
    build = _job(PUBLISH, "build")

    assert "outputs:" in build
    assert f"fonts_mode: ${{{{ {MODE_OUTPUT} }}}}" in build


def test_the_version_gate_takes_the_tag_from_the_environment() -> None:
    """The tag is the release's name; it must come from the ref, not a literal."""
    step = _single_step(_job(PUBLISH, "build"), VERSIONS_SCRIPT)

    assert "RELEASE_TAG: ${{ github.ref_name }}" in step


# --- only a new fonts version is built and uploaded ---------------------------


def test_a_new_fonts_version_is_the_only_case_that_builds_the_fonts_package() -> None:
    step = _single_step(_job(PUBLISH, "build"), "Build distributions")

    assert "FONTS_MODE" in step
    assert FONTS_PACKAGE in _branch(step, "new")


def test_a_reused_fonts_release_is_never_rebuilt() -> None:
    """The verified files were downloaded into `dist/fonts`; a build would replace them."""
    step = _single_step(_job(PUBLISH, "build"), "Build distributions")
    reuse = _branch(step, "reuse")

    assert "python -m build" not in reuse
    assert "dist/fonts" in reuse


def test_an_unresolved_fonts_mode_stops_the_build() -> None:
    """A missing or unexpected mode must fail the job, not publish either way."""
    step = _single_step(_job(PUBLISH, "build"), "Build distributions")
    fallback = _branch(step, "*")

    assert "exit 1" in fallback


def test_a_reused_fonts_release_is_not_offered_to_pypi_again() -> None:
    """PyPI rejects a duplicate filename; the fonts upload must not run at all."""
    fonts = _job(PUBLISH, "publish-fonts")

    assert f"if: needs.build.outputs.fonts_mode == 'new'" in fonts


def test_the_fonts_upload_job_keeps_its_approval_and_trusted_publishing() -> None:
    fonts = _job(PUBLISH, "publish-fonts")

    assert UPLOAD_ACTION in fonts
    assert "environment: pypi" in fonts
    assert "id-token: write" in fonts
    assert "packages-dir: dist/fonts/" in fonts


def test_the_upload_order_stays_fonts_then_core_then_the_github_release() -> None:
    assert "needs: build" in _job(PUBLISH, "publish-fonts")
    assert "needs: publish-fonts" in _job(PUBLISH, "publish-main")
    assert "needs: publish-main" in _job(PUBLISH, "publish-github-release")


def test_the_workflow_never_skips_an_existing_pypi_filename() -> None:
    """`skip-existing` would hide a duplicate upload instead of surfacing it."""
    assert "skip-existing" not in PUBLISH
    assert "skip_existing" not in PUBLISH


# --- both versions reach the manifest and the release -------------------------


def test_the_manifest_is_pinned_to_the_tag_and_to_the_declared_fonts_version() -> None:
    job = _job(PUBLISH, "publish-github-release")
    step = _single_step(job, MANIFEST_SCRIPT)

    assert '--version "$RELEASE_TAG"' in step
    assert '--fonts-version "$FONTS_VERSION"' in step
    assert "FONTS_VERSION: ${{ steps.fonts_version.outputs.version }}" in step
    # The fonts version is read from the tagged commit, never inferred from the tag.
    assert FONTS_PYPROJECT in job


def test_the_release_attaches_the_same_files_the_manifest_hashed() -> None:
    job = _job(PUBLISH, "publish-github-release")
    manifest = _single_step(job, MANIFEST_SCRIPT)
    create = _single_step(job, "gh release create")

    for artifact in ("dist/main/*", "dist/fonts/*"):
        assert artifact in manifest, artifact
        assert artifact in create, artifact
    assert MANIFEST in manifest and MANIFEST in create
    assert "--draft" in create
    assert "gh release edit" in job


# --- documentation ------------------------------------------------------------


def test_the_checklist_describes_the_conditional_fonts_release() -> None:
    section = CHECKLIST[CHECKLIST.index("## PyPI publication"):]

    assert FETCH_SCRIPT in section
    assert FONTS_PYPROJECT in section
    assert "reuse" in section
