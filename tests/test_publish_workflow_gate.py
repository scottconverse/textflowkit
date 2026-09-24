"""The tag publication workflow must gate on CI before it builds or uploads.

The runtime behavior of the gate lives in `tests/test_publish_ci_gate.py`; this
file pins the workflow wiring that makes it effective: the step runs in the
build job (which every upload job needs), it runs before any distribution is
built or uploaded, it receives the token through the environment rather than a
command line, and only that job is granted Actions read.

The workflow text is parsed line by line instead of with a YAML library: PyYAML
is not a dependency of the published package or of the `dev` test extra, so a
parser import could fail on a CI matrix leg that has no diarization stack.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = (ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
GATE_SCRIPT = "scripts/verify_release_ci.py"
UPLOAD_ACTION = "pypa/gh-action-pypi-publish"


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


def _gate_step(job_block: str) -> str:
    steps = [step for step in _steps(job_block) if GATE_SCRIPT in step]
    assert len(steps) == 1, "expected exactly one gate step"
    return steps[0]


def test_the_build_job_runs_the_gate_before_it_builds_anything() -> None:
    build = _job(PUBLISH, "build")
    assert build.index(GATE_SCRIPT) < build.index("Build distributions")


def test_every_upload_job_depends_on_the_gate_that_the_build_job_runs() -> None:
    assert _job(PUBLISH, "build").index(GATE_SCRIPT) < PUBLISH.index(UPLOAD_ACTION)
    assert "needs: build" in _job(PUBLISH, "publish-fonts")
    assert "needs: publish-fonts" in _job(PUBLISH, "publish-main")
    assert "needs: publish-main" in _job(PUBLISH, "publish-github-release")
    for job in ("publish-fonts", "publish-main"):
        assert UPLOAD_ACTION in _job(PUBLISH, job), job


def test_the_gate_step_passes_the_token_through_the_environment_only() -> None:
    step = _gate_step(_job(PUBLISH, "build"))
    assert "GITHUB_TOKEN: ${{ github.token }}" in step
    # A token must never appear in a command line, where it lands in logs.
    assert "run: python scripts/verify_release_ci.py" in step
    run_lines = [line for line in step.splitlines() if line.strip().startswith("run:")]
    assert run_lines and all("token" not in line.lower() for line in run_lines)


def test_only_the_build_job_is_granted_actions_read() -> None:
    build = _job(PUBLISH, "build")
    permissions = build[build.index("permissions:"):]
    assert "contents: read" in permissions
    assert "actions: read" in permissions
    for job in ("publish-fonts", "publish-main", "publish-github-release"):
        assert "actions: read" not in _job(PUBLISH, job), job


def test_the_workflow_level_permission_stays_read_only_contents() -> None:
    preamble = PUBLISH[:PUBLISH.index("jobs:")]
    assert "permissions:\n  contents: read\n" in preamble
    assert "write" not in preamble


def test_the_checklist_names_the_automated_gate_and_its_limits() -> None:
    checklist = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")
    section = checklist[checklist.index("## PyPI publication"):]
    assert "verify_release_ci.py" in section
    assert "exact tagged commit" in section
    assert "live YouTube" in section
