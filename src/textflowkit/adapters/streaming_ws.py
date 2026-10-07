"""Live-streaming WebSocket adapter: the transport around the shared core.

The core (:mod:`textflowkit.core.streaming`) is transport-neutral: PCM in, events
out, no bytes on a wire. This module is the wire. It adds exactly one endpoint to
a host application - ``/stream`` on the developer HTTP app, ``/api/stream`` when
that app is mounted under the local UI - and speaks one small framing over it:

    client -> server   {"type": "start", "version": 1, "format": "pcm_s16le",
                         "sample_rate": 16000, "channels": 1, "language": "en",
                         "token": "..." | "capability": "..."}
                       <binary frame>            # s16le mono 16 kHz, <= 32000 B
                       {"type": "finish"}
                       {"type": "cancel"}
    server -> client   {"type": "ready", "session": "<id>", "limits": {...}}
                       {"type": "event", ...}    # one StreamEvent.to_dict()
                       {"type": "final", ...}    # the finish event + transcript
                       {"type": "error", "code": ..., "message": ...}  # terminal

Why a middleware and not just a route handler: HTTP middleware - the developer
app's ``@app.middleware("http")`` guard and the UI's ``session_guard`` - does
**not** run for a WebSocket handshake. Starlette dispatches ``websocket`` scope
through the middleware stack too, but an ``@app.middleware("http")`` function is
only invoked for ``http`` scopes, so every protection those middlewares carry is
absent from an upgrade. This module supplies a *pure ASGI* middleware (it sees the
raw scope and can reject a handshake before the app allocates anything) that
re-applies the same loopback/peer/origin rules and, for the UI, publishes the UI's
own origin and capability token into the scope so the route handler can enforce
them without the HTTP session layer.

Everything here is opt-in. ``TEXTFLOWKIT_STREAMING=1`` turns it on; with it unset
the endpoint is not added at all, so a disabled server allocates no session and
serves no example asset. It is loopback-only regardless of
``TEXTFLOWKIT_ALLOW_REMOTE`` - the remote opt-in widens the HTTP developer
launcher, never a WebSocket that carries microphone audio into a native engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from textflowkit.core.bind import is_loopback_authority, peer_locality
from textflowkit.core.streaming import (
    CANCEL_DEADLINE_S,
    CHUNK_DEADLINE_S,
    FINISH_DEADLINE_S,
    LANGUAGES,
    MAX_CHUNK_BYTES,
    MAX_QUEUE_SECONDS,
    SAMPLE_RATE,
    START_DEADLINE_S,
    StreamingError,
    StreamingSession,
    WorkerProcessError,
)
from textflowkit.core.streaming_events import StreamingProtocolError

__all__ = [
    "ENV_STREAMING",
    "ENV_STREAMING_ORIGINS",
    "HANDSHAKE_TIMEOUT_S",
    "IDLE_TIMEOUT_S",
    "MAX_CONNECTIONS",
    "MAX_CONTROL_BYTES",
    "MAX_HANDSHAKE_CONCURRENCY",
    "PROTOCOL_VERSION",
    "SEND_TIMEOUT_S",
    "SLOW_READER_TIMEOUT_S",
    "StreamingConfig",
    "StreamingRoute",
    "WebSocketScopeGuard",
    "install_streaming",
    "streaming_enabled",
    "streaming_origins",
]

#: Opt-in switch. Streaming is off unless this is exactly "1"-ish truthy.
ENV_STREAMING = "TEXTFLOWKIT_STREAMING"
#: Comma-separated extra exact origins allowed to open a stream (a "God Eye
#: View" app on a different origin). Each must be an http(s) origin with a host
#: and no wildcard, credentials, or path. Empty by default.
ENV_STREAMING_ORIGINS = "TEXTFLOWKIT_STREAMING_ORIGINS"
#: The one protocol version this adapter speaks.
PROTOCOL_VERSION = 1
#: How long a connection may take to send its ``start`` and authenticate.
HANDSHAKE_TIMEOUT_S = 5.0
#: How long a live session may go with no client message before it is closed.
IDLE_TIMEOUT_S = 60.0
#: How long the pump waits for the core to produce the next event before looping
#: to check for a disconnect or a control command.
SLOW_READER_TIMEOUT_S = 5.0
#: Maximum bytes of a single JSON control frame (start/controls). Audio arrives
#: as binary, not JSON, so this bounds the text side only.
MAX_CONTROL_BYTES = 8192
#: Maximum concurrent connections *in the pre-authentication handshake*. A cheap
#: bound so a peer cannot open thousands of unauthenticated sockets before any
#: work is done; past it a new connection is refused immediately.
MAX_HANDSHAKE_CONCURRENCY = 8
#: Maximum concurrent authenticated streaming connections held at once. The core
#: already caps *sessions* (default one); this bounds the transport's own
#: connections so a peer that authenticates successfully still cannot hold an
#: unbounded number of half-open streams around a single session.
MAX_CONNECTIONS = 16
#: How long any single ``send`` may block before the peer is judged to have
#: stopped reading. A send that exceeds it aborts the connection and cancels the
#: session, rather than letting the event loop block on a stalled socket.
SEND_TIMEOUT_S = 5.0
#: Minimum seconds between a peer's control messages, so many tiny JSON frames
#: cannot busy-loop the handler. Audio (binary) is not rate-limited here - the
#: core's byte queue is the audio bound.
_MIN_CONTROL_INTERVAL_S = 0.002
#: Maximum socket messages buffered between the receiver task and the handler. A
#: bounded queue keeps a peer from forcing unbounded memory through the transport's
#: own inbox; the receiver waits for a slot rather than dropping a frame, so the
#: backpressure is real (the TCP window fills) and no message is lost.
MAX_INBOX = 64
#: How long the pump waits for the next socket message before polling the core, in
#: seconds. Short enough that the core is advanced promptly, long enough to avoid a
#: busy loop.
_POLL_INTERVAL_S = 0.02
#: Timeout for one bounded core ``read_event`` poll, in seconds. The read runs in a
#: worker thread; this bounds how long the loop waits on it before looping.
_CORE_POLL_S = 0.05


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def streaming_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether live streaming is opted into. Off unless ``TEXTFLOWKIT_STREAMING=1``."""
    source = os.environ if env is None else env
    return _truthy(source.get(ENV_STREAMING))


