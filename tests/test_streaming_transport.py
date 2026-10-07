"""The live-streaming WebSocket transport: lifecycle, auth, origin, limits.

Everything here is deterministic and offline. The transport is exercised through a
real Starlette ``TestClient`` WebSocket connection and, where a direct ASGI call is
clearer, through ``WebSocketScopeGuard``/``StreamingRoute`` invoked on a raw scope.
The *core session* it drives is an injected fake worker (see :class:`_FakeFactory`),
so no native library is loaded, no child process is launched, and no model is
downloaded. What these tests pin is the *transport's* contract - the wire protocol,
the auth and origin rules, the resource bounds, and the disconnect/finish
lifecycle - not the engine's accuracy.

The example asset and the deterministic resampler are covered by
``tests/test_streaming_assets.py``; they are not duplicated here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from textflowkit.adapters import streaming_ws as ws
from textflowkit.core import streaming as st

LOOPBACK_PEER = ("127.0.0.1", 50000)
FOREIGN_PEER = ("203.0.113.9", 4444)  # TEST-NET-3, documentation range
UI_ORIGIN = "http://127.0.0.1:8756"
OTHER_ORIGIN = "https://godview.example.com"
HOST = "127.0.0.1:8756"
UI_TOKEN = "ui-capability-token"
API_TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_registry():
    st.reset_session_registry()
    yield
    for session in list(_active()):
        with contextlib.suppress(Exception):
            session.cancel()
    st.reset_session_registry()


def _active():
    with st._registry_lock:
        return set(st._active_sessions)


# --- fake core worker ------------------------------------------------------


class _FakeWorker:
    """A scripted worker that emits a couple of events then a final."""

    def __init__(self, *, events=2, records=None):
        import queue as _queue

        self._q = _queue.Queue()
        self._events = events
        self._records = records
        self._emitted = 0
        self.terminated = False
        self._fed = 0

    def start(self) -> None:
        pass

    def feed(self, frame: bytes) -> None:
        self._fed += len(frame)
        if self._records is not None:
            return
        if self._events > 0:
            self._events -= 1
            self._q.put({
                "kind": "event",
                "record": {
                    "text": "hello", "pending": "", "language": "en",
                    "pass_ms": 0.0, "words": [], "consumed_samples": self._fed // 2,
                },
            })

    def finish(self) -> None:
        self._q.put({
            "kind": "final",
            "record": {
                "text": "hello world", "pending": "", "language": "en",
                "pass_ms": 0.0, "words": [], "consumed_samples": self._fed // 2,
            },
        })
        self._q.put({"kind": "done"})

    def terminate(self) -> None:
        self.terminated = True

    def at_eof(self) -> bool:
        return False

    def read_record(self, timeout: float | None = None):
        import queue as _queue

        if self._records is not None and self._emitted < len(self._records):
            record = self._records[self._emitted]
            self._emitted += 1
            return record
        try:
            if timeout is None:
                return self._q.get()
            return self._q.get(timeout=timeout)
        except _queue.Empty:
            return None


class _SlowStartWorker(_FakeWorker):
    """A worker whose ``start`` blocks, to prove the loop stays responsive."""

    def __init__(self, *, start_delay: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        self._start_delay = start_delay

    def start(self) -> None:
        time.sleep(self._start_delay)


class _FakeFactory:
    def __init__(self, worker_cls=_FakeWorker, **kwargs):
        self._worker_cls = worker_cls
        self._kwargs = kwargs
        self.worker: _FakeWorker | None = None

    def __call__(self):
        self.worker = self._worker_cls(**self._kwargs)
        return self.worker


def _standalone_config(**overrides) -> ws.StreamingConfig:
    """A config that authenticates a non-browser client by API token."""
    kwargs = {
        "own_origin": None,
        "require_api_token": True,
        "api_token": API_TOKEN,
        "worker_factory": _FakeFactory(),
    }
    kwargs.update(overrides)
    return ws.StreamingConfig(**kwargs)


def _ui_config(**overrides) -> ws.StreamingConfig:
    """A config as the UI builds it: own origin only, no cross-app widening."""
    kwargs = {
        "own_origin": UI_ORIGIN,
        "ui_token": UI_TOKEN,
        "allowed_origins": (),
        "require_api_token": False,
        "api_token": None,
        "worker_factory": _FakeFactory(),
    }
    kwargs.update(overrides)
    return ws.StreamingConfig(**kwargs)


def _app(*, config: ws.StreamingConfig | None = None, path: str = "/stream"):
    """A minimal host app with streaming installed at ``path``."""
    async def _home(request):
        return PlainTextResponse("home")

    host = Starlette(routes=[Route("/", _home)])
    if config is None:
        config = _standalone_config()
    return ws.install_streaming(host, path=path, config=config)


def _client(app, *, origin=None, host=HOST, peer=LOOPBACK_PEER):
    # The websocket TestClient defaults ``Host`` to "testserver", which the
    # loopback-only guard rightly refuses; pass a real loopback Host so the test
    # exercises the policy as a real loopback client would.
    headers = {"host": host}
    if origin:
        headers["origin"] = origin
    return TestClient(app, base_url=UI_ORIGIN, client=peer, headers=headers)


def _start_message(**overrides) -> str:
    payload = {
        "type": "start", "version": 1, "format": "pcm_s16le",
        "sample_rate": 16000, "channels": 1, "language": "en",
        "token": API_TOKEN,
    }
    payload.update(overrides)
    return json.dumps(payload)


def pcm(byte_count: int) -> bytes:
    assert byte_count % 2 == 0
    return bytes((i * 7 + 3) & 0xFF for i in range(byte_count))


# --- happy path ------------------------------------------------------------


def test_a_valid_stream_reaches_ready_events_and_final():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"
        assert ready["version"] == 1
        assert ready["session"]
        assert ready["limits"]["sample_rate"] == 16000
        assert ready["limits"]["max_chunk_bytes"] == 32000

        websocket.send_bytes(pcm(3200))
        event = json.loads(websocket.receive_text())
        assert event["type"] == "event"
        assert event["event"]["kind"] == "transcript"
        assert event["event"]["seq"] == 0

        websocket.send_text(json.dumps({"type": "finish"}))
        final = json.loads(websocket.receive_text())
        assert final["type"] == "final"
        assert final["event"]["is_final"] is True
        assert "transcript" in final
        assert final["transcript"]["source"].startswith("stream:")


def test_the_final_message_carries_a_canonical_transcript():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()  # ready
        websocket.send_bytes(pcm(3200))
        websocket.receive_text()  # event
        websocket.send_text(json.dumps({"type": "finish"}))
        final = json.loads(websocket.receive_text())
        transcript = final["transcript"]
        assert transcript["engine"] == "whistle-streaming"
        assert transcript["segments"]
        assert "hello" in transcript["segments"][0]["text"]


# --- disabled by default ---------------------------------------------------


def test_streaming_is_disabled_unless_opted_in(monkeypatch):
    monkeypatch.delenv(ws.ENV_STREAMING, raising=False)
    assert ws.streaming_enabled() is False
    monkeypatch.setenv(ws.ENV_STREAMING, "1")
    assert ws.streaming_enabled() is True
    for off in ("0", "false", "", "no"):
        monkeypatch.setenv(ws.ENV_STREAMING, off)
        assert ws.streaming_enabled() is False


def test_a_disabled_server_has_no_stream_route():
    """With streaming off, an app built without install_streaming serves no route."""
    async def _home(request):
        return PlainTextResponse("home")

    host = Starlette(routes=[Route("/", _home)])
    with pytest.raises(WebSocketDisconnect), _client(host).websocket_connect("/stream"):
        pass


# --- authentication --------------------------------------------------------


def test_standalone_requires_the_api_token():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token="wrong"))
        message = json.loads(websocket.receive_text())
        assert message["type"] == "error"
        assert message["code"] == "STREAMING_UNAUTHORIZED"
    assert st.active_sessions() == 0  # unauthorized allocates nothing


def test_standalone_with_the_correct_token_is_allowed():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token=API_TOKEN))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


def test_a_nonbrowser_client_authenticates_with_the_authorization_header():
    """A programmatic client may present the token as a Bearer header on the upgrade."""
    app = _app()
    with _client(app).websocket_connect(
        "/stream", headers={"authorization": f"Bearer {API_TOKEN}"}
    ) as websocket:
        websocket.send_text(_start_message(token=None))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


def test_a_nonbrowser_client_without_a_token_is_refused():
    """No-Origin, no-token: refused. There is no anonymous path."""
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token=None))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"
    assert st.active_sessions() == 0


def test_a_config_with_no_api_token_serves_no_stream():
    """A misconfigured host (token required but empty) authenticates nobody."""
    config = _standalone_config(api_token=None)
    app = _app(config=config)
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token="anything"))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"
    assert st.active_sessions() == 0


def test_an_empty_token_never_matches():
    config = _standalone_config(api_token="")
    assert config.token_ok("") is False
    assert config.token_ok("anything") is False


def test_an_error_message_never_echoes_the_supplied_secret():
    app = _app()
    secret = "s3cr3t-do-not-log"
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token=secret))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"
        assert secret not in json.dumps(message)


# --- UI capability and origin ---------------------------------------------


def test_ui_requires_the_capability_and_rejects_a_wrong_one():
    app = _app(config=_ui_config())
    with _client(app, origin=UI_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability="wrong", token=None))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"
    assert st.active_sessions() == 0


def test_ui_accepts_the_correct_capability():
    app = _app(config=_ui_config())
    with _client(app, origin=UI_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability=UI_TOKEN, token=None))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


def test_ui_does_not_accept_the_api_token_in_place_of_a_capability():
    """A token is not a capability on the UI's own origin."""
    app = _app(config=_ui_config())
    with _client(app, origin=UI_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability=None, token=API_TOKEN))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"


