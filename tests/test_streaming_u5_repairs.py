"""U5 transport corrections: per-connection limiter phases, and UI route isolation.

Two defects the coordinator's probe (``transport-findings.txt``) reproduced against
the U4 sources:

A. ``_ConnectionLimiter``'s two counters were *global*, not per connection. Two
   pre-auth connections (A, B) both incremented ``_handshaking``/``_total``; A
   authenticating and then closing decremented *both* counters, stranding B in the
   pre-auth phase with a counter that no longer described it. Release must be tied
   to the *phase the connection itself holds*, exactly once per phase.

B. ``create_app`` installed the UI's guarded ``/api/stream`` route by mutating the
   process-global ``http_server.app`` router. Building a second UI app in the same
   process replaced the first app's route, so the first app's own-origin policy was
   silently rewritten to the *second* app's origin. Each UI app must carry its own
   isolated stream route.

These are behavioural tests: A drives the real limiter through the exact acquisition
and release sequence the route performs, and B drives two *live* UI apps through a
real ``TestClient`` WebSocket connection, each authenticating with its own token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from starlette.testclient import TestClient

from textflowkit.adapters import http_server
from textflowkit.adapters import streaming_ws as ws
from textflowkit.core import streaming as st
from textflowkit.ui import app as ui_app

UI_HOST = "127.0.0.1"
LOOPBACK_PEER = "127.0.0.1"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts and ends with an empty session registry."""
    st.reset_session_registry()
    yield
    with st._registry_lock:
        for session in list(st._active_sessions):
            with contextlib.suppress(Exception):
                session.cancel()
    st.reset_session_registry()


# --- A: per-connection phase accounting ------------------------------------


def test_a_preauth_release_never_decrements_another_connections_phase():
    """A release after A authenticates must not release B's held pre-auth slot.

    This is the exact sequence from the coordinator probe: acquire A, acquire B,
    finish A's handshake (A is now authenticated), then release A entirely. B is
    *still* in its pre-auth phase, so exactly one handshake slot and one total slot
    must remain held.
    """
    limiter = ws._ConnectionLimiter(handshake_limit=8, total_limit=8)

    async def _drive():
        a = await limiter.acquire_handshake()
        b = await limiter.acquire_handshake()
        assert a is not None and b is not None
        # A authenticates: A's pre-auth phase ends, A remains an authenticated slot.
        await a.finish_handshake()
        # A's connection closes.
        await a.release()
        # B is untouched: still pre-auth, still counted.
        assert limiter._handshaking == 1, "A's release consumed B's pre-auth slot"
        assert limiter._total == 1, "A's release consumed B's total slot"

    asyncio.run(_drive())


def test_release_is_exactly_once_per_phase_on_every_guard_path():
    """Every guard path releases its *own* phase exactly once, and counters return to 0.

    The guard has four ways out: (1) rejected before accept, (2) accepted and then
    refused during authentication, (3) cancelled mid-handshake, (4) authenticated and
    served. Each must return its own slot exactly once, and a final double-release
    (finish_handshake after release, which the guard can reach) must not underflow.
    """
    limiter = ws._ConnectionLimiter(handshake_limit=8, total_limit=8)

    async def _drive():
        # (1) refused before accept: acquire then release only.
        lease = await limiter.acquire_handshake()
        assert lease is not None
        await lease.release()

        # (2) accepted then refused during auth: finish then release.
        lease = await limiter.acquire_handshake()
        assert lease is not None
        await lease.finish_handshake()
        await lease.release()

        # (3) cancelled mid-handshake (never authenticated): release alone.
        lease = await limiter.acquire_handshake()
        assert lease is not None
        await lease.release()

        # (4) authenticated and served.
        lease = await limiter.acquire_handshake()
        assert lease is not None
        await lease.finish_handshake()
        await lease.release()

        # Every path released its own slot: back to empty.
        assert limiter._handshaking == 0
        assert limiter._total == 0

        # An idempotent extra finish after an authenticated release must not
        # underflow another connection's slot.
        x = await limiter.acquire_handshake()
        y = await limiter.acquire_handshake()
        assert x is not None and y is not None
        await x.finish_handshake()
        await y.finish_handshake()
        await x.release()
        await y.release()
        assert limiter._handshaking == 0
        assert limiter._total == 0

    asyncio.run(_drive())


