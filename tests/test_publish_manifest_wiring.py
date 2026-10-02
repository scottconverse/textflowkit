"""The release workflow must attach a manifest of the artifacts it publishes.

`tests/test_release_manifest.py` pins what the manifest contains; this file pins
the wiring that makes it reach a downloader: it is generated from the
distributions downloaded for the GitHub release, after that download and before
the draft is created, it is attached to the draft alongside the exact four
distributions, and it never reaches PyPI.

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
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHECKLIST = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
MANIFEST = "SHA256SUMS"
MANIFEST_SCRIPT = "scripts/write_release_manifest.py"
DOWNLOAD_ACTION = "actions/download-artifact"
RELEASE_JOB = "publish-github-release"
# The manifest step is newer than the release the README links to, so a claim
# about it must be scoped forward and must say older releases have no asset.
FUTURE_MARKERS = ("next release", "future release", "Starting with")
ABSENCE_MARKERS = (
    "do not carry", "does not carry", "have no such asset", "no manifest",
    "no such asset", "predate", "before that change", "and earlier", "included,",
)
EARLIER_RELEASES_ATTACH = re.compile(r"each release\s+attaches", re.IGNORECASE)


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
    hyphen (`needs.publish-main.result`), which Python cannot dereference, so
    they are underscored. The translated text is asserted to be nothing but
    boolean expression syntax before it reaches `eval`.
    """
    body = condition.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    body = body[3:-2].strip()
    body = body.replace("&&", " and ").replace("||", " or ")
    body = body.replace("!cancelled()", "not cancelled")
    body = body.replace("publish-main", "publish_main")
    assert re.fullmatch(r"[A-Za-z0-9_ .()<>=!']+", body), body
    return body


def _release_runs(*, core: str, cancelled: bool) -> bool:
    """Would the release job run, given the core upload's result?

    `core` is the `needs.publish-main.result` value GitHub reports (`success`,
    `failure`, `skipped`, `cancelled`). This evaluates the job's own condition
    only: the platform's skip propagation is the reason the condition exists.
    """
    needs = SimpleNamespace(publish_main=SimpleNamespace(result=core))
    return bool(
        eval(_python_condition(_condition(_job(PUBLISH, RELEASE_JOB))),
             {"__builtins__": {}}, {"needs": needs, "cancelled": cancelled})
    )


def test_the_release_job_checks_out_the_manifest_script_it_runs() -> None:
    """The step runs the tagged commit's own script, so the job must have it.

    Without a checkout the job only holds the downloaded artifact and the
    manifest step fails with a missing file, leaving the release with no hashes.
    """
    job = _job(PUBLISH, RELEASE_JOB)
    assert "actions/checkout" in job
    assert "persist-credentials: false" in job
    assert job.index("actions/checkout") < job.index(MANIFEST_SCRIPT)


def test_the_manifest_is_generated_from_the_downloaded_distributions() -> None:
    job = _job(PUBLISH, RELEASE_JOB)
    step = _single_step(job, MANIFEST_SCRIPT)
    assert DOWNLOAD_ACTION in job
    assert job.index(DOWNLOAD_ACTION) < job.index(MANIFEST_SCRIPT)
    # Hashing the same download that becomes the release assets is the point:
    # both `dist/main/*` and `dist/fonts/*` are passed to the manifest.
    assert "dist/main/*" in step and "dist/fonts/*" in step


def test_the_manifest_is_generated_before_the_draft_is_created() -> None:
    job = _job(PUBLISH, RELEASE_JOB)
    assert job.index(MANIFEST_SCRIPT) < job.index("gh release create")


def test_the_draft_attaches_the_manifest_alongside_the_exact_distributions() -> None:
    create = _single_step(_job(PUBLISH, RELEASE_JOB), "gh release create")
    assert MANIFEST in create
    assert "dist/main/*" in create and "dist/fonts/*" in create
    # Draft first, public only afterwards.
    assert "--draft" in create
    assert "gh release edit" in _job(PUBLISH, RELEASE_JOB)


