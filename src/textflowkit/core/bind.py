"""Bind-safety guard for the HTTP surfaces.

Developer-mode HTTP and Streamable-HTTP MCP are unauthenticated. The JSON HTTP
adapter has a separate opt-in production Bearer-token profile. The dangerous
configuration is an unauthenticated, file-writing, network-fetching API bound
beyond loopback.

This guard does not add auth. It refuses that specific configuration unless the
operator says so explicitly, so the failure mode is a clear startup error rather
than a silently exposed service.
"""

from __future__ import annotations

import ipaddress
import os

ENV_ALLOW_REMOTE = "TEXTFLOWKIT_ALLOW_REMOTE"

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "::1"})


class UnsafeBindError(ValueError):
    """Raised when a bind address would expose a service without explicit opt-in."""


def is_loopback_host(host: str) -> bool:
    """True if `host` is loopback (or a loopback name).

    A bare hostname that is not `localhost` is treated as non-loopback: it may
    resolve anywhere, and we cannot prove otherwise cheaply.
    """
    if not host:
        return False
    candidate = host.strip().strip("[]").lower()
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def remote_allowed(explicit: bool | None = None) -> bool:
    """Whether exposing the service beyond loopback was explicitly permitted."""
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_ALLOW_REMOTE, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def check_bind_safety(host: str, *, allow_remote: bool | None = None) -> None:
    """Refuse a non-loopback bind unless it was explicitly requested.

    Raises `UnsafeBindError` with an actionable message.
    """
    if is_loopback_host(host):
        return
    if remote_allowed(allow_remote):
        return
    raise UnsafeBindError(
        f"refusing to bind to '{host}': developer HTTP/MCP is unauthenticated, "
        "so binding beyond loopback would expose it on the network. "
        "Bind to 127.0.0.1, or pass --allow-remote (or set "
        f"{ENV_ALLOW_REMOTE}=1) if you have a gateway in front of it."
    )