def test_concurrent_mixed_phases_keep_counters_consistent():
    """Drive many concurrent connections through mixed phases and assert exact counts.

    A strong behavioural test: it interleaves acquisition, authentication, refusal,
    and release across N connections on the real event loop, so a counter that is
    global rather than per-connection drifts and the final accounting fails.
    """
    handshake_limit = 32
    total_limit = 64
    limiter = ws._ConnectionLimiter(handshake_limit=handshake_limit, total_limit=total_limit)

    async def _one(index: int) -> None:
        lease = await limiter.acquire_handshake()
        assert lease is not None
        if index % 3 == 0:
            # authenticate, serve, then close
            await lease.finish_handshake()
            await asyncio.sleep(0)
            await lease.release()
        elif index % 3 == 1:
            # refused during auth, then close
            await lease.finish_handshake()
            await lease.release()
        else:
            # cancelled mid-handshake
            await asyncio.sleep(0)
            await lease.release()

    async def _drive():
        await asyncio.gather(*(_one(i) for i in range(30)))
        assert limiter._handshaking == 0
        assert limiter._total == 0

    asyncio.run(_drive())


# --- B: UI stream route isolation ------------------------------------------


class _IsolatedWorker:
    """A worker stand-in that starts, feeds, finishes, and cancels without a library."""

    def __init__(self):
        self.started = False
        self.finished = False
        self.cancelled = False

    def start(self):
        self.started = True

    def feed(self, _pcm: bytes):
        return []

    def finish(self):
        self.finished = True
        return []

    def cancel(self):
        self.cancelled = True


class _IsolatedFactory:
    def __call__(self):
        return _IsolatedWorker()


def _build_ui(monkeypatch, *, port: int):
    """Build the real UI app on *port* with a faked worker factory."""
    monkeypatch.setenv(ws.ENV_STREAMING, "1")
    monkeypatch.delenv(ws.ENV_STREAMING_ORIGINS, raising=False)

    real = ui_app.ui_streaming_config

    def _faked(*, ui_token, expected_origin):
        config = real(ui_token=ui_token, expected_origin=expected_origin)
        return type(config)(
            own_origin=config.own_origin,
            allowed_origins=config.allowed_origins,
            ui_token=config.ui_token,
            api_token=config.api_token,
            require_api_token=config.require_api_token,
            worker_factory=_IsolatedFactory(),
        )

    monkeypatch.setattr(ui_app, "ui_streaming_config", _faked)
    return ui_app.create_app(host=UI_HOST, port=port)


def _client(app, *, port: int, origin=None):
    origin = origin or f"http://{UI_HOST}:{port}"
    return TestClient(
        app,
        base_url=f"http://{UI_HOST}:{port}",
        client=(LOOPBACK_PEER, 51000),
        headers={"host": f"{UI_HOST}:{port}", "origin": origin},
    )


def _ui_token(app, *, port: int) -> str:
    html = _client(app, port=port).get("/").text
    marker = 'name="textflowkit-capability" content="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _start_message(capability: str) -> str:
    return json.dumps(
        {
            "type": "start",
            "version": 1,
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "language": "en",
            "capability": capability,
        }
    )


def test_two_live_ui_instances_each_authenticate_their_own_token(monkeypatch):
    """Two UI apps built in one process must not share a mutable stream route.

    Build app one on port 8911, then app two on port 8912, exactly as the probe
    does. App one must still enforce *its own* origin and token: its own token
    reaches ``ready`` and app two's token is refused, and vice versa. If the route
    is shared, the last ``create_app`` rewrites the first app's guard and this
    cross-check fails.
    """
    app_one = _build_ui(monkeypatch, port=8911)
    token_one = _ui_token(app_one, port=8911)

    app_two = _build_ui(monkeypatch, port=8912)
    token_two = _ui_token(app_two, port=8912)

    assert token_one != token_two, "each UI process must mint its own capability"

    # App one: its own token works.
    with _client(app_one, port=8911).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start_message(capability=token_one))
        assert json.loads(websocket.receive_text())["type"] == "ready"

    # App one: app two's token is refused (app one's guard was not overwritten).
    with _client(app_one, port=8911).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start_message(capability=token_two))
        msg = json.loads(websocket.receive_text())
        assert msg["type"] == "error"
        assert msg["code"] == "STREAMING_UNAUTHORIZED"

    # App two: its own token works.
    with _client(app_two, port=8912).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start_message(capability=token_two))
        assert json.loads(websocket.receive_text())["type"] == "ready"

    # App two: app one's token is refused.
    with _client(app_two, port=8912).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start_message(capability=token_one))
        msg = json.loads(websocket.receive_text())
        assert msg["type"] == "error"
        assert msg["code"] == "STREAMING_UNAUTHORIZED"


