"""The stream as the *UI* actually mounts it - through the real app, not a stub.

The transport tests drive ``StreamingConfig`` directly. These drive the assembled
UI application: the ``/api/stream`` route the UI installs on the mounted app, the
``/api/streaming-example`` page with this process's capability embedded, and the
refusal of a browser on any other origin. The UI app is built with a fake worker
factory so no native library is touched.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from textflowkit.adapters import http_server
from textflowkit.adapters import streaming_ws as ws
from textflowkit.ui import app as ui_app

UI_HOST = "127.0.0.1"
UI_PORT = 8756
UI_ORIGIN = f"http://{UI_HOST}:{UI_PORT}"
LOOPBACK_PEER = "127.0.0.1"


class _FakeWorker:
    """The shape the route starts, feeds, and finishes without a native library."""

    def __init__(self):
        self.started = False
        self.cancelled = False
        self.finished = False

    def start(self):
        self.started = True

    def feed(self, _pcm: bytes):
        return []

    def finish(self):
        self.finished = True
        return []

    def cancel(self):
        self.cancelled = True


class _FakeFactory:
    def __call__(self):
        return _FakeWorker()


@pytest.fixture
def ui_client(monkeypatch):
    """The real UI app, streaming opted in, its worker factory faked.

    ``create_app`` builds the stream config through ``ui_app.ui_streaming_config``;
    wrapping it here keeps the *route*, the guard, the origin policy, and the
    capability all real while only the worker is a stand-in.
    """
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
            worker_factory=_FakeFactory(),
        )

    monkeypatch.setattr(ui_app, "ui_streaming_config", _faked)
    app = ui_app.create_app(host=UI_HOST, port=UI_PORT)
    return app


def _client(app, *, origin=UI_ORIGIN, peer=LOOPBACK_PEER):
    return TestClient(
        app,
        base_url=UI_ORIGIN,
        client=(peer, 51000),
        headers={"host": f"{UI_HOST}:{UI_PORT}", "origin": origin},
    )


def _start(capability: str) -> str:
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


def _ui_token(app) -> str:
    # The UI's per-process capability is embedded in the shell; read it from there
    # rather than reaching into a private attribute.
    html = _client(app).get("/").text
    marker = 'name="textflowkit-capability" content="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _ui_stream_routes(app):
    """Every stream route the UI app carries on *its own* router.

    The UI installs its guarded ``/api/stream`` route on its own router (before the
    ``/api`` mount), not on the shared inner app, so the route lives at the outer
    path and one UI app cannot rewrite another's guard.
    """
    return [r for r in app.router.routes if isinstance(r, http_server._StreamingWebSocketRoute)]


def test_the_ui_stream_route_is_installed_when_opted_in(monkeypatch):
    monkeypatch.setenv(ws.ENV_STREAMING, "1")
    app = ui_app.create_app(host=UI_HOST, port=UI_PORT)
    paths = {r.path for r in _ui_stream_routes(app)}
    assert "/api/stream" in paths


def test_the_ui_stream_is_absent_when_not_opted_in(monkeypatch):
    monkeypatch.delenv(ws.ENV_STREAMING, raising=False)
    app = ui_app.create_app(host=UI_HOST, port=UI_PORT)
    assert not _ui_stream_routes(app)


def test_a_ui_browser_with_the_capability_reaches_ready(ui_client):
    token = _ui_token(ui_client)
    with _client(ui_client).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start(capability=token))
        ready = json.loads(websocket.receive_text())
        assert ready["type"] == "ready"


def test_a_ui_browser_with_a_wrong_capability_is_refused(ui_client):
    with _client(ui_client).websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start(capability="not-the-token"))
        msg = json.loads(websocket.receive_text())
        assert msg["type"] == "error"
        assert msg["code"] == "STREAMING_UNAUTHORIZED"


def test_a_ui_browser_on_a_wrong_origin_is_refused(ui_client):
    # The handshake guard refuses the origin before any session is allocated, so
    # the socket is closed rather than accepting and then sending an error frame.
    token = _ui_token(ui_client)
    client = _client(ui_client, origin="http://evil.example")
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start(capability=token))
    assert excinfo.value.code == 1008


def test_a_ui_browser_on_a_wrong_origin_with_the_right_capability_is_still_refused(ui_client):
    # The capability alone must never open a stream from a foreign origin.
    token = _ui_token(ui_client)
    client = _client(ui_client, origin="http://localhost:9999")
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/stream") as websocket:
        websocket.send_text(_start(capability=token))
    assert excinfo.value.code == 1008


def test_the_ui_example_embeds_the_capability(ui_client):
    token = _ui_token(ui_client)
    html = _client(ui_client).get("/api/streaming-example").text
    assert token in html
    assert 'name="textflowkit-capability"' in html


def test_the_ui_example_404s_when_streaming_is_off(monkeypatch):
    monkeypatch.delenv(ws.ENV_STREAMING, raising=False)
    app = ui_app.create_app(host=UI_HOST, port=UI_PORT)
    assert _client(app).get("/api/streaming-example").status_code == 404


def test_the_ui_serves_the_packaged_worklet(ui_client):
    response = _client(ui_client).get("/assets/streaming-capture-worklet.js")
    assert response.status_code == 200
    assert b"registerProcessor" in response.content
