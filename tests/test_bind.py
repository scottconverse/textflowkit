"""Bind-safety guard.

The HTTP surfaces ship no authentication, so the guard's job is to refuse the
one dangerous configuration - binding beyond loopback - unless it is explicit.
"""

from __future__ import annotations

import pytest

from textflowkit.core.bind import (
    ENV_ALLOW_REMOTE,
    UnsafeBindError,
    check_bind_safety,
    is_loopback_host,
    remote_allowed,
)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "[::1]", "localhost", "localhost.localdomain", "127.0.0.2"],
)
def test_loopback_hosts_recognised(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.5", "10.0.0.1", "example.com", "", "::"],
)
def test_non_loopback_hosts_recognised(host):
    assert is_loopback_host(host) is False


def test_loopback_bind_allowed(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    check_bind_safety("127.0.0.1")          # does not raise
    check_bind_safety("localhost")


def test_non_loopback_bind_refused(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    with pytest.raises(UnsafeBindError) as exc:
        check_bind_safety("0.0.0.0")
    msg = str(exc.value)
    assert "unauthenticated" in msg
    assert "--allow-remote" in msg
    assert ENV_ALLOW_REMOTE in msg


def test_non_loopback_allowed_explicitly(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    check_bind_safety("0.0.0.0", allow_remote=True)   # does not raise


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_enables_remote(monkeypatch, value):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, value)
    assert remote_allowed() is True
    check_bind_safety("0.0.0.0")


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_env_does_not_enable_remote(monkeypatch, value):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, value)
    assert remote_allowed() is False
    with pytest.raises(UnsafeBindError):
        check_bind_safety("0.0.0.0")


def test_explicit_flag_beats_env(monkeypatch):
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    # explicit False must win over a permissive env
    with pytest.raises(UnsafeBindError):
        check_bind_safety("0.0.0.0", allow_remote=False)


# --- the adapters actually enforce it -------------------------------------

def test_http_cli_refuses_remote_bind(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    from textflowkit.adapters.http_server import main

    assert main(["--host", "0.0.0.0"]) == 2     # refused, no bind attempted


def test_mcp_cli_refuses_remote_http_bind(monkeypatch):
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    from textflowkit.adapters.mcp_server import main

    assert main(["--transport", "http", "--host", "0.0.0.0"]) == 2


def test_mcp_stdio_ignores_bind_guard(monkeypatch):
    """stdio has no bind address, so the guard must not interfere."""
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    import textflowkit.adapters.mcp_server as m

    called = {}
    monkeypatch.setattr(m, "run_stdio", lambda: called.setdefault("stdio", True))
    assert m.main(["--transport", "stdio"]) == 0
    assert called.get("stdio") is True