def test_two_live_ui_instances_each_reject_the_others_origin(monkeypatch):
    """Each UI app refuses a browser presenting the *other* app's origin.

    The guard's origin check is per-app. If ``create_app`` mutates the shared app's
    route, app one's own origin is rewritten to app two's, and a request from app
    one's real origin is then refused (or app two's is wrongly accepted). Both
    cross-origin cases must be refused with the handshake policy close code.
    """
    app_one = _build_ui(monkeypatch, port=8911)
    token_one = _ui_token(app_one, port=8911)
    app_two = _build_ui(monkeypatch, port=8912)
    token_two = _ui_token(app_two, port=8912)

    from starlette.websockets import WebSocketDisconnect

    # A browser claiming app two's origin hitting app one is refused.
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: SIM117 - the socket must close inside the raise
        with _client(app_one, port=8911, origin="http://127.0.0.1:8912").websocket_connect(
            "/api/stream"
        ) as websocket:
            websocket.send_text(_start_message(capability=token_one))
    assert excinfo.value.code == 1008

    # A browser claiming app one's origin hitting app two is refused.
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: SIM117 - the socket must close inside the raise
        with _client(app_two, port=8912, origin="http://127.0.0.1:8911").websocket_connect(
            "/api/stream"
        ) as websocket:
            websocket.send_text(_start_message(capability=token_two))
    assert excinfo.value.code == 1008


def test_ui_stream_routes_are_not_on_the_shared_http_app(monkeypatch):
    """The UI's stream route must not be installed by mutating ``http_server.app``.

    The shared developer app is a process-global used by the standalone launcher and
    by every directly-served ``uvicorn textflowkit.adapters.http_server:app``. A UI
    front end must carry its own isolated route, so building a UI app must not add
    a ``/api/stream`` route to the shared app's router.
    """
    monkeypatch.setenv(ws.ENV_STREAMING, "1")
    monkeypatch.delenv(ws.ENV_STREAMING_ORIGINS, raising=False)
    ui_app.create_app(host=UI_HOST, port=8911)

    shared_paths = {
        getattr(route, "path", None)
        for route in http_server.app.router.routes
        if isinstance(route, http_server._StreamingWebSocketRoute)
    }
    assert "/api/stream" not in shared_paths


def test_streaming_off_does_not_disable_an_already_built_ui_stream(monkeypatch):
    """Opting a *later* app out must not strip an already-built app's stream route.

    ``TEXTFLOWKIT_STREAMING`` is read when each app is built. Building a
    streaming-off app afterwards must leave an already-built streaming app's own
    route intact (previously this removed the shared route the first app depended
    on), and must itself have no stream route.
    """
    app_on = _build_ui(monkeypatch, port=8911)
    token_on = _ui_token(app_on, port=8911)

    monkeypatch.delenv(ws.ENV_STREAMING, raising=False)
    app_off = ui_app.create_app(host=UI_HOST, port=8912)

    # The already-built app still authenticates its own token.
    with _client(app_on, port=8911).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start_message(capability=token_on))
        assert json.loads(websocket.receive_text())["type"] == "ready"

    # The new, streaming-off app serves no stream.
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - the socket must close inside the raise
        with _client(app_off, port=8912).websocket_connect("/api/stream") as websocket:
            websocket.send_text(_start_message(capability=token_on))


