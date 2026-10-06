"""Request policy for the local browser interface.

The UI mounts the existing, unauthenticated developer HTTP app under its own
browser-session layer. That layer adds exactly three things the developer app
does not have, and nothing it removes:

1. **A loopback-only, UI-origin-only gate.** Every request must arrive with a
   loopback ``Host`` and a provably loopback peer (reusing the shared
   :func:`~textflowkit.core.bind.developer_request_refusal`). Additionally, a
   request carrying an ``Origin`` header must name *this UI's own* origin - the
   scheme/host/port the page was actually served from. The shared guard only
   accepts a loopback origin; the UI is stricter and requires its exact origin,
   so a second loopback service on another port cannot drive this one with a
   cross-origin form post. The UI never enables CORS and never sets
   ``Access-Control-Allow-Origin``: a browser therefore cannot read a response
   cross-origin, and a simple cross-origin POST is refused here.

2. **A per-session capability header on mutators.** The UI refuses to bind
   beyond loopback even when ``TEXTFLOWKIT_ALLOW_REMOTE=1`` is set: the remote
   opt-in widens the *developer* launcher, not this one, so the UI cannot be
   exposed by an environment variable it does not read for that purpose. With
   the surface pinned to loopback, a browser is the only plausible client - and
   a browser attached by *any* page can still be made to POST to loopback. So
   every state-changing request must also carry a token that only a page this
   server actually served could hold, in a custom header. A custom header cannot
   be set by a cross-origin ``<form>``, and reading it across origins requires
   CORS, which is off. This is a session capability, not authentication.

3. **A per-process session token** handed to the page in its bootstrap HTML and
   never placed in a URL, a cookie, or a log line. The page reads it from a
   ``<meta>`` tag and echoes it in the header above.

None of this replaces the pipeline's own guards; the mounted developer app keeps
its per-request loopback checks unchanged beneath this layer.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from textflowkit.core.bind import (
    is_loopback_authority,
    is_loopback_origin,
    peer_locality,
)

#: The custom header a browser page must echo on every mutating request. A
#: browser will not let a cross-origin page set this without a CORS preflight,
#: and the UI answers no preflight, so its presence proves the caller is *our*
#: page rather than any other local page firing a simple request at us.
CAPABILITY_HEADER = "x-textflowkit-ui-capability"

#: Methods that change state. A GET/HEAD/OPTIONS is never gated on the
#: capability header - the app has no state-changing GET, and gating reads would
#: break the plain ``<a>``/navigations the workspace is built from.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def new_session_token() -> str:
    """A fresh, high-entropy capability token for one UI process.

    ``token_urlsafe(32)`` is 256 bits of entropy in a URL-safe alphabet, so it
    is safe to embed in an HTML attribute and cannot be guessed or brute-forced
    from another local page.
    """
    return secrets.token_urlsafe(32)


def ui_origin(host: str, port: int) -> str:
    """The exact origin this UI is served from, e.g. ``http://127.0.0.1:8756``.

    Built from the loopback bind address, never from a request header, so it is
    the origin the server *is*, not one a caller *claims*.
    """
    return f"http://{host}:{port}"


def _origin_matches_ui(origin: str | None, expected: str) -> bool:
    """Whether an ``Origin`` header names exactly this UI's own origin.

    Comparison is on the normalized (scheme, host, port) triple, lowercased, so
    ``http://127.0.0.1:8756`` and ``HTTP://127.0.0.1:8756/`` agree while a
    different port, host, or scheme does not. A missing/opaque origin is not a
    match - callers decide separately whether an absent origin is acceptable for
    a given request.
    """
    if not origin:
        return False
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    try:
        expected_parts = urlsplit(expected)
    except ValueError:  # pragma: no cover - expected is built by us
        return False
    if parts.scheme.lower() != expected_parts.scheme.lower():
        return False
    if parts.hostname.lower() != (expected_parts.hostname or "").lower():
        return False
    # Normalize the implicit default port so ":80"/":443" match an origin that
    # omits it, while any other explicit port must agree.
    def _port(p, scheme):
        if p.port is not None:
            return p.port
        return 443 if scheme == "https" else 80

    return _port(parts, parts.scheme.lower()) == _port(
        expected_parts, expected_parts.scheme.lower()
    )


def request_refusal(
    *,
    host: str | None,
    origin: str | None,
    peer: str | None,
    expected_origin: str,
    method: str,
    supplied_capability: str | None,
    session_token: str,
    is_asset_request: bool = False,
) -> str | None:
    """Reason to refuse a UI request, or ``None`` to allow it.

    Order matters - the cheapest and most general check runs first:

    1. **Loopback peer/Host** via the shared developer guard, which is
       fail-closed on the peer. A missing or non-IP peer is refused, so an
       in-process harness that reports no peer must declare a loopback one (as
       the tests do) rather than being assumed local.
    2. **Exact UI origin** when an ``Origin`` header is present. A browser sends
       one on every cross-origin request and on same-origin POSTs; a plain
       same-origin navigation GET may omit it, which is why an absent origin is
       allowed here and left to the capability header to cover mutators. An
       origin that is present but not *ours* is always refused.
    3. **Capability header** on mutators. Constant-time compared against the
       session token, so a wrong token leaks nothing by timing.

    ``is_asset_request`` is for the one class of request that legitimately has no
    capability: the HTML shell itself and its static assets, which the browser
    fetches before any script runs. Those are GETs and are still covered by 1
    and 2; the flag documents that a GET without a capability header is not an
    oversight. It never relaxes 1 or 2.
    """
    # 1. Loopback Host and peer. This deliberately does NOT go through the
    #    shared `developer_request_refusal`: that guard honours
    #    TEXTFLOWKIT_ALLOW_REMOTE as its single opt-in, and the UI must never be
    #    widened by that variable - it widens the developer launcher, not this
    #    browser-facing surface. The checks are the *same rules* (loopback Host,
    #    loopback Origin, provably-loopback peer), applied unconditionally.
    if not is_loopback_authority(host or ""):
        return (
            f"refusing Host '{host}': the local UI serves loopback names and "
            "loopback addresses only (127.0.0.1, localhost, [::1])"
        )
    if origin and not is_loopback_origin(origin):
        return (
            f"refusing cross-origin request from '{origin}': the local UI is "
            "loopback-only and unauthenticated"
        )
    locality = peer_locality(peer)
    if locality is None:
        return (
            "refusing request whose client address cannot be determined (peer "
            f"'{peer}'): the local UI serves only a provably loopback peer"
        )
    if locality is False:
        return (
            f"refusing request from non-loopback peer '{peer}': the local UI is "
            "loopback-only and unauthenticated"
        )

    # 2. Origin, when present, must be exactly this UI's own. The loopback check
    #    above already rejected a foreign origin; this rejects a *loopback* but
    #    wrong origin (a second local service on another port).
    if origin and not _origin_matches_ui(origin, expected_origin):
        return (
            f"refusing Origin '{origin}': the local UI answers only its own "
            f"origin ({expected_origin})"
        )

    # 3. Capability on mutators.
    if method.upper() in MUTATING_METHODS and (
        not supplied_capability or not hmac.compare_digest(supplied_capability, session_token)
    ):
        return (
            "missing or invalid UI capability header: a state-changing "
            "request must come from this UI's own page"
        )
    return None


def peer_is_loopback(peer: str | None) -> bool:
    """Whether a peer address is provably loopback (used by the launch check)."""
    return peer_locality(peer) is True
