"""Fail the release unless the exact tagged commit has a successful main CI run.

A tag push is not evidence that the tagged commit passed deterministic CI: the
run that a maintainer looked at may have been a pull-request run, a run for a
different commit, a run still in progress, or a run that failed. This gate reads
GitHub's own record for the commit being published and refuses to continue
unless it finds a completed, successful `push` run of the CI workflow on
`main` whose `head_sha` is exactly that commit.

It is deliberately fail-closed: an unreachable or unreadable API, an empty
result, a mismatched commit, or an unexpected field aborts the release instead
of allowing a build. The token is read from `GITHUB_TOKEN` and never appears in
an argument, URL, or log line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# GitHub resolves `workflow_id` as a workflow filename, not as a repository path:
# `/workflows/ci.yml/runs` answers 200 while `/workflows/.github/workflows/ci.yml/runs`
# answers 404 (read-only probe of this public repository, 2026-09-24). A path here
# would fail the gate closed even with green CI.
DEFAULT_WORKFLOW = "ci.yml"
DEFAULT_BRANCH = "main"
DEFAULT_EVENT = "push"
DEFAULT_TIMEOUT = 30
API_ROOT = "https://api.github.com"
COMMIT_ID = re.compile(r"\A[0-9a-f]{40}\Z")
REPO_NAME = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
RUN_FIELDS = ("head_sha", "head_branch", "event", "status", "conclusion", "html_url")


class GateError(Exception):
    """A release-blocking condition, described for the operator."""


def _runs_url(repo: str, workflow: str, sha: str, branch: str, event: str) -> str:
    path = f"/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='/')}/runs"
    query = urllib.parse.urlencode({
        "branch": branch, "event": event, "head_sha": sha, "per_page": 100,
    })
    return f"{API_ROOT}{path}?{query}"


def _request_json(url: str, token: str, opener, timeout: int) -> object:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "textflowkit-release-gate",
    })
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise GateError(f"GitHub API returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateError(f"cannot reach the GitHub API: {exc}") from exc
    if status != 200:
        raise GateError(f"GitHub API returned HTTP {status} for {url}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"GitHub API response is not UTF-8 JSON: {exc}") from exc


def _matching_runs(payload: object, sha: str, branch: str, event: str) -> list[dict]:
    """Keep only rows that are themselves the required run.

    The query filters are a request, not a guarantee: a proxy, a cached
    response, or a changed API could return rows for other commits. Every row is
    checked here, and an unreadable row is an error rather than a skip.
    """
    if not isinstance(payload, dict):
        raise GateError("GitHub API response is not a JSON object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise GateError("GitHub API response has no workflow_runs list")
    matching = []
    for run in runs:
        if not isinstance(run, dict):
            raise GateError("GitHub API returned a workflow run that is not an object")
        missing = [field for field in RUN_FIELDS if field not in run]
        if missing:
            raise GateError(f"GitHub API run entry is missing {', '.join(missing)}")
        if run["head_sha"] == sha and run["head_branch"] == branch and run["event"] == event:
            matching.append(run)
    return matching


def check_exact_commit_ci(
    repo: str,
    sha: str,
    token: str,
    *,
    workflow: str = DEFAULT_WORKFLOW,
    branch: str = DEFAULT_BRANCH,
    event: str = DEFAULT_EVENT,
    timeout: int = DEFAULT_TIMEOUT,
    opener=None,
) -> str:
    """Return the URL of the run that authorizes publishing `sha`."""
    if not REPO_NAME.match(repo):
        raise GateError(f"--repo must be owner/name, got {repo!r}")
    if not COMMIT_ID.match(sha):
        raise GateError(f"commit must be 40 lowercase hex characters, got {sha!r}")
    if not token:
        raise GateError("GITHUB_TOKEN is not set, so Actions results cannot be read")
    url = _runs_url(repo, workflow, sha, branch, event)
    payload = _request_json(url, token, opener or urllib.request.urlopen, timeout)
    matching = _matching_runs(payload, sha, branch, event)
    if not matching:
        raise GateError(f"no {event} run of {workflow} on {branch} for commit {sha}")
    successful = [
        run for run in matching
        if run["status"] == "completed" and run["conclusion"] == "success"
    ]
    if successful:
        return successful[0]["html_url"]
    incomplete = [run for run in matching if run["status"] != "completed"]
    if incomplete:
        statuses = ", ".join(sorted({str(run["status"]) for run in incomplete}))
        raise GateError(
            f"the {event} run of {workflow} on {branch} for commit {sha} has not "
            f"completed (status: {statuses})"
        )
    conclusions = ", ".join(sorted({str(run["conclusion"] or "unknown") for run in matching}))
    raise GateError(
        f"no successful {event} run of {workflow} on {branch} for commit {sha} "
        f"(conclusions: {conclusions})"
    )


def main(argv: list[str] | None = None, *, opener=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="owner/name, defaults to $GITHUB_REPOSITORY")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""),
                        help="tagged commit, defaults to $GITHUB_SHA")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        html_url = check_exact_commit_ci(
            args.repo, args.sha, os.environ.get("GITHUB_TOKEN", ""),
            workflow=args.workflow, branch=args.branch,
            timeout=args.timeout, opener=opener,
        )
    except GateError as exc:
        print(f"release CI gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"release CI gate passed: successful {args.branch} push run of {args.workflow} "
          f"for commit {args.sha}")
    print(f"run: {html_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