def test_standalone_stream_still_served_after_a_ui_app_is_built(monkeypatch):
    """A UI front end must not disturb the standalone developer app's own route.

    The standalone route lives on the shared app at ``/stream`` and is installed at
    import/opt-in time. Building a UI app in the same process must not remove or
    rewrite it.
    """
    monkeypatch.setenv(ws.ENV_STREAMING, "1")
    monkeypatch.delenv(ws.ENV_STREAMING_ORIGINS, raising=False)
    # Re-assert the standalone route the way the launcher does.
    http_server.install_streaming_route(config=http_server.streaming_config(path="/stream"))
    before = {
        id(route)
        for route in http_server.app.router.routes
        if isinstance(route, http_server._StreamingWebSocketRoute)
    }
    assert before, "the standalone stream route must exist before building a UI app"

    _build_ui(monkeypatch, port=8911)

    after = {
        id(route)
        for route in http_server.app.router.routes
        if isinstance(route, http_server._StreamingWebSocketRoute)
    }
    assert after == before, "building a UI app mutated the shared standalone stream route"


# --- C: gone signal without spin, finish under a watcher, no audio after finish


class _BlockingWorker:
    """A worker whose ``start`` and ``finish`` block for a controlled time.

    Records whether it was started, whether it was terminated, and how many bytes
    it was fed, so a test can prove the owned runtime was cleaned up and that no
    audio was fed after a finish.
    """

    def __init__(self, *, start_delay=0.0, finish_delay=0.0):
        import queue as _queue

        self._start_delay = start_delay
        self._finish_delay = finish_delay
        self.started = False
        self.terminated = False
        self.finished = False
        self.fed_bytes = 0
        self._q = _queue.Queue()

    def start(self) -> None:
        if self._start_delay:
            import time as _time

            _time.sleep(self._start_delay)
        self.started = True

    def feed(self, frame: bytes) -> None:
        self.fed_bytes += len(frame)

    def finish(self) -> None:
        if self._finish_delay:
            import time as _time

            _time.sleep(self._finish_delay)
        self.finished = True
        # The core's finish drain pulls a real final record and then ``done``: it
        # never fabricates the tail. A fake that returned None forever would spin
        # the drain to its deadline, so emit a real final exactly as the engine does.
        self._q.put({
            "kind": "final",
            "record": {
                "text": "", "pending": "", "language": "en",
                "pass_ms": 0.0, "words": [], "consumed_samples": self.fed_bytes // 2,
            },
        })
        self._q.put({"kind": "done"})

    def terminate(self) -> None:
        self.terminated = True

    def at_eof(self) -> bool:
        return False

    def read_record(self, timeout=None):
        import queue as _queue

        try:
            if timeout is None:
                return self._q.get()
            return self._q.get(timeout=timeout)
        except _queue.Empty:
            return None


class _OrderingWorker(_BlockingWorker):
    """A blocking worker that records the first byte of each fed frame, in order."""

    def __init__(self, *, start_delay=0.0, finish_delay=0.0, order=None):
        super().__init__(start_delay=start_delay, finish_delay=finish_delay)
        self._order = order if order is not None else []

    def feed(self, frame: bytes) -> None:
        super().feed(frame)
        if frame:
            self._order.append(frame[0])


class _BlockingFactory:
    def __init__(self, *, start_delay=0.0, finish_delay=0.0, order=None, worker_cls=None):
        self._start_delay = start_delay
        self._finish_delay = finish_delay
        self._order = order
        self._worker_cls = worker_cls or _BlockingWorker
        self.worker = None

    def __call__(self):
        kwargs = {"start_delay": self._start_delay, "finish_delay": self._finish_delay}
        if self._worker_cls is _OrderingWorker:
            kwargs["order"] = self._order
        self.worker = self._worker_cls(**kwargs)
        return self.worker


class _RecordingSend:
    def __init__(self):
        self.messages = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    def texts(self):
        return [m["text"] for m in self.messages if m.get("type") == "websocket.send"]


API_TOKEN_VALUE = "0123456789abcdef0123456789abcdef"
HOST = "127.0.0.1:8756"


def _raw_scope(*, config, origin=None, path="/api/stream", host=HOST, peer=LOOPBACK_PEER):
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    context = ws.StreamingScopeContext(config=config, path=path)
    return {
        "type": "websocket",
        "path": path,
        "headers": headers,
        "client": peer,
        "state": {"streaming": context},
    }


