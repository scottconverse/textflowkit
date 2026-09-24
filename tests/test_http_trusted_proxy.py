"""Production client identity behind an explicitly configured trusted proxy.

The production rate limit is per client. Behind a proxy every request arrives
from the proxy's address, so one bucket serves every caller. The fix is only
safe if the forwarded chain is consulted *exactly* when the TCP peer is a proxy
the operator named: `X-Forwarded-For` is attacker-controlled text, so any
request whose peer is not trusted must ignore it completely.

These tests drive the real ASGI app through `TestClient` with an explicit peer
address; no socket is opened and nothing on the network is contacted. `TestClient`
does not run uvicorn's `ProxyHeadersMiddleware`, so `scope['client']` is exactly
the peer each test names - the same raw peer the application sees when its own
CLI disables that middleware (see `test_cli_start_does_not_let_uvicorn_rewrite_the_peer`).

The identity rule is "rightmost untrusted hop": a proxy appends the address it
saw to the right of the header, so the leftmost entries are whatever the caller
typed and are worthless. Walking from the right and stopping at the first
address that is not a trusted proxy is the only position a caller cannot forge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from textflowkit.adapters import http_server
from textflowkit.core import bind
from textflowkit.core.bind import ENV_ALLOW_REMOTE
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import reset_default_store

TOKEN = "a-long-test-token-12345"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Spelled out rather than imported: the name is part of the operator-facing
# contract, and a wrong constant must fail as a behaviour mismatch, not as a
# collection error that hides every other result in this file.
ENV_TRUSTED_PROXY_IPS = "TEXTFLOWKIT_TRUSTED_PROXY_IPS"
ENV_RATE_PER_MINUTE = "TEXTFLOWKIT_RATE_PER_MINUTE"

PROXY = ("10.0.0.1", 4000)  # the trusted hop in these tests
DIRECT = ("203.0.113.5", 4000)  # TEST-NET-3; documentation range, not routable
CLIENT_A = "198.51.100.7"  # TEST-NET-2; a real client behind PROXY
CLIENT_B = "203.0.113.9"


@pytest.fixture
def production(monkeypatch, tmp_path):
    """A production profile with the trusted-proxy setting left unset."""
    reset_default_executor()
    reset_default_store()
    input_root = tmp_path / "input"
    input_root.mkdir()
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_API_TOKEN", TOKEN)
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(input_root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv(ENV_TRUSTED_PROXY_IPS, raising=False)
    http_server._RATE_BUCKETS.clear()
    monkeypatch.setattr(http_server, "_next_expiry", float("inf"), raising=False)
    yield
    reset_default_executor()
    reset_default_store()
    http_server._RATE_BUCKETS.clear()


def get(peer, forwarded_for=None, *, host=None):
    """One production request from an explicit TCP peer.

    `forwarded_for` may be a list, which sends one `X-Forwarded-For` header line
    per entry - what a proxy does when it appends its own header instead of
    extending the caller's value.
    """
    headers = list(AUTH.items())
    if forwarded_for is not None:
        values = forwarded_for if isinstance(forwarded_for, list) else [forwarded_for]
        headers.extend(("X-Forwarded-For", value) for value in values)
    if host is not None:
        headers.append(("Host", host))
    return TestClient(http_server.app, client=peer).get("/health", headers=headers)


# --- no trusted proxy configured: forwarded headers are noise ---------------

def test_unset_trusted_proxy_keeps_one_bucket_per_peer(production, monkeypatch):
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, CLIENT_A).status_code == 200
    # A different claimed client is still the same TCP peer, so it is the same
    # bucket and the one-request limit already applies.
    assert get(PROXY, CLIENT_B).status_code == 429
    assert set(http_server._RATE_BUCKETS) == {"10.0.0.1"}


def test_untrusted_direct_peer_cannot_forge_distinct_clients(production, monkeypatch):
    """The whole point: naming a proxy elsewhere must not trust this caller."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(DIRECT, CLIENT_A).status_code == 200
    assert get(DIRECT, CLIENT_B).status_code == 429
    assert set(http_server._RATE_BUCKETS) == {"203.0.113.5"}


# --- configured trusted proxy: each forwarded client gets its own bucket -----

def test_trusted_proxy_separates_forwarded_clients(production, monkeypatch):
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, CLIENT_A).status_code == 200
    assert get(PROXY, CLIENT_B).status_code == 200
    # Each client keeps its own window, so a repeat of the first is refused.
    assert get(PROXY, CLIENT_A).status_code == 429
    assert set(http_server._RATE_BUCKETS) == {CLIENT_A, CLIENT_B}


def test_leftmost_spoofed_hop_cannot_choose_the_identity(production, monkeypatch):
    """A caller-typed leftmost entry is discarded; the proxy's append wins."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, f"1.2.3.4, {CLIENT_A}").status_code == 200
    # Same real client, different forged prefix: same bucket.
    assert get(PROXY, f"5.6.7.8, {CLIENT_A}").status_code == 429
    # A genuinely different real client is not collateral damage.
    assert get(PROXY, f"1.2.3.4, {CLIENT_B}").status_code == 200
    assert set(http_server._RATE_BUCKETS) == {CLIENT_A, CLIENT_B}


def test_repeated_forwarded_header_lines_keep_the_appended_one(production, monkeypatch):
    """A proxy may append its own header line instead of extending the value.

    Reading only the first line would let the caller's line stand in for the
    real client, so every line has to be joined before the walk.
    """
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, ["1.2.3.4", CLIENT_A]).status_code == 200
    assert set(http_server._RATE_BUCKETS) == {CLIENT_A}
    # A different forged first line, same appended real client: one bucket.
    assert get(PROXY, ["5.6.7.8", CLIENT_A]).status_code == 429
    assert get(PROXY, ["5.6.7.8", CLIENT_B]).status_code == 200


@pytest.mark.parametrize(
    "forwarded",
    [
        "not-an-ip",
        "still-not-an-ip",
        "",
        "[::1]:5000",
        "1.2.3.4, ",
        CLIENT_A + "/24",
    ],
)
def test_malformed_forwarded_value_does_not_create_an_identity(
    production, monkeypatch, forwarded
):
    """Unparseable text must not become a bucket key of its own."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, forwarded).status_code == 200
    # Whatever the text was, it is not a bucket; the request fell back to the
    # proxy's own address. A second try exhausts that single shared bucket.
    assert set(http_server._RATE_BUCKETS) == {"10.0.0.1"}
    assert get(PROXY, forwarded).status_code == 429
    # A well-formed client still gets its own identity afterwards.
    assert get(PROXY, CLIENT_A).status_code == 200
    assert set(http_server._RATE_BUCKETS) == {"10.0.0.1", CLIENT_A}


