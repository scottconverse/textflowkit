"""The MCP Streamable-HTTP boundary: loopback peer, Host, and Origin.

The MCP SDK's built-in DNS-rebinding protection checks `Host` and `Origin` but
never the ASGI peer. So the app `MCPServer.streamable_http_app(host='127.0.0.1')`
returns - the documented localhost-safe configuration - accepts a remote caller
that sends a loopback `Host` whenever it is reached through another ASGI server:
a widened uvicorn bind, a container, or a reverse proxy in front of it. Only a
loopback peer address proves the caller is local, which is the same verdict the
developer HTTP surface already reaches in `core.bind`.

These tests therefore exercise **TextFlowKit's exported app**
(`textflowkit.adapters.mcp_server.mcp.streamable_http_app`) and `run_http`, not a
helper and not the bare SDK app. Every request is in-process: `TestClient`
declares the peer address explicitly, so no socket is opened, no tool is invoked,
and no model or network is reached. `GET /mcp` without a session is refused by
the SDK itself, which is what the "the handler ran" cases assert: it is proof the
boundary let the request through rather than a boundary verdict of its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the MCP adapter")

from starlette.testclient import TestClient

from textflowkit.adapters import mcp_server
from textflowkit.adapters.mcp_server import mcp
from textflowkit.core.bind import ENV_ALLOW_REMOTE, UnsafeBindError

LOOPBACK_BASE = "http://127.0.0.1:8766"
LOOPBACK_PEER = ("127.0.0.1", 50000)
LOOPBACK_V6_PEER = ("::1", 50000)
FOREIGN_PEER = ("203.0.113.9", 4000)  # TEST-NET-3; documentation range, not routable
NON_ADDRESS_PEER = ("testclient", 50000)  # a harness that reports no real address

# The host:port a loopback caller sends; the SDK's own allowlist needs the port.
LOOPBACK_HOST = "127.0.0.1:8766"


@pytest.fixture(autouse=True)
def developer_mode(monkeypatch):
    """Plain developer mode, with no remote opt-in from the environment."""
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)


def streamable_app(host: str = "127.0.0.1"):
    """A fresh app per call: the SDK's session manager refuses to be entered twice."""
    return mcp.streamable_http_app(host=host)


@contextlib.contextmanager
def sdk_transport(monkeypatch):
    """Let the SDK's real transport path run, without opening a socket.

    `run_streamable_http_async` builds the app, wraps it in `uvicorn.Config`, and
    awaits `Server.serve()`. Replacing `serve` records the config the SDK really
    produced and returns at once: the app construction, the context it ran in,
    and the config are all the SDK's; only the bind is not. This is what proves
    an opt-in reaches the app the server actually serves, rather than an app a
    test built itself.
    """
    import uvicorn

    captured: dict = {}

    async def no_serve(self):
        captured["app"] = self.config.app
        captured["host"] = self.config.host
        captured["port"] = self.config.port

    monkeypatch.setattr(uvicorn.Server, "serve", no_serve)
    yield captured


def request_mcp(app, peer, headers=None, *, base_url: str = LOOPBACK_BASE):
    """One GET /mcp from an explicitly declared peer, in-process."""
    body = {"Accept": "text/event-stream", **(headers or {})}
    with TestClient(app, base_url=base_url, client=peer) as client:
        return client.get("/mcp", headers=body)


def get_mcp(peer, headers=None, *, base_url: str = LOOPBACK_BASE, app_host: str = "127.0.0.1"):
    return request_mcp(streamable_app(app_host), peer, headers, base_url=base_url)


# A real protocol exchange, not a tool call: `initialize` negotiates capabilities
# and reaches no tool, model, or network. It is the one request whose *streamed*
# answer the guard has to leave intact, since it is middleware over a transport
# that streams.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "textflowkit-boundary-test", "version": "0"},
    },
}


def post_mcp(app, peer, payload, headers=None, *, base_url: str = LOOPBACK_BASE):
    """One JSON-RPC POST to /mcp from an explicitly declared peer, in-process."""
    body = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **(headers or {}),
    }
    with TestClient(app, base_url=base_url, client=peer) as client:
        return client.post("/mcp", json=payload, headers=body)


