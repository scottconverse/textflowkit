"""Bind-safety guard for the HTTP surfaces.

Developer-mode HTTP and Streamable-HTTP MCP are unauthenticated. The JSON HTTP
adapter has a separate opt-in production Bearer-token profile. The dangerous
configuration is an unauthenticated, file-writing, network-fetching API bound
beyond loopback.

This guard does not add auth. It refuses that specific configuration unless the
operator says so explicitly, so the failure mode is a clear startup error rather
than a silently exposed service.

The same policy answers the per-request question. A startup bind check cannot
cover an app started directly through an ASGI server (`uvicorn
textflowkit.adapters.http_server:app --host 0.0.0.0`), and it cannot see the
`Host` a browser was pointed at. `developer_request_refusal()` closes both: a
loopback-only `Host` allowlist stops a DNS name that resolves to loopback
(rebinding), and the peer address stops a remote client on a widened bind.
Forwarded headers are deliberately not consulted there - they are
attacker-controlled unless a trusted proxy is known, and developer mode never
knows one.

`resolve_client_identity()` is the one place that may read them, and only for
the production rate limit. It reads `X-Forwarded-For` only when the TCP peer is
an address named in `TEXTFLOWKIT_TRUSTED_PROXY_IPS`, and then counts the
rightmost hop that is not itself a trusted proxy. With that setting unset it
behaves as before: the peer address and nothing else.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

ENV_ALLOW_REMOTE = "TEXTFLOWKIT_ALLOW_REMOTE"
ENV_TRUSTED_PROXY_IPS = "TEXTFLOWKIT_TRUSTED_PROXY_IPS"

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


# --- per-request policy ---------------------------------------------------

def _is_loopback_literal(value: str) -> bool:
    """True if `value` is an IP literal meaning loopback.

    IPv4-mapped IPv6 is judged by its embedded address: a dual-stack socket
    reports an IPv4 loopback client as `::ffff:127.0.0.1`, which is local.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return False


def authority_hostname(authority: str) -> str:
    """Hostname part of a Host header or URL authority, lowercased.

    Strips the port and IPv6 brackets: `127.0.0.1:8767` -> `127.0.0.1`,
    `[::1]:8767` -> `::1`.
    """
    value = (authority or "").strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].lower() if end > 0 else ""
    if value.count(":") == 1:
        name, _, port = value.partition(":")
        if port.isdecimal():
            return name.lower()
    return value.lower()


def is_loopback_authority(authority: str) -> bool:
    """True if a Host-style authority names loopback (loopback name or address).

    Hostnames that are not `localhost` are not loopback: they may resolve
    anywhere, so they are not provably local.
    """
    name = authority_hostname(authority)
    if not name:
        return False
    if name in _LOOPBACK_NAMES:
        return True
    return _is_loopback_literal(name)


def is_loopback_origin(origin: str) -> bool:
    """True if an Origin header is an http(s) loopback origin.

    Opaque (`null`) and non-http(s) origins are not loopback.
    """
    raw = (origin or "").strip()
    if not raw:
        return False
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not hostname:
        return False
    return is_loopback_authority(hostname)


def peer_locality(peer_host: str | None) -> bool | None:
    """True if the peer is loopback, False if not, None if it cannot be judged.

    None covers a missing peer address and peers that are not IP literals
    (in-process ASGI test clients). Real servers report the peer IP, so a remote
    client on a widened bind is judged False and refused.
    """
    if not peer_host:
        return None
    candidate = peer_host.strip().strip("[]")
    if _is_loopback_literal(candidate):
        return True
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return False