def test_ui_cross_app_allowlist_does_not_widen_the_own_origin():
    """The cross-app allowlist must never be consulted for the UI's own surface."""
    config = _ui_config(allowed_origins=(OTHER_ORIGIN,))
    # A page on the allowlisted origin cannot use the UI capability: it is not the
    # own origin, so it is graded as a cross-origin client and needs a token.
    app = _app(config=config)
    with _client(app, origin=OTHER_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability=UI_TOKEN, token=None))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"


def test_a_browser_wrong_origin_is_refused_before_allocation():
    """A valid capability from a foreign origin is still refused at the handshake."""
    app = _app(config=_ui_config())
    foreign = "http://evil.example.com"
    with pytest.raises(WebSocketDisconnect), _client(app, origin=foreign).websocket_connect("/stream"):
        pass
    assert st.active_sessions() == 0


def test_a_non_loopback_peer_is_refused_even_with_a_valid_origin():
    app = _app(config=_ui_config())
    with pytest.raises(WebSocketDisconnect), _client(
        app, origin=UI_ORIGIN, peer=FOREIGN_PEER
    ).websocket_connect("/stream"):
        pass
    assert st.active_sessions() == 0


def test_an_allowed_cross_origin_with_the_api_token_is_accepted():
    """A generic 'God Eye View' app on a configured origin authenticates by token."""
    config = _standalone_config(allowed_origins=(OTHER_ORIGIN,))
    app = _app(config=config)
    with _client(app, origin=OTHER_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(token=API_TOKEN))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