def sse_messages(text: str) -> list:
    """The JSON-RPC messages an event-stream body carries."""
    return [json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")]


def refusal_reason(response) -> str:
    """The guard's refusal text, asserting the refusal is a readable JSON body."""
    assert response.status_code == 403, response.text
    return json.loads(response.text)["error"]


class _HandlerRan(Exception):
    """Raised if the MCP handler is reached when the boundary should refuse."""


class _Detector:
    """ASGI app standing in for the MCP handler; reaching it fails the test."""

    async def __call__(self, scope, receive, send):
        raise _HandlerRan("the MCP handler ran for a request the boundary refused")


def detach_mcp_handler(app):
    """Replace the /mcp route's ASGI app so a refusal becomes provable.

    The guard runs outside the router, so a refused request must never reach
    this. `TestClient` re-raises, which turns "reached the handler" into a test
    failure rather than a status code that could be confused with a verdict.
    """
    for route in app.routes:
        if getattr(route, "path", None) == "/mcp":
            route.app = _Detector()
            return app
    raise AssertionError("the MCP app has no /mcp route")


# --- the peer is the security decision ------------------------------------

def test_remote_peer_with_loopback_host_is_refused():
    """The defect: the SDK's Host check passes, so the handler was reached."""
    response = get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST})
    assert "loopback" in refusal_reason(response).lower()


def test_remote_peer_refusal_happens_before_the_mcp_handler():
    response = request_mcp(
        detach_mcp_handler(streamable_app()), FOREIGN_PEER, {"Host": LOOPBACK_HOST}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("peer", [None, NON_ADDRESS_PEER])
def test_indeterminate_peer_is_refused(peer):
    """A peer the server cannot judge is not assumed local."""
    response = get_mcp(peer, {"Host": LOOPBACK_HOST})
    assert "loopback" in refusal_reason(response).lower()


@pytest.mark.parametrize("peer", [None, NON_ADDRESS_PEER])
def test_indeterminate_peer_refusal_happens_before_the_mcp_handler(peer):
    app = detach_mcp_handler(streamable_app())
    with TestClient(app, base_url=LOOPBACK_BASE, client=peer) as client:
        response = client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Forwarded-Host": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
    ],
)
def test_forwarded_headers_cannot_supply_a_local_peer(headers):
    """Forwarded headers are attacker-controlled; only the peer establishes locality."""
    response = get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST, **headers})
    assert response.status_code == 403


# --- foreign Host and Origin stay refused ---------------------------------

def test_foreign_host_is_refused():
    """Also refused by the SDK's own allowlist - and refused before it here."""
    response = get_mcp(LOOPBACK_PEER, {"Host": "evil.example"})
    assert "host" in refusal_reason(response).lower()


def test_missing_host_is_refused():
    response = get_mcp(LOOPBACK_PEER, {"Host": ""})
    assert "host" in refusal_reason(response).lower()


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "https://evil.example:8443", "null"]
)
def test_foreign_origin_is_refused(origin):
    response = get_mcp(LOOPBACK_PEER, {"Host": LOOPBACK_HOST, "Origin": origin})
    assert "origin" in refusal_reason(response).lower()


def test_foreign_host_refusal_happens_before_the_mcp_handler():
    response = request_mcp(
        detach_mcp_handler(streamable_app()), LOOPBACK_PEER, {"Host": "evil.example"}
    )
    assert response.status_code == 403


# --- a real local caller still reaches the handler -------------------------

def test_loopback_peer_reaches_the_mcp_handler():
    """400 Missing session ID is the SDK's answer, i.e. the boundary let it in."""
    response = get_mcp(LOOPBACK_PEER, {"Host": LOOPBACK_HOST})
    assert response.status_code == 400, response.text
    assert "session" in response.text.lower()


