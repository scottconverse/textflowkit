"""Publication must be gated on CI that actually ran for the tagged commit.

A pull-request run, a later `main` run, a run that failed or is still going, and
an unreadable API response are all *not* evidence that the commit being
published passed deterministic CI. Only a completed, successful `push` run of
the CI workflow on `main` whose `head_sha` is the tagged commit is.

These tests never touch GitHub: the HTTP transport is injected and every
response is a local fixture.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from scripts import verify_release_ci

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "scottconverse/textflowkit"
TOKEN = "ghs_do_not_leak_me"
RUN_URL = "https://github.com/scottconverse/textflowkit/actions/runs/1"


class _Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(**overrides: object) -> dict:
    run = {
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "html_url": RUN_URL,
    }
    run.update(overrides)
    return run


def _opener(payload=None, *, error=None, seen=None):
    def open_(request, timeout=None):
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error
        return _Response(payload)

    return open_


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


def _gate(capsys, payload=None, *, error=None, sha=SHA, seen=None, argv=None):
    code = verify_release_ci.main(
        argv if argv is not None else ["--repo", REPO, "--sha", sha],
        opener=_opener(payload, error=error, seen=seen),
    )
    return code, capsys.readouterr()


def test_accepts_completed_successful_main_push_at_the_exact_commit(capsys):
    code, out = _gate(capsys, {"total_count": 1, "workflow_runs": [_run()]})
    assert code == 0
    assert SHA in out.out
    assert RUN_URL in out.out
    assert out.err == ""


def test_queries_the_ci_workflow_runs_filtered_by_sha_branch_and_event(capsys):
    seen: list = []
    code, _ = _gate(capsys, {"workflow_runs": [_run()]}, seen=seen)
    assert code == 0
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url.startswith(
        "https://api.github.com/repos/scottconverse/textflowkit/actions/workflows/ci.yml/runs"
    )
    assert f"head_sha={SHA}" in request.full_url
    assert "branch=main" in request.full_url
    assert "event=push" in request.full_url
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in request.full_url


def test_default_workflow_identifier_is_the_bare_filename(capsys):
    """GitHub resolves `workflow_id` as a filename, not as a repository path.

    A read-only, unauthenticated probe of this public repository (coordinator,
    2026-09-24) returned HTTP 200 for `/workflows/ci.yml/runs` and HTTP 404 for
    `/workflows/.github/workflows/ci.yml/runs`. A release must not fail closed
    over the identifier form when the commit's CI is green.
    """
    assert verify_release_ci.DEFAULT_WORKFLOW == "ci.yml"
    seen: list = []
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, seen=seen)
    assert code == 0
    assert "/actions/workflows/ci.yml/runs?" in seen[0].full_url
    assert "/workflows/.github" not in seen[0].full_url
    assert out.err == ""


def test_accepts_a_later_success_after_an_earlier_failed_rerun(capsys):
    payload = {"workflow_runs": [
        _run(conclusion="failure", html_url="https://github.com/example/runs/old"),
        _run(),
    ]}
    code, out = _gate(capsys, payload)
    assert code == 0
    assert RUN_URL in out.out


def test_rejects_when_main_has_no_run_at_all(capsys):
    code, out = _gate(capsys, {"total_count": 0, "workflow_runs": []})
    assert code == 1
    assert "no push run" in out.err
    assert SHA in out.err


def test_rejects_a_run_that_passed_for_a_different_commit(capsys):
    code, out = _gate(capsys, {"workflow_runs": [_run(head_sha=OTHER_SHA)]})
    assert code == 1
    assert "no push run" in out.err


def test_rejects_a_successful_pull_request_run_at_the_same_commit(capsys):
    code, out = _gate(capsys, {"workflow_runs": [_run(event="pull_request")]})
    assert code == 1
    assert "no push run" in out.err


@pytest.mark.parametrize("branch", ["release-0.1", "maintenance", "feature/x"])
def test_rejects_a_push_run_on_another_branch(capsys, branch):
    code, out = _gate(capsys, {"workflow_runs": [_run(head_branch=branch)]})
    assert code == 1
    assert "no push run" in out.err


@pytest.mark.parametrize("status", ["queued", "in_progress", "requested", "waiting"])
def test_rejects_a_run_that_has_not_completed(capsys, status):
    code, out = _gate(capsys, {"workflow_runs": [_run(status=status, conclusion=None)]})
    assert code == 1
    assert "has not completed" in out.err
    assert status in out.err


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_rejects_a_concluded_but_unsuccessful_run(capsys, conclusion):
    code, out = _gate(capsys, {"workflow_runs": [_run(conclusion=conclusion)]})
    assert code == 1
    assert "no successful push run" in out.err
    assert conclusion in out.err


def test_rejects_a_completed_run_without_a_conclusion(capsys):
    code, out = _gate(capsys, {"workflow_runs": [_run(conclusion=None)]})
    assert code == 1
    assert "unknown" in out.err


def test_incomplete_run_blocks_even_when_another_run_failed(capsys):
    payload = {"workflow_runs": [_run(status="in_progress", conclusion=None), _run(conclusion="failure")]}
    code, out = _gate(capsys, payload)
    assert code == 1
    assert "has not completed" in out.err


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://api.github.com/x", 500, "server error", {}, io.BytesIO(b"")),
    urllib.error.HTTPError("https://api.github.com/x", 403, "forbidden", {}, io.BytesIO(b"")),
    urllib.error.HTTPError("https://api.github.com/x", 404, "not found", {}, io.BytesIO(b"")),
    urllib.error.URLError("temporary failure in name resolution"),
    TimeoutError("timed out"),
])
def test_fails_closed_on_transport_errors(capsys, error):
    code, out = _gate(capsys, error=error)
    assert code == 1
    assert "release CI gate failed" in out.err


@pytest.mark.parametrize("payload", [
    [{"workflow_runs": []}],
    {"total_count": 1},
    {"workflow_runs": "not-a-list"},
    {"workflow_runs": [{"head_sha": SHA}]},
    {"workflow_runs": ["not-an-object"]},
])
def test_fails_closed_on_a_malformed_response(capsys, payload):
    code, out = _gate(capsys, payload)
    assert code == 1
    assert "release CI gate failed" in out.err


def test_fails_closed_on_a_non_json_body(capsys):
    def opener(request, timeout=None):
        class Broken(_Response):
            def read(self):
                return b"<html>proxy error</html>"

        return Broken({})

    code = verify_release_ci.main(["--repo", REPO, "--sha", SHA], opener=opener)
    out = capsys.readouterr()
    assert code == 1
    assert "release CI gate failed" in out.err


def test_refuses_to_query_without_a_token(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    seen: list = []
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, seen=seen)
    assert code == 1
    assert "GITHUB_TOKEN" in out.err
    assert seen == []


@pytest.mark.parametrize("sha", ["", "abc", "a" * 39, "A" * 40, "z" * 40, "a" * 41])
def test_refuses_a_sha_that_is_not_a_full_commit_id(capsys, sha):
    seen: list = []
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, sha=sha, seen=seen)
    assert code == 1
    assert seen == []
    assert "release CI gate failed" in out.err


@pytest.mark.parametrize("repo", ["", "textflowkit", "a/b/c", "/x", "x/"])
def test_refuses_a_repo_that_is_not_owner_slash_name(capsys, repo):
    seen: list = []
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, seen=seen, argv=["--repo", repo, "--sha", SHA])
    assert code == 1
    assert seen == []
    assert "release CI gate failed" in out.err


def test_refuses_when_no_commit_is_named(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    seen: list = []
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, seen=seen, argv=["--repo", REPO])
    assert code == 1
    assert seen == []
    assert "release CI gate failed" in out.err


def test_reads_repo_and_commit_from_the_ci_environment(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    code, out = _gate(capsys, {"workflow_runs": [_run()]}, seen=None, argv=[])
    assert code == 0
    assert RUN_URL in out.out


@pytest.mark.parametrize("payload", [{"workflow_runs": [_run()]}, {"workflow_runs": []}])
def test_never_prints_the_token(capsys, payload):
    code, out = _gate(capsys, payload)
    assert code in (0, 1)
    assert TOKEN not in out.out
    assert TOKEN not in out.err