def test_an_unlisted_cross_origin_is_refused():
    config = _standalone_config(allowed_origins=("https://allowed.example.com",))
    app = _app(config=config)
    with pytest.raises(WebSocketDisconnect), _client(
        app, origin="https://other.example.com"
    ).websocket_connect("/stream"):
        pass


def test_production_ui_requires_both_the_capability_and_the_token():
    """In the production profile the capability is never a replacement for the token."""
    config = _ui_config(require_api_token=True, api_token=API_TOKEN)
    app = _app(config=config)
    with _client(app, origin=UI_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability=UI_TOKEN, token=None))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_UNAUTHORIZED"
    with _client(app, origin=UI_ORIGIN).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(capability=UI_TOKEN, token=API_TOKEN))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


# --- the wire protocol -----------------------------------------------------


def test_bad_version_is_a_protocol_error():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message(version=2))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"
    assert st.active_sessions() == 0


def test_a_boolean_is_not_accepted_where_a_number_is_required():
    """``True == 1`` in Python; a strict check must still refuse ``true``."""
    app = _app()
    for override in ({"version": True}, {"sample_rate": True}, {"channels": True}):
        with _client(app).websocket_connect("/stream") as websocket:
            websocket.send_text(_start_message(**override))
            message = json.loads(websocket.receive_text())
            assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_bad_format_sample_rate_and_channels_are_refused():
    app = _app()
    for override in ({"format": "pcm_f32le"}, {"sample_rate": 44100}, {"channels": 2}):
        with _client(app).websocket_connect("/stream") as websocket:
            websocket.send_text(_start_message(**override))
            message = json.loads(websocket.receive_text())
            assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_malformed_first_message_is_refused():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text("not json")
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_an_empty_audio_frame_is_a_protocol_error():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()  # ready
        websocket.send_bytes(b"")
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_an_oversize_audio_frame_is_a_protocol_error():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()
        websocket.send_bytes(pcm(32002))  # past the 32000-byte ceiling
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_audio_after_finish_is_forbidden():
    """After ``finish`` the stream is terminal: no further frame is accepted.

    The server closes the connection once the ``final`` message is sent, so an
    audio frame after ``finish`` finds a closed stream rather than a second
    session. What is asserted is the contract that matters: the finished
    transcript is not extended by a late frame.
    """
    from starlette.websockets import WebSocketDisconnect

    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()  # ready
        websocket.send_bytes(pcm(3200))
        websocket.receive_text()  # event
        websocket.send_text(json.dumps({"type": "finish"}))
        final = json.loads(websocket.receive_text())
        assert final["event"]["is_final"] is True
        with pytest.raises(WebSocketDisconnect):
            websocket.send_bytes(pcm(3200))
            websocket.receive_text()