def streaming_origins(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """The extra exact origins allowed to open a stream, validated.

    ``TEXTFLOWKIT_STREAMING_ORIGINS`` is a comma-separated list of exact origins
    (scheme://host[:port]) for a separate application - a generic "God Eye View"
    dashboard on its own origin - allowed in addition to the host's own origin.
    Each entry is validated: it must be an ``http``/``https`` origin with a host
    and no wildcard, no embedded credentials, and no path. A malformed entry is a
    hard configuration error - silently dropping it would leave an operator
    believing an origin is allowed when it is not.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_STREAMING_ORIGINS, "")
    if not raw.strip():
        return ()
    origins: list[str] = []
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        origins.append(_validate_origin(item))
    return tuple(origins)


def _parse_origin(origin: str) -> tuple[str, str, int] | None:
    """Split an origin into ``(scheme, host, port)``, or ``None`` if malformed.

    Deliberately strict: an origin is ``scheme://host[:port]`` and nothing else -
    no userinfo, no path (beyond a bare trailing slash), no query, no fragment, no
    wildcard. A malformed origin returns ``None`` rather than raising, so a
    caller decides whether that is a configuration error (a bad allowlist entry)
    or simply "not allowed" (an attacker-supplied ``Origin`` header). The port is
    read through :attr:`urlsplit.port`, which itself raises ``ValueError`` on an
    out-of-range or non-numeric port; that is caught here and normalised to
    ``None`` so a hostile ``Origin`` cannot raise out of the guard.
    """
    from urllib.parse import urlsplit

    if not origin or "*" in origin:
        return None
    try:
        parts = urlsplit(origin.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    if not parts.hostname:
        return None
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _validate_origin(origin: str) -> str:
    parsed = _parse_origin(origin)
    if parsed is None:
        # The *rejected value* is never echoed into an error: a misconfigured
        # origin could be a secret-bearing URL, and a config error is logged.
        raise StreamingProtocolError(
            "a streaming origin must be an exact http(s) origin - scheme://host[:port], "
            "no wildcard, no credentials, no path, query, or fragment"
        )
    scheme, host, port = parsed
    return _canonical_origin(scheme, host, port)


@dataclass
class StreamingConfig:
    """Resolved streaming policy for one host application. Fails closed.

    There is **no anonymous path**. A stream is served only to a caller that
    proves one of two things:

    - a **browser** on the host's own exact ``own_origin`` holding the UI
      ``ui_token`` (a page the UI itself served). The UI is never widened by the
      cross-app allowlist; a browser on any other origin is refused;
    - a **browser on an explicitly allowed cross origin** (a generic app such as
      a "God Eye View" dashboard) or a **non-browser client** (no ``Origin``)
      holding the ``api_token``. Cross-origin and headless clients authenticate by
      token, never by capability - a capability is bound to the UI's own origin.

    A non-browser caller with no token is refused, an empty token never matches,
    and so a misconfigured server (no token) serves no stream rather than an open
    one.

    ``own_origin`` is the UI's exact origin (``None`` on the standalone app, which
    is not a page origin). ``allowed_origins`` are extra *exact* origins from
    :data:`ENV_STREAMING_ORIGINS`, honoured for token-authenticated cross-origin
    clients. ``ui_token`` is the UI's per-process capability, matched against a
    ``capability`` field in the ``start`` message; it is ``None`` on the standalone
    app. ``api_token`` is the Bearer token a non-browser or cross-origin client
    must present.
    """

    own_origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    ui_token: str | None = None
    require_api_token: bool = True
    api_token: str | None = None
    #: A worker factory injected for tests/dev; production leaves it ``None`` so a
    #: session launches the real owned subprocess.
    worker_factory: Any = None

    def is_own_origin(self, origin: str | None) -> bool:
        """Whether ``origin`` is exactly this host's own origin - never a widening."""
        if self.own_origin is None or origin is None:
            return False
        normalized = _normalize_origin(origin)
        return normalized is not None and normalized == _normalize_origin(self.own_origin)

    def is_allowed_cross_origin(self, origin: str | None) -> bool:
        """Whether ``origin`` is an explicitly configured *cross* origin.

        The host's own origin is **not** matched here: it is handled by
        :meth:`is_own_origin`, and a capability-authenticated browser on it must
        never be graded as an allowlist client. The allowlist is exact-match only.
        """
        if origin is None:
            return False
        normalized = _normalize_origin(origin)
        return normalized is not None and normalized in {
            _normalize_origin(o) for o in self.allowed_origins
        }

    def origin_allowed(self, origin: str | None) -> bool:
        """Whether a browser ``Origin`` may *open* a stream at all (before auth).

        A non-browser client (no ``Origin``) is allowed here and authenticated by
        token instead. A browser origin must be exactly the host's own origin or
        one configured in :data:`ENV_STREAMING_ORIGINS` - never a substring or
        wildcard match, and the host's own origin is never widened by the
        cross-app allowlist. This is a *gate before authentication*; the caller is
        still authenticated separately.
        """
        if origin is None:
            return True
        return self.is_own_origin(origin) or self.is_allowed_cross_origin(origin)

    def capability_ok(self, supplied: str | None) -> bool:
        """Whether a supplied start-message capability matches the UI's token.

        ``supplied`` comes straight from parsed JSON, so it may be any type at all -
        a list, a dict, a number, a non-ASCII string. ``hmac.compare_digest`` raises
        ``TypeError`` on anything that is not a same-kind ASCII-only ``bytes``/``str``.
        A credential that is not an ASCII string can never equal the token, so it is
        refused here rather than raising out of the guard and leaking a traceback.
        """
        if not self.ui_token:
            return False
        if not _is_ascii_credential(supplied):
            return False
        return bool(supplied) and hmac.compare_digest(supplied, self.ui_token)

    def token_ok(self, supplied: str | None) -> bool:
        """Whether a supplied start-message token matches the API Bearer token.

        Constant-time; a missing, empty, or unconfigured token never matches. A
        credential of the wrong type (or a non-ASCII string) is refused without ever
        reaching ``hmac.compare_digest``, so a malformed ``start`` can never raise a
        ``TypeError``/``UnicodeError`` out of the authentication path. Never logs the
        supplied value.
        """
        if not self.api_token:
            return False
        if not _is_ascii_credential(supplied):
            return False
        return bool(supplied) and hmac.compare_digest(supplied, self.api_token)