def test_trusted_proxy_network_accepts_cidr_and_ipv6(production, monkeypatch):
    # The forwarded IPv6 client is deliberately outside the trusted /48: an
    # address inside it is another trusted hop and would be skipped.
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.0/8, 2001:db8:aaaa::/48")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(("10.1.2.3", 4000), CLIENT_A).status_code == 200
    assert get(("10.1.2.3", 4000), CLIENT_B).status_code == 200
    assert get(("2001:db8:aaaa::5", 4000), "2001:db8:1::7").status_code == 200
    assert set(http_server._RATE_BUCKETS) == {CLIENT_A, CLIENT_B, "2001:db8:1::7"}


def test_ipv4_mapped_forms_are_normalised(production, monkeypatch):
    """A dual-stack socket reports `::ffff:10.0.0.1` for a trusted IPv4 proxy."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(("::ffff:10.0.0.1", 4000), f"::ffff:{CLIENT_A}").status_code == 200
    # Both the peer and the hop were unwrapped, so the bucket is the real client.
    assert set(http_server._RATE_BUCKETS) == {CLIENT_A}


def test_all_trusted_chain_falls_back_to_the_peer(production, monkeypatch):
    """A chain of trusted hops names no client, so the peer stays the identity."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1, 10.0.0.2")
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    assert get(PROXY, "10.0.0.2").status_code == 200
    assert set(http_server._RATE_BUCKETS) == {"10.0.0.1"}
    assert get(PROXY, "10.0.0.2").status_code == 429


# --- a malformed setting fails closed instead of being ignored ---------------

@pytest.mark.parametrize(
    "value",
    ["not-an-ip", "10.0.0.1,", "10.0.0.1,,10.0.0.2", "*", "10.0.0.0/33", "proxy.example"],
)
def test_malformed_trusted_proxy_setting_refuses_the_request(
    production, monkeypatch, value
):
    """Silently dropping a bad entry would leave the operator unproxied."""
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, value)
    monkeypatch.setenv(ENV_RATE_PER_MINUTE, "1")
    response = get(PROXY, CLIENT_A)
    assert response.status_code == 503, response.text
    assert ENV_TRUSTED_PROXY_IPS in response.json()["error"]


# --- the setting is production-only -----------------------------------------

def test_developer_mode_ignores_the_trusted_proxy_setting(monkeypatch):
    """Developer mode still judges the peer, forwarded headers and all."""
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_API_TOKEN", raising=False)
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "127.0.0.1")
    response = TestClient(http_server.app, client=DIRECT, base_url="http://127.0.0.1").get(
        "/sources",
        headers={"Host": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 403


# --- the exported contract --------------------------------------------------

def test_trusted_proxy_setting_name_is_the_documented_one():
    assert bind.ENV_TRUSTED_PROXY_IPS == ENV_TRUSTED_PROXY_IPS


def test_cli_start_does_not_let_uvicorn_rewrite_the_peer(monkeypatch):
    """uvicorn's own middleware trusts 127.0.0.1 implicitly and takes the
    *leftmost* forwarded entry, which is the caller's to forge. The CLI start
    must hand the application the raw peer and let it apply its own trust set.
    """
    import uvicorn

    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    captured: dict[str, object] = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert http_server.main(["--host", "127.0.0.1", "--port", "8767"]) == 0
    assert captured["app"] is http_server.app
    assert captured["proxy_headers"] is False


# --- unit level: the identity rule itself ------------------------------------

def test_resolve_client_identity_picks_rightmost_untrusted_hop(monkeypatch):
    monkeypatch.setenv(ENV_TRUSTED_PROXY_IPS, "10.0.0.1")
    assert bind.resolve_client_identity("10.0.0.1", f"1.2.3.4, {CLIENT_A}") == CLIENT_A
    assert bind.resolve_client_identity("10.0.0.1", "10.0.0.1, 10.0.0.1") == "10.0.0.1"
    assert bind.resolve_client_identity("10.0.0.1", None) == "10.0.0.1"
    assert bind.resolve_client_identity("10.0.0.1", "garbage") == "10.0.0.1"
    # An untrusted peer is never read from the header.
    assert bind.resolve_client_identity("203.0.113.5", CLIENT_A) == "203.0.113.5"
    assert bind.resolve_client_identity(None, CLIENT_A) == "unknown"


def test_resolve_client_identity_without_configuration_is_the_peer(monkeypatch):
    monkeypatch.delenv(ENV_TRUSTED_PROXY_IPS, raising=False)
    assert bind.resolve_client_identity("203.0.113.5", CLIENT_A) == "203.0.113.5"
    assert bind.resolve_client_identity(None, None) == "unknown"