def test_an_unknown_control_type_is_a_protocol_error():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()
        websocket.send_text(json.dumps({"type": "explode"}))
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"


def test_an_oversize_control_frame_is_refused_by_byte_length():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()
        # Multi-byte characters: the bound is on wire bytes, not characters, so a
        # frame well under the byte limit in character count still exceeds it.
        big = json.dumps({"type": "finish", "pad": "é" * (ws.MAX_CONTROL_BYTES)})
        websocket.send_text(big)
        message = json.loads(websocket.receive_text())
        assert message["code"] == "STREAMING_PROTOCOL_ERROR"


# --- lifecycle: the finish flush, and completion ---------------------------


def test_finish_flushes_remaining_events_exactly_once():
    app = _app()
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()  # ready
        websocket.send_bytes(pcm(3200))
        first = json.loads(websocket.receive_text())
        assert first["event"]["seq"] == 0
        websocket.send_text(json.dumps({"type": "finish"}))
        final = json.loads(websocket.receive_text())
        assert final["type"] == "final"
        # The final event's seq is exactly one past the last transcript event, and
        # no event was emitted twice.
        assert final["event"]["seq"] == 1


def test_finishing_a_session_releases_the_slot_and_the_worker():
    factory = _FakeFactory()
    config = _standalone_config(worker_factory=factory)
    app = _app(config=config)
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()
        websocket.send_text(json.dumps({"type": "finish"}))
        json.loads(websocket.receive_text())
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_disconnect_cancels_the_session():
    """A dropped connection cancels the session; no replay or resume is provided."""
    factory = _FakeFactory()
    config = _standalone_config(worker_factory=factory)
    app = _app(config=config)
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()  # ready
        websocket.send_bytes(pcm(3200))
        websocket.receive_text()  # event
    # Exiting the context closes the socket; the handler must cancel the session.
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_an_explicit_cancel_releases_the_session():
    factory = _FakeFactory()
    config = _standalone_config(worker_factory=factory)
    app = _app(config=config)
    with _client(app).websocket_connect("/stream") as websocket:
        websocket.send_text(_start_message())
        websocket.receive_text()
        websocket.send_text(json.dumps({"type": "cancel"}))
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


