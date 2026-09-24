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
from types import SimpleNamespace

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


def _needs(job_block: str) -> list[str]:
    """The job ids in the job's `needs:`, in order, for a scalar or a list."""
    match = re.search(r"^    needs: (?P<value>.+)$", job_block, re.MULTILINE)
    assert match, "the job declares no needs"
    value = match.group("value").strip()
    assert value and not value.startswith("#"), value
    if value.startswith("["):
        assert value.endswith("]"), value
        value = value[1:-1]
    return [item.strip() for item in value.split(",") if item.strip()]


def _condition(job_block: str) -> str:
    """The job's `if:` expression, with runs of whitespace collapsed.

    The expression may be a one-line scalar or a folded (`>-`) block spanning
    several lines; the continuation lines are joined before the `${{ ... }}`
    body is taken, so both spellings are read the same way.
    """
    lines = job_block.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("    if:")), None)
    assert start is not None, "the job declares no if condition"
    body = [lines[start].split("if:", 1)[1]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("      "):
            break
        body.append(line)
    match = re.search(r"\$\{\{.*?\}\}", re.sub(r"\s+", " ", " ".join(body)))
    assert match, f"the job's if is not an expression: {' '.join(body).strip()!r}"
    return match.group(0)


def _python_condition(condition: str) -> str:
    """Translate a workflow `if` expression into an equivalent Python one.

    GitHub's expression language is close enough to Python for the boolean
    structure to be checked by translating the two operators and the one status
    check function used here, then evaluating the result. Job ids may contain a
    hyphen (`needs.publish-fonts.result`), which Python cannot dereference, so
    they are underscored. The translated text is asserted to be nothing but
    boolean expression syntax before it reaches `eval`.
    """
    body = condition.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    body = body[3:-2].strip()
    body = body.replace("&&", " and ").replace("||", " or ")
    body = body.replace("!cancelled()", "not cancelled")
    body = body.replace("publish-fonts", "publish_fonts")
    assert re.fullmatch(r"[A-Za-z0-9_ .()<>=!']+", body), body
    return body


def _runs(condition: str, *, build: str, fonts: str, mode: str, cancelled: bool) -> bool:
    """Would a job with this `if` run, given those prerequisite results?

    `build` and `fonts` are the `needs.<job>.result` values GitHub reports
    (`success`, `failure`, `skipped`, `cancelled`); `mode` is the build job's
    `fonts_mode` output. This evaluates the job's own condition only: the
    platform's skip propagation is the reason the condition exists.
    """
    needs = SimpleNamespace(
        build=SimpleNamespace(result=build, outputs=SimpleNamespace(fonts_mode=mode)),
        publish_fonts=SimpleNamespace(result=fonts),
    )
    return bool(
        eval(_python_condition(condition), {"__builtins__": {}},
             {"needs": needs, "cancelled": cancelled})
    )


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

    assert "if: needs.build.outputs.fonts_mode == 'new'" in fonts


def test_the_fonts_upload_job_keeps_its_approval_and_trusted_publishing() -> None:
    fonts = _job(PUBLISH, "publish-fonts")

    assert UPLOAD_ACTION in fonts
    assert "environment: pypi" in fonts
    assert "id-token: write" in fonts
    assert "packages-dir: dist/fonts/" in fonts


def test_the_upload_order_stays_fonts_then_core_then_the_github_release() -> None:
    """Core waits for the fonts upload, and the GitHub release waits for core.

    Core needs `build` as well as `publish-fonts`: a job whose `needs:` names a
    skipped job is skipped too, so the core upload has to wait on the fonts job
    by name *and* carry its own `if` (below), not inherit its result.
    """
    assert _needs(_job(PUBLISH, "publish-fonts")) == ["build"]
    assert _needs(_job(PUBLISH, "publish-main")) == ["build", "publish-fonts"]
    assert _needs(_job(PUBLISH, "publish-github-release")) == ["publish-main"]


# --- a skipped fonts upload must not skip the core upload ----------------------


def test_the_core_upload_survives_a_skipped_fonts_job_with_a_status_check() -> None:
    """GitHub skips a job whose `needs:` job was skipped unless the job's `if`
    uses a status check function; a bare `if:` is implicitly `success()`, which
    still loses to that propagation. `cancelled()` is the status check used, so
    the job runs on the reuse path (fonts uploaded nothing because nothing was
    built) while a cancelled run is still refused."""
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert "!cancelled()" in condition
    assert "needs.publish-fonts.result" in condition


def test_a_reused_fonts_release_still_publishes_the_core_distributions() -> None:
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert _runs(condition, build="success", fonts="skipped", mode="reuse", cancelled=False)


def test_a_new_fonts_upload_still_publishes_the_core_distributions() -> None:
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert _runs(condition, build="success", fonts="success", mode="new", cancelled=False)


def test_a_skipped_fonts_upload_is_only_tolerated_for_a_reused_release() -> None:
    """Skipping the fonts upload while a *new* fonts version was declared would
    publish core against a fonts version that never reached PyPI."""
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert not _runs(condition, build="success", fonts="skipped", mode="new", cancelled=False)
    assert not _runs(condition, build="success", fonts="skipped", mode="", cancelled=False)


def test_a_failed_fonts_upload_cannot_publish_the_core_distributions() -> None:
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert not _runs(condition, build="success", fonts="failure", mode="new", cancelled=False)
    assert not _runs(condition, build="success", fonts="failure", mode="reuse", cancelled=False)
    assert not _runs(condition, build="success", fonts="cancelled", mode="reuse", cancelled=False)


def test_a_failed_or_cancelled_run_cannot_publish_the_core_distributions() -> None:
    condition = _condition(_job(PUBLISH, "publish-main"))

    assert not _runs(condition, build="failure", fonts="success", mode="new", cancelled=False)
    assert not _runs(condition, build="skipped", fonts="success", mode="new", cancelled=False)
    assert not _runs(condition, build="success", fonts="success", mode="new", cancelled=True)


def test_the_core_upload_job_keeps_its_approval_and_trusted_publishing() -> None:
    main = _job(PUBLISH, "publish-main")

    assert UPLOAD_ACTION in main
    assert "environment: pypi" in main
    assert "id-token: write" in main
    assert "packages-dir: dist/main/" in main


def test_the_github_release_cannot_bypass_a_failed_core_upload() -> None:
    """No job-level `if` on purpose: the default gate propagates a failed or
    skipped `publish-main`, so a core upload that did not succeed cannot end in
    a public release."""
    release = _job(PUBLISH, "publish-github-release")

    assert not re.search(r"^    if:", release, re.MULTILINE), "a job-level if could bypass the gate"
    assert "environment: pypi" not in release


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