@pytest.mark.parametrize(
    "peer", [LOOPBACK_PEER, LOOPBACK_V6_PEER, ("::ffff:127.0.0.1", 50000)]
)
def test_loopback_peer_variants_reach_the_mcp_handler(peer):
    response = get_mcp(peer, {"Host": LOOPBACK_HOST})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "host", [LOOPBACK_HOST, "localhost:8766", "[::1]:8766"]
)
def test_loopback_host_variants_reach_the_mcp_handler(host):
    """Loopback Hosts our guard allows still reach the SDK's own allowlist.

    The list is exactly the SDK's auto-enabled `allowed_hosts`, which is
    narrower than `core.bind`'s loopback policy: it wants a port, and it names
    only 127.0.0.1/localhost/[::1]. A Host our guard permits and the SDK then
    refuses is refused either way, so the guard has no reason to widen it.
    """
    response = get_mcp(LOOPBACK_PEER, {"Host": host})
    assert response.status_code == 400, response.text


def test_loopback_origin_reaches_the_mcp_handler():
    response = get_mcp(
        LOOPBACK_PEER, {"Host": LOOPBACK_HOST, "Origin": "http://127.0.0.1:3000"}
    )
    assert response.status_code == 400, response.text


def test_local_initialize_round_trip_survives_the_guard():
    """The answer streams, so the guard must pass `send` through unbuffered."""
    response = post_mcp(streamable_app(), LOOPBACK_PEER, INITIALIZE, {"Host": LOOPBACK_HOST})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    (message,) = sse_messages(response.text)
    assert message["result"]["serverInfo"]["name"] == "textflowkit"


def test_remote_initialize_is_refused_before_it_is_answered():
    response = post_mcp(streamable_app(), FOREIGN_PEER, INITIALIZE, {"Host": LOOPBACK_HOST})
    assert "loopback" in refusal_reason(response).lower()


# --- direct ASGI use: a widened bind cannot serve a remote peer -----------

def test_widened_bind_app_refuses_a_remote_peer():
    """`uvicorn ...:mcp.streamable_http_app(host='0.0.0.0')` bypasses main()."""
    response = get_mcp(
        FOREIGN_PEER, {"Host": "10.0.0.5:8766"}, base_url="http://10.0.0.5:8766", app_host="0.0.0.0"
    )
    assert response.status_code == 403


# --- explicit remote opt-in ----------------------------------------------

def test_env_optin_stands_the_guard_down(monkeypatch):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    response = get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST})
    assert response.status_code == 400, response.text


def test_env_optin_does_not_apply_to_another_app(monkeypatch):
    """The opt-in is read per request, so it is not cached in the guard."""
    response = get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST})
    assert response.status_code == 403
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    response = get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST})
    assert response.status_code == 400, response.text


# --- stdio is not an HTTP surface -----------------------------------------

def test_stdio_transport_never_builds_an_http_app(monkeypatch):
    called = {}

    def boom(**kwargs):
        raise AssertionError(f"stdio built a Streamable-HTTP app: {kwargs}")

    monkeypatch.setattr(mcp, "streamable_http_app", boom)
    monkeypatch.setattr(mcp_server, "run_stdio", lambda: called.setdefault("stdio", True))
    assert mcp_server.main(["--transport", "stdio", "--host", "0.0.0.0"]) == 0
    assert called.get("stdio") is True


# --- run_http validates its own bind, not only main() ---------------------

def test_run_http_refuses_a_non_loopback_bind(monkeypatch):
    """No bind is attempted: the guard runs before the transport is reached."""

    def bind_attempted(**kwargs):
        raise AssertionError(f"run_http reached the transport for {kwargs}")

    monkeypatch.setattr(mcp, "run", bind_attempted)
    with pytest.raises(UnsafeBindError):
        mcp_server.run_http(host="0.0.0.0")