def _canonical_origin(scheme: str, host: str, port: int) -> str:
    """The one canonical spelling of an origin, so equality is exact string equality."""
    return f"{scheme}://{host}:{port}"


def _is_ascii_credential(value: Any) -> bool:
    """Whether a supplied credential can be compared with :func:`hmac.compare_digest`.

    The value comes from parsed JSON, so it may be any type: a list, a dict, a number,
    or a string with non-ASCII characters. ``hmac.compare_digest`` requires two
    ASCII-only ``str`` (or two ``bytes``) and raises ``TypeError`` otherwise; a
    non-ASCII ``str`` raises too. Anything that is not a plain ASCII ``str`` is not a
    token and is refused here, so no malformed credential can raise out of the guard.
    """
    return isinstance(value, str) and value.isascii()


def _normalize_origin(origin: str | None) -> str | None:
    """Canonicalise an origin for exact comparison, or ``None`` if it is malformed."""
    parsed = _parse_origin(origin or "")
    if parsed is None:
        return None
    return _canonical_origin(*parsed)


# --- pure ASGI websocket guard ---------------------------------------------


class _ConnectionLease:
    """One accepted connection's own slot in the :class:`_ConnectionLimiter`.

    The counters are process-wide, but *ownership* is per connection: this object
    remembers which phase *it* holds and releases exactly that, exactly once. A
    connection cannot release another's pre-auth slot, because it never touches a
    bare counter - it only decrements the phase this lease still owns.
    """

    __slots__ = ("_holds_handshake", "_holds_total", "_limiter")

    def __init__(self, limiter: _ConnectionLimiter):
        self._limiter = limiter
        self._holds_handshake = True
        self._holds_total = True

    async def finish_handshake(self) -> None:
        """Move *this* connection out of the pre-auth phase. Idempotent on the lease."""
        if not self._holds_handshake:
            return
        self._holds_handshake = False
        async with self._limiter._lock:
            if self._limiter._handshaking > 0:
                self._limiter._handshaking -= 1

    async def release(self) -> None:
        """Release this connection's slot(s), whoever opened them, exactly once each."""
        async with self._limiter._lock:
            if self._holds_handshake:
                self._holds_handshake = False
                if self._limiter._handshaking > 0:
                    self._limiter._handshaking -= 1
            if self._holds_total:
                self._holds_total = False
                if self._limiter._total > 0:
                    self._limiter._total -= 1


class _ConnectionLimiter:
    """A process-wide cap on live streaming connections, split by phase.

    Two bounds, because they defend different things:

    - ``handshake`` bounds sockets that have been accepted but have **not yet
      authenticated**. This is the cheap bound on pre-auth work: a peer cannot
      hold thousands of unauthenticated upgrades open. It is released the moment a
      connection authenticates (or is refused), so it does not double as a
      connection cap.
    - ``total`` bounds authenticated connections - sessions and the short window
      around them - so a client that authenticates successfully still cannot open
      unbounded concurrent streams. Past it a new connection is refused, bounded
      and explicit rather than queued without limit.

    Acquisition returns a :class:`_ConnectionLease`; every release goes through that
    lease, so a connection releases only the phases it actually holds, once each.
    """

    def __init__(
        self,
        handshake_limit: int = MAX_HANDSHAKE_CONCURRENCY,
        total_limit: int = MAX_CONNECTIONS,
    ):
        self._handshake_limit = handshake_limit
        self._total_limit = total_limit
        self._lock = asyncio.Lock()
        self._handshaking = 0
        self._total = 0

    async def acquire_handshake(self) -> _ConnectionLease | None:
        """Claim a pre-auth slot, or ``None`` when a bound refuses the connection."""
        async with self._lock:
            if self._handshaking >= self._handshake_limit or self._total >= self._total_limit:
                return None
            self._handshaking += 1
            self._total += 1
            return _ConnectionLease(self)


_connection_limiter = _ConnectionLimiter()


@dataclass
class StreamingScopeContext:
    """What a websocket guard publishes for the route handler to enforce.

    The handler reads this from ``scope["state"]`` (a plain dict Starlette carries
    through). It records the *decisions* the guard made - origin allowed, the
    UI token, whether an API token is required - so the handler does not re-derive
    the policy and cannot drift from it.
    """

    config: StreamingConfig
    path: str
    #: This connection's own slot in the limiter; ``finish_handshake`` moves it out of
    #: the pre-auth phase. Both are idempotent on the lease, and the lease releases
    #: only the phases *this* connection holds, so a release never touches another
    #: connection's count.
    lease: _ConnectionLease | None = None
    #: Called exactly once by the handler when the connection leaves the pre-auth
    #: phase (authenticated or refused). Idempotent on the lease side.
    finish_handshake: Any = None