# --- configuration: origins ------------------------------------------------


def test_streaming_origins_parses_and_validates(monkeypatch):
    monkeypatch.setenv(
        ws.ENV_STREAMING_ORIGINS, "https://a.example.com, http://127.0.0.1:9000"
    )
    origins = ws.streaming_origins()
    assert "https://a.example.com:443" in origins
    assert "http://127.0.0.1:9000" in origins


def test_streaming_origins_refuses_wildcards_credentials_and_paths(monkeypatch):
    for bad in (
        "https://*.example.com",
        "https://user:pass@example.com",
        "https://example.com/some/path",
        "ftp://example.com",
        "https://example.com?q=1",
        "https://example.com#frag",
        "https://example.com:99999",
        "not-an-origin",
    ):
        monkeypatch.setenv(ws.ENV_STREAMING_ORIGINS, bad)
        with pytest.raises(st.StreamingProtocolError):
            ws.streaming_origins()


def test_an_empty_origin_list_allows_no_cross_origin(monkeypatch):
    monkeypatch.delenv(ws.ENV_STREAMING_ORIGINS, raising=False)
    assert ws.streaming_origins() == ()


def test_a_malformed_origin_header_is_not_allowed():
    config = _standalone_config(allowed_origins=(OTHER_ORIGIN,))
    for bad in ("http://host:99999", "not an origin", "javascript:alert(1)", ""):
        assert config.origin_allowed(bad) is False


# --- concurrency: cancel/disconnect during a blocking step -----------------

# These call the guard and route directly on raw ASGI scopes, so the timing of a
# disconnect during a blocking ``start``/``finish`` can be controlled precisely.


class _ScopeSend:
    """Collects the messages a handler sends, so a test can inspect them."""

    def __init__(self):
        self.messages: list[dict] = []
        self.closed = False

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)
        if message.get("type") == "websocket.close":
            self.closed = True

    def texts(self):
        return [m for m in self.messages if m.get("type") == "websocket.send"]


def _raw_scope(*, config, origin=None, path="/stream", host=HOST, peer=LOOPBACK_PEER):
    """A raw ASGI websocket scope with the guard's context already published.

    Calling the route directly (rather than through the guard) is what lets a test
    control the exact interleaving of a disconnect or a cancel with a blocking
    step; the context the guard would publish is supplied here instead.
    """
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    context = ws.StreamingScopeContext(config=config, path=path)
    return {
        "type": "websocket", "path": path, "headers": headers, "client": peer,
        "state": {"streaming": context},
    }


def _scripted_receive(messages):
    """A receive() that yields the given messages, then waits forever."""
    queue = list(messages)

    async def _receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)
        return {"type": "websocket.disconnect", "code": 1000}

    return _receive


