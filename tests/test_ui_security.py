"""The local UI request gate: loopback, exact UI origin, and capability header.

The UI mounts the unauthenticated developer app, so its own gate is the extra
protection: a request must arrive from a provably loopback peer with a loopback
Host, an ``Origin`` (when present) must be *exactly this UI's* origin - not just
any loopback origin - and a mutating request must carry the per-process session
capability. These tests use the real app with ``TestClient`` and an explicit
loopback peer; no socket is opened and no remote host is contacted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from textflowkit.ui.app import create_app
from textflowkit.ui.security import CAPABILITY_HEADER, _origin_matches_ui, ui_origin

LOOPBACK_PEER = ("127.0.0.1", 50000)
FOREIGN_PEER = ("203.0.113.9", 4444)  # TEST-NET-3, documentation range


@pytest.fixture
def app():
    return create_app(host="127.0.0.1", port=8756)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)


@pytest.fixture
def token(client):
    """The capability token embedded in the served shell."""
    import re

    text = client.get("/").text
    return re.search(r'name="textflowkit-capability" content="([^"]+)"', text).group(1)


def test_shell_served_with_capability_meta(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'name="textflowkit-capability"' in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_capability_never_in_url_or_cookie(client):
    """The token is only in the body: not a Set-Cookie, not a Location."""
    resp = client.get("/")
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    assert "__TFK_CAPABILITY__" not in resp.text  # placeholder was replaced


def test_capability_not_leaked_as_literal_placeholder(client):
    text = client.get("/").text
    assert "__TFK_ORIGIN__" not in text
    assert "__TFK_VERSION__" not in text


def test_foreign_peer_refused(client, app):
    foreign = TestClient(app, base_url="http://127.0.0.1:8756", client=FOREIGN_PEER)
    resp = foreign.get("/")
    assert resp.status_code == 403
    assert "non-loopback peer" in resp.json()["error"]


def test_non_ip_peer_refused(app):
    """A peer that is not an IP literal cannot prove it is local."""
    unknown = TestClient(app, base_url="http://127.0.0.1:8756")  # peer 'testclient'
    resp = unknown.get("/")
    assert resp.status_code == 403


def test_foreign_host_refused(app):
    """A Host that is not loopback is refused even from a local peer."""
    c = TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)
    resp = c.get("/", headers={"host": "evil.example.com"})
    assert resp.status_code == 403


def test_foreign_origin_refused(client):
    resp = client.get("/", headers={"origin": "http://evil.example"})
    assert resp.status_code == 403


def test_wrong_port_loopback_origin_refused(client):
    """A loopback origin on a *different* port is not this UI's origin."""
    resp = client.get("/", headers={"origin": "http://127.0.0.1:9999"})
    assert resp.status_code == 403


def test_matching_origin_allowed(client):
    resp = client.get("/", headers={"origin": "http://127.0.0.1:8756"})
    assert resp.status_code == 200


def test_absent_origin_allowed_for_get(client):
    """A same-origin navigation GET may omit Origin entirely."""
    resp = client.get("/")
    assert resp.status_code == 200


@pytest.mark.parametrize("path,method", [
    ("/ui/jobs", "post"),
    ("/ui/uploads", "post"),
    ("/ui/preflight", "post"),
])
def test_mutator_without_capability_refused(client, path, method):
    resp = getattr(client, method)(path, json={})
    assert resp.status_code == 403
    assert "capability" in resp.json()["error"]


def test_mutator_with_capability_allowed(client, token):
    resp = client.post(
        "/ui/preflight", json={"source": "https://example.com/a.mp3", "formats": []},
        headers={CAPABILITY_HEADER: token},
    )
    assert resp.status_code == 200


def test_wrong_capability_refused(client):
    resp = client.post(
        "/ui/preflight", json={"source": "x", "formats": []},
        headers={CAPABILITY_HEADER: "not-the-token"},
    )
    assert resp.status_code == 403


def test_get_never_needs_capability(client):
    assert client.get("/ui/capabilities").status_code == 200
    assert client.get("/ui/jobs").status_code == 200


def test_allow_remote_does_not_widen_the_ui(app, monkeypatch):
    """TEXTFLOWKIT_ALLOW_REMOTE widens the developer launcher, not the UI."""
    monkeypatch.setenv("TEXTFLOWKIT_ALLOW_REMOTE", "1")
    foreign = TestClient(app, base_url="http://127.0.0.1:8756", client=FOREIGN_PEER)
    assert foreign.get("/").status_code == 403


def test_no_cors_headers(client):
    """No Access-Control-Allow-Origin is ever emitted."""
    for path in ("/", "/ui/capabilities"):
        resp = client.get(path, headers={"origin": "http://127.0.0.1:8756"})
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


# --- origin matching unit tests ------------------------------------------

@pytest.mark.parametrize("origin,expected,ok", [
    ("http://127.0.0.1:8756", "http://127.0.0.1:8756", True),
    ("HTTP://127.0.0.1:8756", "http://127.0.0.1:8756", True),
    ("http://127.0.0.1:8756/", "http://127.0.0.1:8756", True),
    ("http://127.0.0.1:8755", "http://127.0.0.1:8756", False),
    ("http://localhost:8756", "http://127.0.0.1:8756", False),
    ("https://127.0.0.1:8756", "http://127.0.0.1:8756", False),
    ("null", "http://127.0.0.1:8756", False),
    ("", "http://127.0.0.1:8756", False),
])
def test_origin_match(origin, expected, ok):
    assert _origin_matches_ui(origin, expected) is ok


def test_ui_origin_shape():
    assert ui_origin("127.0.0.1", 8756) == "http://127.0.0.1:8756"