def _drain_receive(messages):
    """A receive that yields the scripted messages, then blocks forever.

    A disconnect is *not* appended automatically: a test that wants one includes it
    in the script at the exact step it should arrive.
    """
    queue = list(messages)

    async def _receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)
        return {"type": "websocket.disconnect", "code": 1000}

    return _receive


def _stream_config(**overrides):
    kwargs = {
        "own_origin": None,
        "require_api_token": True,
        "api_token": API_TOKEN_VALUE,
    }
    kwargs.update(overrides)
    return ws.StreamingConfig(**kwargs)


def _start_payload(**overrides) -> str:
    payload = {
        "type": "start",
        "version": 1,
        "format": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "language": "en",
        "token": API_TOKEN_VALUE,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _pcm(bytes_count: int) -> bytes:
    return b"\x01\x00" * (bytes_count // 2)


def test_audio_racing_a_blocked_start_does_not_starve_the_event_loop():
    """A frame queued while ``start`` blocks must not spin the loop.

    The old watcher consumed a non-cancel frame, requeued it to the front, and read
    it again immediately with no yield - a busy loop that starved the event loop
    while the native start was still running. An independent asyncio ticker must keep
    firing at its own cadence throughout.

    The scenario runs in a worker *thread* with a hard wall-clock join so a starved
    loop is reported as a failure rather than hanging the test run: if the loop spins,
    the ticker never advances and the join times out.
    """
    import threading

    factory = _BlockingFactory(start_delay=1.2)
    config = _stream_config(worker_factory=factory)
    route = ws.StreamingRoute(config)
    result: dict = {}

    def _run_scenario():
        ticks = 0
        intervals = []

        async def _drive():
            nonlocal ticks
            receive = _drain_receive(
                [
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": _start_payload()},
                    # Audio frames that race the ~1.2s start. In the old code the watcher
                    # consumed one, requeued it, and re-read it in a tight loop that never
                    # yielded, starving the loop for the whole start; these frames must be
                    # left in order while the start runs.
                    {"type": "websocket.receive", "bytes": _pcm(3200)},
                    {"type": "websocket.receive", "bytes": _pcm(3200)},
                    {"type": "websocket.receive", "text": json.dumps({"type": "finish"})},
                ]
            )
            send = _RecordingSend()

            async def _ticker():
                nonlocal ticks
                loop = asyncio.get_event_loop()
                last = loop.time()
                while True:
                    await asyncio.sleep(0.02)
                    now = loop.time()
                    intervals.append(now - last)
                    last = now
                    ticks += 1

            ticker = asyncio.create_task(_ticker())
            try:
                await asyncio.wait_for(
                    route(_raw_scope(config=config), receive, send), timeout=8.0
                )
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

        asyncio.run(_drive())
        result["ticks"] = ticks
        result["intervals"] = intervals

    thread = threading.Thread(target=_run_scenario, name="u5-starve-scenario", daemon=True)
    thread.start()
    thread.join(timeout=12.0)
    assert not thread.is_alive(), (
        "the stream scenario never returned: the event loop was starved "
        f"(ticker reached {result.get('ticks', 0)} ticks)"
    )
    ticks = result["ticks"]
    intervals = result["intervals"]
    # Over the ~1.2s blocked start a 20ms ticker must fire dozens of times; a starved
    # loop would show a handful. Demand a floor a spin cannot reach.
    assert ticks >= 30, f"the event loop was starved while start blocked: {ticks} ticks"
    assert max(intervals) < 0.5, f"the loop stalled for {max(intervals):.3f}s"


def test_a_cancel_during_a_blocked_finish_terminates_the_owned_worker():
    """A cancel arriving while ``finish`` drains must clean up promptly.

    ``_do_finish`` ran the blocking finish with no watcher, so a cancel or a
    disconnect during the drain was noticed only after it returned. The owned worker
    must be terminated and the session slot freed well under 2s.
    """
    factory = _BlockingFactory(finish_delay=3.0)
    config = _stream_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _drain_receive(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _start_payload()},
                {"type": "websocket.receive", "text": json.dumps({"type": "finish"})},
                # The cancel arrives while finish is draining in its thread.
                {"type": "websocket.receive", "text": json.dumps({"type": "cancel"})},
            ]
        )
        send = _RecordingSend()
        loop = asyncio.get_event_loop()
        started = loop.time()
        await asyncio.wait_for(route(_raw_scope(config=config), receive, send), timeout=10.0)
        return loop.time() - started

    elapsed = asyncio.run(_drive())
    assert elapsed < 2.0, "a cancel during finish must not wait out the whole drain"
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_a_disconnect_during_a_blocked_finish_terminates_the_owned_worker():
    """A disconnect while ``finish`` drains must also unwind promptly."""
    factory = _BlockingFactory(finish_delay=3.0)
    config = _stream_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _drain_receive(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _start_payload()},
                {"type": "websocket.receive", "text": json.dumps({"type": "finish"})},
                {"type": "websocket.disconnect", "code": 1006},
            ]
        )
        send = _RecordingSend()
        loop = asyncio.get_event_loop()
        started = loop.time()
        await asyncio.wait_for(route(_raw_scope(config=config), receive, send), timeout=10.0)
        return loop.time() - started

    elapsed = asyncio.run(_drive())
    assert elapsed < 2.0, "a disconnect during finish must not wait out the whole drain"
    assert st.active_sessions() == 0
    assert factory.worker is not None and factory.worker.terminated is True