def test_run_http_still_serves_loopback(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    mcp_server.run_http(host="127.0.0.1")
    assert calls == [
        {"transport": "streamable-http", "host": "127.0.0.1", "port": 8766, "streamable_http_path": "/mcp"}
    ]


def test_run_http_allows_a_non_loopback_bind_with_the_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    mcp_server.run_http(host="0.0.0.0", allow_remote=True)
    assert calls and calls[0]["host"] == "0.0.0.0"


def test_run_http_allows_a_non_loopback_bind_with_the_env(monkeypatch):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    calls = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    mcp_server.run_http(host="0.0.0.0")
    assert calls and calls[0]["host"] == "0.0.0.0"


def test_allow_remote_is_captured_by_the_app_the_sdk_builds(monkeypatch):
    """The flag reaches the per-request guard without a process-global env write."""
    with sdk_transport(monkeypatch) as captured:
        mcp_server.run_http(host="127.0.0.1", allow_remote=True)
    app = captured["app"]
    assert captured["host"] == "127.0.0.1", "the SDK never built the app"
    assert request_mcp(app, FOREIGN_PEER, {"Host": LOOPBACK_HOST}).status_code == 400


def test_the_app_the_sdk_builds_refuses_a_remote_peer_without_the_flag(monkeypatch):
    with sdk_transport(monkeypatch) as captured:
        mcp_server.run_http(host="127.0.0.1")
    app = captured["app"]
    assert captured["host"] == "127.0.0.1", "the SDK never built the app"
    assert request_mcp(app, FOREIGN_PEER, {"Host": LOOPBACK_HOST}).status_code == 403


def test_an_opted_in_server_does_not_widen_a_concurrent_app(monkeypatch):
    """Audit regression: the opt-in must not reach an app it was not granted to.

    `remote_opt_in` was one mutable attribute on the process-wide `mcp`, so any
    thread that built a direct app while an opted-in server was starting captured
    the opt-in and lost the boundary. Recorded red: 400, where 403 is required.
    """
    entered = threading.Event()
    release = threading.Event()

    def hold(**kwargs):
        entered.set()
        assert release.wait(10), "the holder was never released"

    monkeypatch.setattr(mcp, "run", hold)
    worker = threading.Thread(
        target=lambda: mcp_server.run_http(host="127.0.0.1", allow_remote=True), daemon=True
    )
    worker.start()
    try:
        assert entered.wait(10), "run_http never reached the transport"
        assert get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST}).status_code == 403
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def test_the_optin_ends_with_the_run_that_was_granted_it(monkeypatch):
    """The app the run built opts in; an app built afterwards does not."""
    with sdk_transport(monkeypatch) as captured:
        mcp_server.run_http(host="127.0.0.1", allow_remote=True)
    assert request_mcp(captured["app"], FOREIGN_PEER, {"Host": LOOPBACK_HOST}).status_code == 400
    assert get_mcp(FOREIGN_PEER, {"Host": LOOPBACK_HOST}).status_code == 403


def test_the_flag_is_not_applied_as_a_process_global(monkeypatch):
    """No env write: an opt-in for one server must not widen another process-wide."""
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    with sdk_transport(monkeypatch):
        mcp_server.run_http(host="127.0.0.1", allow_remote=True)
    assert ENV_ALLOW_REMOTE not in os.environ


# --- the CLI keeps its flag and env behaviour -----------------------------

def test_cli_http_passes_the_flag_through(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server, "run_http", lambda **kwargs: calls.append(kwargs)
    )
    assert mcp_server.main(["--transport", "http", "--host", "0.0.0.0", "--allow-remote"]) == 0
    assert calls == [
        {"host": "0.0.0.0", "port": 8766, "path": "/mcp", "allow_remote": True}
    ]


def test_cli_http_passes_no_optin_without_the_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server, "run_http", lambda **kwargs: calls.append(kwargs)
    )
    assert mcp_server.main(["--transport", "http", "--host", "127.0.0.1"]) == 0
    assert calls == [
        {"host": "127.0.0.1", "port": 8766, "path": "/mcp", "allow_remote": None}
    ]


# --- the SDK app underneath has no peer check (premise pin) ---------------

def test_the_bare_sdk_app_has_no_peer_guard():
    """The upstream gap this unit compensates for, pinned rather than assumed.

    This is not coverage of TextFlowKit: it asserts the *lack* of an upstream
    control, so it failing means the SDK grew a peer check and ours is only
    redundant. `MCPServer` is built fresh here so no TextFlowKit tool is touched.
    """
    from mcp.server.mcpserver import MCPServer

    raw = MCPServer("u37-premise-pin").streamable_http_app(host="127.0.0.1")
    response = request_mcp(raw, FOREIGN_PEER, {"Host": LOOPBACK_HOST})
    assert response.status_code != 403
