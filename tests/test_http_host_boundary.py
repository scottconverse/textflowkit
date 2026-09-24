"""Developer-mode HTTP boundary: Host/Origin/peer validation.

Developer mode has no authentication, so the served app must not act on behalf of
a foreign origin. A browser can be pointed at a name that resolves to loopback
(DNS rebinding) and will then send that foreign name as `Host`; it can also be
used to fire cross-site requests at a local service with its own `Origin`. Both
are rejected here before any route runs. `TestClient` is used with an explicit
loopback base URL and explicit peer address so the tests stay local: no socket is
opened and no remote host is contacted.

Production (`TEXTFLOWKIT_PROFILE=production`) keeps its Bearer profile and is not
subject to the developer Host allowlist. Explicit remote opt-in
(`--allow-remote` / `TEXTFLOWKIT_ALLOW_REMOTE=1`) keeps working.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from textflowkit.adapters import http_server
from textflowkit.core.bind import ENV_ALLOW_REMOTE
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import reset_default_store

LOOPBACK_BASE = "http://127.0.0.1"
FOREIGN_PEER = ("203.0.113.9", 4444)  # TEST-NET-3; documentation range, not routable


@pytest.fixture(autouse=True)
def developer_mode(monkeypatch):
    """Plain developer mode: no profile, no remote opt-in, no token.

    Deliberately does not reset the job store or executor: no test here touches
    either (every request is refused before the route, or goes to /sources or
    /health), and resetting the default store would re-bind the cached executor
    to a stale store for later tests.
    """
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_API_TOKEN", raising=False)


def client(**kwargs) -> TestClient:
    kwargs.setdefault("base_url", LOOPBACK_BASE)
    return TestClient(http_server.app, **kwargs)


# --- foreign Host is refused before the route runs -------------------------

def test_foreign_host_rejected_on_get_sources():
    response = client().get("/sources", headers={"Host": "evil.example"})
    assert response.status_code == 403
    assert "host" in response.json()["error"].lower()


def test_foreign_host_does_not_reach_jobs_handler(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("submit_request ran for a foreign-Host request")

    monkeypatch.setattr(http_server, "submit_request", boom)
    response = client().post("/jobs", json={"source": "x"}, headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_foreign_host_beats_invalid_body_validation():
    """403 (boundary) must come first, not the handler's 422."""
    response = client().post(
        "/jobs",
        json={"source": "x", "formats": ["xyzzy"]},
        headers={"Host": "evil.example"},
    )
    assert response.status_code == 403


def test_missing_host_rejected():
    response = client().get("/sources", headers={"Host": ""})
    assert response.status_code == 403


# --- legitimate loopback Host variants still work --------------------------

@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8767",
        "localhost",
        "localhost:8767",
        "[::1]:8767",
        "127.0.0.2",
    ],
)
def test_loopback_host_variants_accepted(host):
    response = client().get("/sources", headers={"Host": host})
    assert response.status_code == 200, response.text


# --- foreign Origin is refused --------------------------------------------

@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "https://evil.example:8443", "null", "http://203.0.113.9"],
)
def test_foreign_origin_rejected(origin):
    response = client().get(
        "/sources", headers={"Host": "127.0.0.1", "Origin": origin}
    )
    assert response.status_code == 403
    assert "origin" in response.json()["error"].lower()


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1", "http://127.0.0.1:3000", "http://localhost:5173", "http://[::1]:8080"],
)
def test_loopback_origin_accepted(origin):
    response = client().get(
        "/sources", headers={"Host": "127.0.0.1", "Origin": origin}
    )
    assert response.status_code == 200, response.text


def test_no_origin_still_works():
    """Non-browser clients (curl, SDKs) send no Origin."""
    assert client().get("/sources").status_code == 200


# --- direct-ASGI startup: non-loopback peer without opt-in ----------------

def test_non_loopback_peer_rejected_in_developer_mode():
    response = client(client=FOREIGN_PEER).get("/sources", headers={"Host": "127.0.0.1"})
    assert response.status_code == 403
    assert "loopback" in response.json()["error"].lower()


def test_non_loopback_peer_cannot_forge_forwarded_headers():
    """We must not trust X-Forwarded-* to decide locality."""
    response = client(client=FOREIGN_PEER).get(
        "/sources",
        headers={
            "Host": "127.0.0.1",
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Host": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        },
    )
    assert response.status_code == 403


def test_forwarded_host_header_does_not_bypass_host_check():
    """X-Forwarded-Host is not the Host header; it must not widen the allowlist."""
    response = client().get(
        "/sources", headers={"Host": "evil.example", "X-Forwarded-Host": "127.0.0.1"}
    )
    assert response.status_code == 403


def test_indeterminate_peer_is_allowed_local_only():
    """Documented limitation: a peer that is not an IP address is not judged.

    Real ASGI servers (uvicorn) always report the peer IP, so this branch only
    affects in-process test harnesses. The Host allowlist still applies.
    """
    assert client(client=("testclient", 50000)).get("/sources").status_code == 200


# --- explicit remote opt-in still works -----------------------------------

def test_remote_optin_allows_non_loopback_peer(monkeypatch):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    response = client(client=FOREIGN_PEER).get("/sources", headers={"Host": "127.0.0.1"})
    assert response.status_code == 200


def test_remote_optin_allows_gateway_host_header(monkeypatch):
    """Behind a gateway the public Host arrives; opt-in is what permits it."""
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    response = client().get("/sources", headers={"Host": "transcribe.example"})
    assert response.status_code == 200


# --- production profile keeps its own (Bearer) policy --------------------

def test_production_profile_uses_bearer_not_host_allowlist(monkeypatch, tmp_path):
    # Production validates that the cached store and executor point at
    # TEXTFLOWKIT_DB, so they must be re-bound here; without this the check runs
    # against whatever store an earlier test cached.
    reset_default_executor()
    reset_default_store()
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_API_TOKEN", "a-long-test-token-12345")
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(tmp_path / "input"))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    (tmp_path / "input").mkdir()
    headers = {"Authorization": "Bearer a-long-test-token-12345"}
    try:
        # A public Host behind a real domain is normal for production, with a token.
        assert client().get(
            "/health", headers={**headers, "Host": "api.example"}
        ).status_code == 200
        # Without the token it is still unauthorized, regardless of Host.
        assert client().get("/health", headers={"Host": "127.0.0.1"}).status_code == 401
    finally:
        reset_default_executor()
        reset_default_store()