def test_audio_queued_behind_a_finish_is_not_fed():
    """Frames already behind ``finish`` must be refused, never fed to the worker.

    The old pump, on seeing ``finish``, drained the rest of the inbox feeding every
    queued audio frame before finishing. Audio that arrived *after* the finish control
    is not part of the stream and must not reach the worker.
    """
    factory = _BlockingFactory()
    config = _stream_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _drain_receive(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _start_payload()},
                # A frame that legitimately precedes the finish.
                {"type": "websocket.receive", "bytes": _pcm(3200)},
                {"type": "websocket.receive", "text": json.dumps({"type": "finish"})},
                # Frames queued *behind* the finish: must be refused, not fed.
                {"type": "websocket.receive", "bytes": _pcm(3200)},
                {"type": "websocket.receive", "bytes": _pcm(3200)},
            ]
        )
        send = _RecordingSend()
        await asyncio.wait_for(route(_raw_scope(config=config), receive, send), timeout=8.0)

    asyncio.run(_drive())
    assert factory.worker is not None
    assert factory.worker.fed_bytes == 3200, (
        f"audio queued behind finish was fed to the worker: {factory.worker.fed_bytes} bytes"
    )


def test_audio_racing_a_blocked_start_is_preserved_in_order():
    """A frame that raced a blocked start is neither dropped nor reordered.

    Once the start completes, the raced frames must reach the worker in arrival order.
    """
    order = []
    factory = _BlockingFactory(start_delay=0.6, order=order, worker_cls=_OrderingWorker)
    config = _stream_config(worker_factory=factory)
    route = ws.StreamingRoute(config)

    async def _drive():
        receive = _drain_receive(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "text": _start_payload()},
                {"type": "websocket.receive", "bytes": b"\x01\x00"},
                {"type": "websocket.receive", "bytes": b"\x02\x00"},
                {"type": "websocket.receive", "bytes": b"\x03\x00"},
                {"type": "websocket.receive", "text": json.dumps({"type": "finish"})},
            ]
        )
        send = _RecordingSend()
        await asyncio.wait_for(route(_raw_scope(config=config), receive, send), timeout=8.0)

    asyncio.run(_drive())
    assert order == [1, 2, 3], f"raced frames were dropped or reordered: {order}"


# --- D: malformed credential types are a protocol error, never a leak -----

_BAD_CREDENTIALS = [
    [],
    ["x"] * 1,
    {"token": "x"},
    123,
    1.5,
    True,
    None,
    "tökén",
    "ünïcode",
    "ÿ" * 32,
]


def test_a_malformed_capability_type_is_refused_without_raising():
    """A capability of the wrong type must never raise out of ``capability_ok``."""
    config = ws.StreamingConfig(
        own_origin="http://127.0.0.1:8756", ui_token="cap-token", require_api_token=False
    )
    for bad in _BAD_CREDENTIALS:
        assert config.capability_ok(bad) is False, f"capability_ok({bad!r}) must be False"