class WebSocketScopeGuard:
    """Pure ASGI middleware applying the streaming policy to ``websocket`` scopes.

    It runs for the raw ASGI scope, so it sees a WebSocket handshake the HTTP
    middlewares never see. It:

    - refuses any scope that is not the configured stream path (passes others
      through untouched, so mounting this in a host app changes nothing else);
    - requires a loopback ``Host`` and a *provably loopback* peer, always - even
      with ``TEXTFLOWKIT_ALLOW_REMOTE`` set;
    - rejects a browser ``Origin`` that is not the host's own origin or an
      explicitly configured allowed origin, **before** the app allocates a
      session;
    - bounds concurrent *pre-authentication* handshakes, and separately the total
      number of authenticated connections;
    - publishes a :class:`StreamingScopeContext` into the scope for the handler,
      and a hand-off callback the handler calls once the connection authenticates
      (or is refused) so the pre-auth bound is released without ever double-
      releasing it.

    A refusal closes the socket with a policy code without ever constructing a
    :class:`StreamingSession`, so a rejected handshake allocates no session slot
    and touches no asset.
    """

    def __init__(self, app: Any, *, path: str, config: StreamingConfig):
        self._app = app
        self._path = path
        self._config = config

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "websocket" or scope.get("path") != self._path:
            await self._app(scope, receive, send)
            return

        refusal = self._handshake_refusal(scope)
        if refusal is not None:
            await _reject_ws(send, 1008, refusal)
            return

        lease = await _connection_limiter.acquire_handshake()
        if lease is None:
            await _reject_ws(
                send, 1013, "too many concurrent streaming connections; try again shortly"
            )
            return
        # This connection's slot is its own lease. It finishes the pre-auth phase when
        # it authenticates or is refused, and releases at the end; both are idempotent
        # on the lease, and the lease decrements only the phases *it* holds, so this
        # connection can never consume another's pre-auth or total slot.
        try:
            state = scope.setdefault("state", {})
            state["streaming"] = StreamingScopeContext(
                config=self._config,
                path=self._path,
                lease=lease,
                finish_handshake=lease.finish_handshake,
            )
            await self._app(scope, receive, send)
        finally:
            await lease.release()

    def _handshake_refusal(self, scope: dict) -> str | None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host")
        origin = headers.get("origin")
        # Loopback peer is always required, independent of the remote opt-in.
        peer = scope.get("client")
        peer_host = peer[0] if peer else None
        if not is_loopback_authority(host or ""):
            return (
                "live streaming serves loopback names and addresses only "
                "(127.0.0.1, localhost, [::1])"
            )
        locality = peer_locality(peer_host)
        if locality is None:
            return "live streaming requires a provably loopback peer"
        if locality is False:
            return "live streaming is loopback-only"
        if not self._config.origin_allowed(origin):
            return f"origin '{origin}' is not allowed to open a live stream"
        return None


async def _reject_ws(send: Any, code: int, reason: str) -> None:
    """Refuse a WebSocket handshake with a close code, before accepting it."""
    await send({"type": "websocket.close", "code": code, "reason": reason})


# --- the connection handler ------------------------------------------------


@dataclass
class _Connection:
    """One accepted connection's mutable state, owned by its single handler task.

    Every field is touched only from the handler coroutine (the event loop), so
    the object needs no lock of its own. The *core session* it points at is the
    only thing shared with a worker thread, and the core makes that thread-safe.
    """

    session: StreamingSession | None = None
    #: True once ``finish`` has run and the final message has been sent.
    finished: bool = False
    #: True once a cancel or a disconnect has been observed.
    cancelled: bool = False
    #: True once a ``finish`` control has been seen. Audio that arrives after it -
    #: including frames already queued behind it - is refused, never fed.
    finish_requested: bool = False
    #: Set by the receiver the moment it sees a disconnect or a cancel. A *dedicated*
    #: signal, so a blocking step waits on one event instead of re-scanning the inbox,
    #: which spins and starves the loop when racing audio is present.
    gone: asyncio.Event = field(default_factory=asyncio.Event)
    #: The core's final event, delivered once inside the ``final`` message.
    final_event: Any = None
    #: Monotonic time of the last accepted audio frame, for the idle timeout.
    last_audio_at: float = field(default_factory=time.monotonic)
    #: Monotonic time of the last control message, for the control-rate bound.
    last_control_at: float = 0.0
    audio_frames: int = 0
    accepted_bytes: int = 0


class _ClientGone(Exception):
    """Raised internally when the peer has disconnected or cancelled the stream."""