def test_the_manifest_is_written_outside_the_directories_published_to_pypi() -> None:
    step = _single_step(_job(PUBLISH, RELEASE_JOB), MANIFEST_SCRIPT)
    match = re.search(r"--output\s+(\S+)", step)
    assert match is not None, "the manifest step must name its output file"
    assert match.group(1) == MANIFEST
    for packages_dir in ("dist/main", "dist/fonts"):
        assert not match.group(1).startswith(packages_dir)


def test_the_manifest_is_not_uploaded_to_pypi() -> None:
    for job in ("publish-fonts", "publish-main"):
        block = _job(PUBLISH, job)
        assert MANIFEST not in block, job
        assert MANIFEST_SCRIPT not in block, job


def test_the_manifest_step_pins_the_release_version_from_the_tag() -> None:
    step = _single_step(_job(PUBLISH, RELEASE_JOB), MANIFEST_SCRIPT)
    assert 'RELEASE_TAG: ${{ github.ref_name }}' in step
    assert '"$RELEASE_TAG"' in step


# --- the release cannot be suppressed by a skipped fonts job ------------------


def test_the_release_job_survives_a_skipped_fonts_job_with_a_status_check() -> None:
    """The release needs `publish-main` by name, but the workflow-wide skip
    propagation from a *reused* fonts release reaches it through `publish-main`
    and its skipped fonts dependency, so its `if` must use a status check
    function. An implicit `success()` loses to that propagation: v0.1.7
    initially skipped the release even though core reached PyPI, and the
    recovery published it.
    """
    condition = _condition(_job(PUBLISH, RELEASE_JOB))

    assert "!cancelled()" in condition
    assert "needs.publish-main.result" in condition


def test_a_successful_core_upload_still_publishes_the_release_on_the_reuse_path() -> None:
    """Fonts skipped upstream (reuse) -> core succeeded -> the release must run."""
    assert _release_runs(core="success", cancelled=False)


def test_a_core_upload_that_did_not_succeed_cannot_publish_the_release() -> None:
    assert not _release_runs(core="failure", cancelled=False)
    assert not _release_runs(core="skipped", cancelled=False)
    assert not _release_runs(core="cancelled", cancelled=False)


def test_a_cancelled_run_cannot_publish_the_release() -> None:
    assert not _release_runs(core="success", cancelled=True)


def test_the_release_job_keeps_its_write_scope_and_no_pypi_environment() -> None:
    release = _job(PUBLISH, RELEASE_JOB)

    assert "contents: write" in release
    assert "environment: pypi" not in release
    assert "actions: read" not in release


def test_readme_describes_the_manifest_asset_rather_than_an_unspecified_hash_list() -> None:
    assert MANIFEST in README
    assert "SHA-256" in README
    assert "The release lists their SHA-256 hashes." not in README


def test_the_checklist_names_the_manifest_it_compares_against_pypi() -> None:
    section = CHECKLIST[CHECKLIST.index("## PyPI publication"):]
    assert MANIFEST in section
    assert "asset" in section


def _paragraphs(text: str, needle: str) -> list[str]:
    """Return the blank-line-delimited paragraphs that mention `needle`."""
    return [block for block in re.split(r"\n[ \t]*\n", text) if needle in block]


def test_readme_scopes_every_manifest_claim_to_releases_after_the_change() -> None:
    """The release the README links to is older than the manifest step.

    `README.md` points readers at `/releases/latest`, which today is v0.1.5 -
    published before the workflow attached any manifest. An unconditional "each
    release attaches a SHA256SUMS asset" is therefore false about the very
    release a reader reaches, so every claim about the asset must be scoped to
    releases published after the change and must say older ones have none.
    """
    paragraphs = _paragraphs(README, MANIFEST)
    assert paragraphs, "the README must still describe the manifest asset"
    for paragraph in paragraphs:
        assert any(marker in paragraph for marker in FUTURE_MARKERS), paragraph
        assert any(marker in paragraph for marker in ABSENCE_MARKERS), paragraph
    assert not EARLIER_RELEASES_ATTACH.search(README)


def test_the_checklist_marks_earlier_releases_as_having_no_manifest() -> None:
    """The maintainer must not hunt for a `SHA256SUMS` asset on v0.1.5."""
    section = CHECKLIST[CHECKLIST.index("## PyPI publication"):]
    assert MANIFEST in section
    assert any(marker in section for marker in ABSENCE_MARKERS), section