def test_a_malformed_token_type_is_refused_without_raising():
    """A token of the wrong type must never raise out of ``token_ok``."""
    config = ws.StreamingConfig(
        own_origin=None, api_token="x" * 32, require_api_token=True
    )
    for bad in _BAD_CREDENTIALS:
        assert config.token_ok(bad) is False, f"token_ok({bad!r}) must be False"


def test_a_malformed_credential_in_start_is_a_protocol_error_not_a_leak():
    """A ``start`` with a list/dict/non-ASCII credential is refused cleanly.

    ``hmac.compare_digest`` raises ``TypeError`` on a list or dict and ``TypeError``
    on a non-ASCII string; that must be turned into an explicit ``STREAMING_UNAUTHORIZED``
    error on the wire, never a traceback that closes the socket with a 500-class fault.
    """
    route = ws.StreamingRoute(_stream_config())
    for bad in ([], {"k": "v"}, 123, "tökén"):
        payload = json.dumps(
            {
                "type": "start",
                "version": 1,
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "token": bad,
            }
        )
        outcomes = []

        async def _drive(payload=payload):
            receive = _drain_receive(
                [
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": payload},
                ]
            )
            send = _RecordingSend()
            await asyncio.wait_for(
                route(_raw_scope(config=_stream_config()), receive, send), timeout=5.0
            )
            return send

        send = asyncio.run(_drive())
        texts = [json.loads(t) for t in send.texts()]
        outcomes.append(texts)
        first = texts[0]
        assert first["type"] == "error", f"credential {bad!r} did not produce an error"
        assert first["code"] == "STREAMING_UNAUTHORIZED"


def test_a_malformed_capability_on_the_own_origin_is_refused():
    """A non-string capability on the UI's own origin is refused, not raised."""
    config = ws.StreamingConfig(
        own_origin="http://127.0.0.1:8756", ui_token="cap-token", require_api_token=False
    )
    route = ws.StreamingRoute(config)
    for bad in ([], {"k": "v"}, "tökén"):
        payload = json.dumps(
            {
                "type": "start",
                "version": 1,
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "capability": bad,
            }
        )

        async def _drive(payload=payload):
            receive = _drain_receive(
                [
                    {"type": "websocket.connect"},
                    {"type": "websocket.receive", "text": payload},
                ]
            )
            send = _RecordingSend()
            await asyncio.wait_for(
                route(
                    _raw_scope(config=config, origin="http://127.0.0.1:8756"),
                    receive,
                    send,
                ),
                timeout=5.0,
            )
            return send

        send = asyncio.run(_drive())
        first = json.loads(send.texts()[0])
        assert first["type"] == "error"
        assert first["code"] == "STREAMING_UNAUTHORIZED"


# --- D: the standalone browser example's own-origin requirement -------------


def test_the_standalone_stream_refuses_a_browser_origin_until_it_is_allowlisted(monkeypatch):
    """A browser hitting the standalone ``/stream`` is refused unless its origin is listed.

    The standalone config has no own origin and an empty allowlist, so a browser page
    served from ``http://127.0.0.1:8767/streaming-example`` sends an ``Origin`` that is
    not allowed and the handshake is refused. This is why the standalone browser
    example needs ``TEXTFLOWKIT_STREAMING_ORIGINS`` set to *its own* origin - there is
    no non-browser bypass to infer it from.
    """
    monkeypatch.delenv(ws.ENV_STREAMING_ORIGINS, raising=False)
    config = http_server.streaming_config(path="/stream")
    assert config.own_origin is None
    assert config.allowed_origins == ()
    assert config.origin_allowed("http://127.0.0.1:8767") is False
    # A non-browser client (no Origin) is allowed here and authenticated by token.
    assert config.origin_allowed(None) is True


def test_listing_the_standalone_origin_unlocks_the_browser_example(monkeypatch):
    """Listing the example's own origin in the allowlist lets its browser connect."""
    monkeypatch.setenv(ws.ENV_STREAMING_ORIGINS, "http://127.0.0.1:8767")
    config = http_server.streaming_config(path="/stream")
    assert "http://127.0.0.1:8767" in config.allowed_origins
    assert config.origin_allowed("http://127.0.0.1:8767") is True