def developer_request_refusal(
    *, host: str | None, origin: str | None, peer: str | None
) -> str | None:
    """Reason to refuse a developer-mode request, or None to allow it.

    `remote_allowed()` is the single opt-in: with it set, an operator may front
    the service with a gateway, so the loopback allowlist does not apply. The
    production profile does not use this path at all - it authenticates with the
    Bearer token.
    """
    if remote_allowed():
        return None
    if origin and not is_loopback_origin(origin):
        return (
            f"refusing cross-origin request from '{origin}': developer mode is "
            "unauthenticated and loopback-only"
        )
    if not is_loopback_authority(host or ""):
        return (
            f"refusing Host '{host}': developer mode is unauthenticated, so only "
            "loopback names and loopback addresses are served (127.0.0.1, "
            "localhost, [::1]); this blocks a DNS name that resolves to loopback"
        )
    if peer_locality(peer) is False:
        return (
            f"refusing request from non-loopback peer '{peer}': developer mode is "
            "unauthenticated and loopback-only. Bind to 127.0.0.1, or pass "
            f"--allow-remote (or set {ENV_ALLOW_REMOTE}=1) if you have a gateway "
            "in front of it"
        )
    return None


# --- production client identity -------------------------------------------

class TrustedProxyConfigError(ValueError):
    """ENV_TRUSTED_PROXY_IPS cannot be used as written."""


def _as_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse one address literal, or None if it is not one.

    An IPv4-mapped IPv6 form is unwrapped to the IPv4 address it names: a
    dual-stack socket reports an IPv4 peer as `::ffff:127.0.0.1`, and a proxy
    built on one may forward the same form, so `10.0.0.1` and `::ffff:10.0.0.1`
    have to compare equal and count as one client.
    """
    candidate = value.strip().strip("[]")
    if not candidate:
        return None
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def trusted_proxy_networks() -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Networks named by ENV_TRUSTED_PROXY_IPS; empty when it is unset.

    Comma-separated IP literals and CIDR networks, IPv4 or IPv6; a bare address
    becomes a /32 or /128. Anything else - a hostname, a blank entry, `*`, a bad
    prefix - is refused rather than skipped. Skipping would leave an operator who
    thinks a proxy is trusted silently without one, which is an identity hole
    that no request ever reports.

    Only an *absent* variable means "no trusted proxy". A variable that is set
    but empty is a broken setting, not an unset one: reading it as unset would
    leave an operator who meant to configure a proxy counting every one of its
    clients as a single caller, with nothing to show for it.
    """
    raw = os.environ.get(ENV_TRUSTED_PROXY_IPS)
    if raw is None:
        return ()
    if not raw.strip():
        raise TrustedProxyConfigError(
            f"{ENV_TRUSTED_PROXY_IPS} is set but empty. Unset it to stop trusting "
            "forwarded headers, or name the proxy addresses."
        )
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        entry = item.strip()
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise TrustedProxyConfigError(
                f"{ENV_TRUSTED_PROXY_IPS} entry '{entry}' is not an IP address or "
                "CIDR network"
            ) from None
    return tuple(networks)


def resolve_client_identity(peer: str | None, forwarded_for: str | None) -> str:
    """The address a production rate limit counts this request against.

    With no trusted proxy configured this is exactly the TCP peer and
    `forwarded_for` is ignored entirely. With one configured, the forwarded
    chain is read only when the peer is itself a trusted proxy, and the identity
    is the *rightmost* hop that is not a trusted proxy - each proxy appends the
    address it saw, so everything to the left of that entry was typed by the
    caller and cannot be trusted.

    Anything unparseable stops the walk and falls back to the peer. That is the
    fail-closed direction: a caller cannot invent a bucket key, a chain that
    cannot be read is charged to the proxy rather than to a client, and a
    forwarded value never survives as an identity unless it is a real address.

    This is production-only policy. Developer mode never calls it, and must not:
    there the peer itself is the security decision, in
    `developer_request_refusal()`.
    """
    proxies = trusted_proxy_networks()
    peer_address = _as_address(peer or "")
    fallback = (peer or "").strip() or "unknown"
    if not proxies or peer_address is None:
        return fallback
    if not any(peer_address in network for network in proxies):
        return fallback
    for hop in reversed((forwarded_for or "").split(",")):
        hop_address = _as_address(hop)
        if hop_address is None:
            break
        if not any(hop_address in network for network in proxies):
            return str(hop_address)
    return fallback