class StreamingRoute:
    """The ASGI application for one stream path: the wire around one live session.

    Concurrency, in one place, because it is the whole difficulty:

    - **One receiver task, one core consumer.** A single background task owns
      ``receive()`` for the whole connection and posts every message onto a bounded
      queue; the handler coroutine is the only core consumer. The socket is thus
      always being watched - during the handshake, during ``start``, during
      ``finish`` - so a disconnect or a cancel is noticed promptly and never
      silently drops a queued frame. The core session is driven only from
      :func:`asyncio.to_thread`, one call at a time, because the core forbids
      ``read_event`` and ``finish`` running together.
    - **Blocking work never runs on the event loop.** ``start`` (which loads the
      model, up to its own 30 s deadline), ``finish`` (up to its finish deadline),
      and ``cancel`` (which terminates the owned native child) all run in a worker
      thread. The loop stays free so a second connection - and the UI's own HTTP
      surface - stays responsive while a stream is starting or stopping.
    - **Backpressure is explicit, never a silent drop.** Audio frames are accepted
      only up to the core's own byte queue; when the core refuses a frame the
      connection is aborted with an ``error``. Every ``send`` is bounded by
      :data:`SEND_TIMEOUT_S`; a peer that stops reading is disconnected and its
      session cancelled rather than stalling the loop.
    - **Nothing is allocated before authentication.** No session, no worker, no
      model, until the first message is a valid ``start`` that authenticates.
    """

    def __init__(self, config: StreamingConfig):
        self._config = config

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "websocket":
            return
        state = scope.get("state") or {}
        context: StreamingScopeContext | None = state.get("streaming")
        if context is None:
            # The guard did not run (misconfiguration): refuse rather than serve an
            # unprotected stream. A missing guard must fail closed.
            await _reject_ws(send, 1011, "streaming guard not installed")
            return

        message = await receive()
        if message.get("type") != "websocket.connect":
            await _finish_handshake(context)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        origin = headers.get("origin")
        header_token = _bearer_token(headers.get("authorization"))
        # The guard already validated the origin; re-checking here would be a second
        # policy. We accept the handshake now and authenticate on `start`. The accept
        # is bounded too: a peer that connects and then stops reading must not stall
        # the loop on the very first send.
        try:
            await asyncio.wait_for(
                send({"type": "websocket.accept"}), SEND_TIMEOUT_S
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - the peer is gone
            await _finish_handshake(context)
            return

        connection = _Connection()
        inbox: asyncio.Queue = asyncio.Queue(maxsize=MAX_INBOX)
        receiver = asyncio.create_task(self._receiver(receive, inbox, connection.gone))
        try:
            await self._run(connection, context, origin, header_token, inbox, send)
        except _ClientGone:
            pass
        except StreamingError as exc:
            await _safe_send_error(send, exc)
        except asyncio.CancelledError:
            # The connection task itself was cancelled (server shutdown, or the
            # transport tearing down). Cleanup still must happen; re-raise after.
            _release_session_sync(connection)
            raise
        except Exception as exc:  # noqa: BLE001 - a handler fault must not leak a session
            await _safe_send_error(send, exc)
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver
            await self._release_session_async(connection)
            await _safe_close(send)

    async def _release_session_async(self, connection: _Connection) -> None:
        """Cancel the connection's session if it is still live, off the loop.

        If *this* coroutine is itself being cancelled, the ``await`` below would
        raise before the cancel is issued; the synchronous fallback runs then, so
        cleanup happens on every path.
        """
        session = connection.session
        if session is None or connection.finished:
            return
        try:
            await _cancel_session_async(session)
        except asyncio.CancelledError:
            _cancel_session_sync(session)
            raise

    async def _receiver(self, receive: Any, inbox: asyncio.Queue, gone: asyncio.Event) -> None:
        """The one task that reads the socket, for the whole connection.

        It runs until the connection ends. It never blocks on a full queue
        indefinitely - it waits until the handler drains a slot - so no frame is
        dropped by this task. Alongside queueing every message, it raises the dedicated
        ``gone`` signal the moment it sees a disconnect or a cancel: a blocking step
        (``start``/``finish``) waits on that one event instead of re-scanning the inbox,
        so a frame that races the blocking step is *left in order* rather than being
        requeued in a loop that starves the event loop.
        """
        while True:
            message = await receive()
            if message.get("type") == "websocket.disconnect" or _is_cancel_control(message):
                gone.set()
            await inbox.put(message)

    async def _run(
        self,
        connection: _Connection,
        context: StreamingScopeContext,
        origin: str | None,
        header_token: str | None,
        inbox: asyncio.Queue,
        send: Any,
    ) -> None:
        started = await self._await_start(connection, context, origin, header_token, inbox, send)
        if not started:
            return
        assert connection.session is not None
        await send_json(send, {
            "type": "ready",
            "version": PROTOCOL_VERSION,
            "session": connection.session.id,
            "limits": {
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "format": "pcm_s16le",
                "max_chunk_bytes": MAX_CHUNK_BYTES,
                "max_queue_seconds": MAX_QUEUE_SECONDS,
                "chunk_deadline_s": CHUNK_DEADLINE_S,
                "idle_timeout_s": IDLE_TIMEOUT_S,
            },
        })
        await self._pump(connection, inbox, send)

    async def _await_start(
        self,
        connection: _Connection,
        context: StreamingScopeContext,
        origin: str | None,
        header_token: str | None,
        inbox: asyncio.Queue,
        send: Any,
    ) -> bool:
        """Wait for and validate the ``start`` message; allocate nothing until then.

        The ``start`` itself - which launches the owned worker and loads the model -
        is run off the event loop, and the session is assigned to the connection
        *before* it is started, so a disconnect or a cancel arriving during the
        launch is observed and the owned child is still cleaned up.
        """
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        payload: dict | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await send_error(send, "STREAMING_HANDSHAKE_TIMEOUT",
                                 "no start message before the handshake deadline")
                return False
            try:
                raw = await asyncio.wait_for(inbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if raw.get("type") == "websocket.disconnect":
                return False
            payload = _decode_control(raw)
            break
        if payload is None:
            await send_error(send, "STREAMING_PROTOCOL_ERROR",
                             "the first message must be a JSON start object")
            return False
        if payload.get("type") != "start":
            await send_error(send, "STREAMING_PROTOCOL_ERROR",
                             "the first message must have type 'start'")
            return False
        refusal = self._authenticate(payload, origin, header_token)
        if refusal is not None:
            # Unauthorized: no session is allocated, no asset is touched. The
            # refused field value is never echoed back.
            await send_error(send, "STREAMING_UNAUTHORIZED", refusal)
            return False
        try:
            session = self._build_session(payload)
        except StreamingProtocolError as exc:
            await send_error(send, "STREAMING_PROTOCOL_ERROR", str(exc))
            return False
        # Publish the session before starting it, so the cleanup path can reach it
        # even if the start is still in flight when the connection ends.
        connection.session = session
        try:
            await self._start_session_guarded(connection, session, inbox)
        except StreamingError as exc:
            await send_error(send, exc.code, str(exc))
            return False
        connection.last_audio_at = time.monotonic()
        return True

    async def _start_session_guarded(
        self, connection: _Connection, session: StreamingSession, inbox: asyncio.Queue
    ) -> None:
        """Start the session off-loop, abandoning it if the client goes away first.

        ``start`` is a long, blocking call (it launches the native worker and loads
        the model). It runs in a thread; the handler keeps watching the socket, so a
        disconnect or a cancel during the launch cancels the session - whose own
        ``start`` is cancel-safe and will not resurrect a cancelled state - and the
        owned child is released.
        """
        start_task = asyncio.create_task(asyncio.to_thread(session.start))
        watcher = asyncio.create_task(self._watch_for_gone(connection))
        try:
            done, _pending = await asyncio.wait(
                {start_task, watcher}, timeout=START_DEADLINE_S + 1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (start_task, watcher):
                if not task.done():
                    task.cancel()
            for task in (start_task, watcher):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if watcher in done:
            # The client disconnected or cancelled while the worker was launching.
            connection.cancelled = True
            await _cancel_session_async(session)
            raise _ClientGone()
        if start_task not in done:
            # The launch outran its own deadline. Treat it as a fatal start failure:
            # cancel so the owned child is released, then report it.
            await _cancel_session_async(session)
            raise WorkerProcessError("the streaming worker did not start in time")
        start_task.result()  # re-raises a start failure, already cleaned up by core

    async def _watch_for_gone(self, connection: _Connection) -> None:
        """Resolve when the peer disconnects or sends a cancel, during a blocking step.

        It waits on the connection's dedicated ``gone`` event, which the receiver sets
        the instant it sees a disconnect or a cancel. Nothing here reads the inbox, so
        a message that races the blocking step is left *in order* for the pump and this
        wait neither drops a frame nor spins - the busy requeue loop that starved the
        event loop under racing audio is gone.
        """
        await connection.gone.wait()

    def _build_session(self, payload: dict) -> StreamingSession:
        return _build_session(payload, worker_factory=self._config.worker_factory)

    def _authenticate(
        self, payload: dict, origin: str | None, header_token: str | None
    ) -> str | None:
        """Reason to refuse this ``start``, or ``None`` to allow it. No anonymous path.

        Three cases, and nothing else:

        - **Browser on the host's own origin.** Must hold the UI capability. In the
          production profile the API token is required *as well* - the capability
          is never a replacement for the token, only the extra check the UI itself
          would make.
        - **Browser on an allowed cross origin, or a non-browser client (no
          ``Origin``).** Must hold the API Bearer token, supplied either as an
          ``Authorization: Bearer`` header on the upgrade or as a ``token`` field in
          the ``start`` message. A capability is never accepted here: it is bound to
          the UI's own origin and a cross-origin page cannot have been served it.
        - **Anything else.** Refused. A wrong capability on the own origin, a token
          used to stand in for a capability, a valid capability from a foreign
          origin, or a host that requires a token and is presented none - all
          refused, and the refused value is never echoed back.
        """
        if origin is not None and self._config.is_own_origin(origin):
            if not self._config.capability_ok(payload.get("capability")):
                return "invalid or missing UI capability"
            if self._config.require_api_token and not self._config.token_ok(payload.get("token")):
                return "invalid or missing API token"
            return None
        # A cross-origin browser, or a non-browser client: authenticated by token.
        # A host that requires no token (a misconfiguration) authenticates no one.
        if not self._config.require_api_token:
            return "this server is not configured to authenticate a stream"
        supplied = payload.get("token") if payload.get("token") else header_token
        if not self._config.token_ok(supplied):
            return "invalid or missing API token"
        return None

    async def _pump(self, connection: _Connection, inbox: asyncio.Queue, send: Any) -> None:
        """Receive controls/audio and emit events, from a single task.

        A ``finish`` ends this stream's *input*: the finish is performed once, the
        tail is drained exactly once, and the pump returns. Audio that arrives after
        the finish control - including frames already queued behind it - is not part
        of the stream and is never fed to the worker; the pump stops draining audio
        the moment it sees the finish, so a frame behind the finish cannot slip in by
        being "already queued". A ``cancel`` or a disconnect cancels the session,
        which stops the worker thread.
        """
        assert connection.session is not None
        while True:
            # Drain at least one queued socket message, but never block past a core
            # poll cycle, so the core is still advanced promptly.
            try:
                message = await asyncio.wait_for(inbox.get(), timeout=_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                message = None
            if message is not None:
                if message.get("type") == "websocket.disconnect":
                    connection.cancelled = True
                    return
                outcome = await self._handle_message(connection, message, send)
                if outcome == "cancel":
                    connection.cancelled = True
                    return
                if outcome == "finish":
                    # The finish ends input right here: no frame behind it is fed,
                    # whether it was already queued or arrives later. Finish now.
                    connection.finish_requested = True
                    await self._do_finish(connection, send)
                    return
                if outcome == "error":
                    return

            if _idle_expired(connection):
                await send_error(
                    send, "STREAMING_IDLE_TIMEOUT",
                    f"no audio or control for {IDLE_TIMEOUT_S:.0f}s",
                )
                return

            # Poll the core for the next event, bounded so a disconnect or a control
            # command is noticed promptly even on a slow model.
            event = await _read_core_event(connection.session)
            if event is not None:
                await _send_event(send, event)

    async def _handle_message(
        self, connection: _Connection, message: dict, send: Any
    ) -> str:
        """Handle one client message. Returns "", "cancel", "finish", or "error"."""
        if message.get("type") != "websocket.receive":
            return ""
        data = message.get("bytes")
        text = message.get("text")
        if data is not None:
            return await self._accept_audio(connection, data, send)
        if text is not None:
            return await self._handle_control(connection, text, send)
        return ""

    async def _accept_audio(self, connection: _Connection, data: bytes, send: Any) -> str:
        assert connection.session is not None
        if connection.finished or connection.finish_requested:
            await send_error(send, "STREAMING_PROTOCOL_ERROR", "audio received after finish")
            return "error"
        if not data or len(data) % 2 != 0 or len(data) > MAX_CHUNK_BYTES:
            await send_error(
                send, "STREAMING_PROTOCOL_ERROR",
                f"an audio frame must be 1..{MAX_CHUNK_BYTES} whole 16-bit samples",
            )
            return "error"
        try:
            # The core validates and queues; it never blocks on the engine, so this
            # await returns promptly. A full core queue is an explicit refusal.
            await asyncio.to_thread(connection.session.feed, data)
        except StreamingError as exc:
            # Overload or a queue refusal: abort explicitly rather than drop audio.
            await send_error(send, exc.code, str(exc))
            return "error"
        connection.audio_frames += 1
        connection.accepted_bytes += len(data)
        connection.last_audio_at = time.monotonic()
        return ""

    async def _handle_control(self, connection: _Connection, text: str, send: Any) -> str:
        now = time.monotonic()
        if now - connection.last_control_at < _MIN_CONTROL_INTERVAL_S:
            await send_error(send, "STREAMING_PROTOCOL_ERROR",
                             "control messages are arriving too quickly")
            return "error"
        connection.last_control_at = now
        # The bound is on *bytes* the frame occupies, not characters: a multi-byte
        # control is refused at the same wire size a byte budget would refuse it.
        if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
            await send_error(send, "STREAMING_PROTOCOL_ERROR",
                             f"control message exceeds {MAX_CONTROL_BYTES} bytes")
            return "error"
        try:
            payload = json.loads(text)
        except ValueError:
            await send_error(send, "STREAMING_PROTOCOL_ERROR", "malformed JSON control")
            return "error"
        if not isinstance(payload, dict):
            await send_error(send, "STREAMING_PROTOCOL_ERROR", "control must be an object")
            return "error"
        kind = payload.get("type")
        if kind == "cancel":
            return "cancel"
        if kind == "finish":
            if connection.finished:
                return ""
            return "finish"
        await send_error(
            send, "STREAMING_PROTOCOL_ERROR",
            "unknown control type; expected 'finish' or 'cancel'",
        )
        return "error"

    async def _do_finish(self, connection: _Connection, send: Any) -> None:
        """Run the core finish off-loop, watched, then emit the tail and final message.

        The core's ``finish`` is a blocking call (it drains the engine up to its
        finish deadline), so it runs in a thread. A *watcher* races it against the
        connection's ``gone`` signal, so a cancel or a disconnect that arrives while
        the finish is draining cancels the session immediately - terminating the owned
        worker - rather than waiting out the whole drain. The finish itself is left to
        complete in the background; its result is discarded because the client is gone.
        """
        assert connection.session is not None
        finish_task = asyncio.create_task(
            asyncio.to_thread(connection.session.finish, FINISH_DEADLINE_S)
        )
        watcher = asyncio.create_task(self._watch_for_gone(connection))
        try:
            done, _pending = await asyncio.wait(
                {finish_task, watcher},
                timeout=FINISH_DEADLINE_S + 1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (finish_task, watcher):
                if not task.done():
                    task.cancel()
            for task in (finish_task, watcher):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if watcher in done:
            # The client cancelled or disconnected while the finish was draining.
            connection.cancelled = True
            await _cancel_session_async(connection.session)
            raise _ClientGone()
        if finish_task not in done:
            # The finish outran its deadline: cancel so the owned child is released.
            await _cancel_session_async(connection.session)
            await send_error(
                send, "STREAMING_FINISH_TIMEOUT",
                "the stream did not finish within its deadline",
            )
            return
        try:
            final_event = finish_task.result()
        except StreamingError as exc:
            await send_error(send, exc.code, str(exc))
            return
        connection.finished = True
        connection.final_event = final_event
        # Emit any *transcript* events still buffered from the finish drain, then the
        # one ``final`` message. The final event itself is delivered inside the
        # ``final`` message (with the canonical transcript), never also as a bare
        # ``event`` - it is emitted exactly once, in one place.
        await _drain_events(connection.session, send)
        transcript = _transcript_dict(connection.session, final_event)
        if transcript is None:
            await send_error(
                send, "STREAMING_TRANSCRIPT_ERROR",
                "the final transcript could not be built for this session",
            )
            return
        await send_json(send, {
            "type": "final",
            "event": final_event.to_dict(),
            "transcript": transcript,
        })


async def _read_core_event(session: StreamingSession) -> Any:
    """Read the next core event in a worker thread, bounded by a short timeout.

    The read runs off the event loop so a slow model cannot block the socket, and
    it is bounded so the loop regains control often enough to notice a disconnect
    or a control command. Returning ``None`` here means "nothing ready yet", not
    end-of-stream: an EOF or a fatal core error raises out of the thread.
    """
    try:
        return await asyncio.to_thread(session.read_event, _CORE_POLL_S)
    except StreamingError:
        raise
    except Exception as exc:
        raise StreamingProtocolError(str(exc)) from exc


async def _drain_events(session: StreamingSession, send: Any) -> None:
    """Send any *transcript* events the session still holds from the finish drain.

    The final event is deliberately skipped here: it is delivered exactly once, in
    the ``final`` message, so a consumer never sees it twice.
    """
    while True:
        event = await asyncio.to_thread(session.read_event, 0.0)
        if event is None:
            return
        if event.is_final:
            continue
        await _send_event(send, event)


async def _send_event(send: Any, event: Any) -> None:
    """Send one core event as a wire ``event`` message."""
    await send_json(send, {"type": "event", "event": event.to_dict()})


def _transcript_dict(session: StreamingSession, final_event: Any) -> dict[str, Any] | None:
    """The canonical final transcript, or ``None`` if it cannot be built.

    A failure here is *not* masked with an empty object: an empty transcript would
    read as "the session produced no text", silently discarding a real result. The
    caller sends an explicit error instead.
    """
    try:
        return session.final_transcript.to_dict(include_words=True)
    except Exception:  # noqa: BLE001 - reported to the client, never swallowed
        return None


def _idle_expired(connection: _Connection) -> bool:
    return time.monotonic() - connection.last_audio_at > IDLE_TIMEOUT_S


def _bearer_token(header: str | None) -> str | None:
    """The token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _is_cancel_control(message: dict) -> bool:
    """Whether a socket message is a ``cancel`` control, without consuming state."""
    text = message.get("text")
    if not text or len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "cancel"


def _decode_control(message: dict) -> dict | None:
    text = message.get("text")
    if text is None:
        return None
    if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _build_session(payload: dict, *, worker_factory: Any = None) -> StreamingSession:
    """Validate a ``start`` message and build (but do not start) a session."""
    _require_int(payload.get("version"), "version", PROTOCOL_VERSION)
    if payload.get("format") != "pcm_s16le":
        raise StreamingProtocolError("only format 'pcm_s16le' is supported")
    _require_int(payload.get("sample_rate"), "sample_rate", SAMPLE_RATE)
    _require_int(payload.get("channels"), "channels", 1)
    language = payload.get("language")
    if language is not None and language not in LANGUAGES:
        raise StreamingProtocolError(
            f"language {language!r} is not one of {', '.join(LANGUAGES)} (or null)"
        )
    return StreamingSession(language=language, worker_factory=worker_factory)


def _require_int(value: Any, name: str, expected: int) -> None:
    """Require ``value`` to be exactly the integer ``expected``.

    ``bool`` is a subclass of ``int`` in Python, so ``True`` would equal ``1``. A
    strict check rejects ``true``/``false`` where a number is required, so a
    malformed ``start`` cannot smuggle a boolean past a numeric field.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise StreamingProtocolError(f"{name} must be the integer {expected}")


async def _cancel_session_async(session: StreamingSession) -> None:
    """Cancel a session off the event loop, bounded, never raising.

    ``cancel`` terminates the owned native child, which can take up to the core's
    cancel deadline; running it in a thread keeps the loop free so the server - and
    the UI's own HTTP surface - stays responsive while a stream is being torn down.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(session.cancel), CANCEL_DEADLINE_S + 2.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001, S110 - teardown must not raise
        pass


def _cancel_session_sync(session: StreamingSession) -> None:
    """Cancel a session on the calling thread. The last-resort path when no await may run.

    Used only when the handler coroutine is itself being cancelled, so a further
    ``await`` would be aborted before the cancel was issued. ``cancel`` is bounded by
    the core's own cancel deadline and acts only on this session's owned child, so a
    synchronous call here cannot hang the process or touch any other process.
    """
    with contextlib.suppress(Exception):
        session.cancel()


def _release_session_sync(connection: _Connection) -> None:
    """Cancel the connection's session if still live, synchronously, never raising."""
    session = connection.session
    if session is None or connection.finished:
        return
    _cancel_session_sync(session)


async def _finish_handshake(context: StreamingScopeContext) -> None:
    """Release the pre-auth handshake slot exactly once, if the guard provided one."""
    callback = context.finish_handshake
    if callback is not None:
        with contextlib.suppress(Exception):
            await callback()


async def send_json(send: Any, payload: dict[str, Any]) -> None:
    """Send one JSON message, bounded by :data:`SEND_TIMEOUT_S`.

    A peer that has stopped reading will not block the event loop here: a send that
    cannot complete within the deadline raises, is treated as a dead connection, and
    the caller's cleanup path cancels the session.
    """
    data = json.dumps(payload, ensure_ascii=False)
    try:
        await asyncio.wait_for(send({"type": "websocket.send", "text": data}), SEND_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise _ClientGone("the client stopped reading the stream") from exc


async def send_error(send: Any, code: str, message: str) -> None:
    await send_json(send, {"type": "error", "code": code, "message": message})


async def _safe_send_error(send: Any, exc: Exception) -> None:
    code = getattr(exc, "code", "STREAMING_ERROR")
    try:
        await send_error(send, code, str(exc) or type(exc).__name__)
    except Exception:  # noqa: BLE001, S110 - the socket may already be gone
        pass


async def _safe_close(send: Any) -> None:
    try:
        await asyncio.wait_for(send({"type": "websocket.close", "code": 1000}), SEND_TIMEOUT_S)
    except Exception:  # noqa: BLE001, S110 - a half-closed socket is not an error here
        pass


# --- installation onto a host app -----------------------------------------


def install_streaming(host_app: Any, *, path: str, config: StreamingConfig) -> Any:
    """Wrap ``host_app`` so ``path`` serves a guarded, opt-in live stream.

    The returned app is the ASGI application to serve. For a WebSocket scope on
    ``path`` the guard validates loopback peer/Host and origin and the streaming
    route handles it; **every other scope is passed straight through to
    ``host_app``**, so mounting this changes nothing about the host's HTTP routes,
    middleware, or guards. Wrapping the whole app - rather than adding a route to
    it - is what lets the pure-ASGI guard see the raw handshake before the host
    allocates anything, since an HTTP middleware never runs for an upgrade.
    """
    route = StreamingRoute(config)

    async def _stream_app(scope: dict, receive: Any, send: Any) -> None:
        await route(scope, receive, send)

    guarded = WebSocketScopeGuard(_stream_app, path=path, config=config)
    _original = host_app

    async def _root(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "websocket" and scope.get("path") == path:
            await guarded(scope, receive, send)
            return
        await _original(scope, receive, send)

    return _root