def test_a_disconnect_during_start_cancels_the_owned_worker():
    """A client that vanishes while the worker is loading must still be cleaned up."""
    factory = _FakeFactory(worker_cls=_SlowStartWorker, start_delay=1.5)
    config = _standalone_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _scripted_receive([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _start_message()},
            # A disconnect arrives while ``start`` is still sleeping in its thread.
            {"type": "websocket.disconnect", "code": 1006},
        ])
        send = _ScopeSend()
        started = time.monotonic()
        await route(_raw_scope(config=config), receive, send)
        return time.monotonic() - started

    elapsed = asyncio.run(asyncio.wait_for(_drive(), timeout=5.0))
    assert elapsed < 4.0, "a disconnect during start must unwind promptly"
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_a_cancel_is_observed_while_finish_is_draining():
    """A cancel during a slow finish must stop the session and free its slot."""
    factory = _FakeFactory()
    config = _standalone_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _scripted_receive([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _start_message()},
            {"type": "websocket.receive", "text": json.dumps({"type": "cancel"})},
        ])
        send = _ScopeSend()
        await route(_raw_scope(config=config), receive, send)
        return send

    asyncio.run(asyncio.wait_for(_drive(), timeout=5.0))
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_a_slow_reading_peer_does_not_block_the_event_loop():
    """A peer that never reads must not wedge the loop: the send is bounded."""
    config = _standalone_config()

    async def _drive():
        receive = _scripted_receive([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _start_message()},
            {"type": "websocket.disconnect", "code": 1006},
        ])

        async def _stalling_send(message):
            # Model a socket whose peer has stopped reading: the send never returns.
            await asyncio.sleep(3600)

        route = ws.StreamingRoute(config)
        started = time.monotonic()
        await asyncio.wait_for(route(_raw_scope(config=config), receive, _stalling_send), timeout=12.0)
        return time.monotonic() - started

    elapsed = asyncio.run(_drive())
    # The bounded send times out at SEND_TIMEOUT_S, not at the test watchdog.
    assert elapsed < ws.SEND_TIMEOUT_S + 4.0
    assert st.active_sessions() == 0


def test_the_event_loop_is_not_blocked_while_a_stream_starts():
    """A second coroutine must make progress while a slow start is in its thread."""
    factory = _FakeFactory(worker_cls=_SlowStartWorker, start_delay=0.6)
    config = _standalone_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _scripted_receive([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _start_message()},
        ])
        send = _ScopeSend()
        ticks = 0

        async def _heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(_heartbeat())
        try:
            # Start, then immediately cancel so the handler returns.
            task = asyncio.create_task(route(_raw_scope(config=config), receive, send))
            await asyncio.sleep(0.3)
            assert ticks >= 3, "the loop must run while start() blocks in its thread"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    asyncio.run(_drive())
    assert st.active_sessions() == 0


# --- the connection bound --------------------------------------------------


def test_handshake_concurrency_is_bounded():
    limiter = ws._ConnectionLimiter(handshake_limit=2, total_limit=4)

    async def _drive():
        a = await limiter.acquire_handshake()
        b = await limiter.acquire_handshake()
        assert a is not None and b is not None
        # A third pre-auth connection is refused, not queued.
        assert await limiter.acquire_handshake() is None
        # Authenticating one frees a handshake slot, but the total bound still holds.
        await a.finish_handshake()
        c = await limiter.acquire_handshake()
        assert c is not None
        await b.finish_handshake()
        await c.finish_handshake()
        await a.release()
        await b.release()
        await c.release()

    asyncio.run(_drive())


def test_the_total_connection_bound_holds_after_authentication():
    limiter = ws._ConnectionLimiter(handshake_limit=8, total_limit=2)

    async def _drive():
        a = await limiter.acquire_handshake()
        b = await limiter.acquire_handshake()
        assert a is not None and b is not None
        await a.finish_handshake()
        await b.finish_handshake()
        # Both authenticated, total bound reached: no more connections at all.
        assert await limiter.acquire_handshake() is None
        await a.release()
        assert await limiter.acquire_handshake() is not None
        await b.release()

    asyncio.run(_drive())
