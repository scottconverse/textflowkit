"""A real serve on a reserved socket: the launcher's own readiness contract.

This test starts a real uvicorn server on a **reserved** socket (the launcher's
own path), drives one HTTP request against it from a real client, and stops it
through the app's controlled-shutdown flag. It proves the two things the launcher
promises and a probe cannot: the socket serves *our* app, and ``should_exit``
drains it - no pid is signalled.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest

uvicorn = pytest.importorskip("uvicorn")

from textflowkit.ui.app import create_app
from textflowkit.ui.launcher import reserve_port


@pytest.fixture
def temp_store(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "w"))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "o"))
    from textflowkit.core import jobs as jobs_mod
    from textflowkit.core.startup import reset_startup_recovery

    jobs_mod.reset_default_store()
    reset_startup_recovery()
    yield
    jobs_mod.reset_default_store()
    reset_startup_recovery()


def test_real_uvicorn_serves_reserved_socket_and_stops(temp_store):
    port, sock = reserve_port("127.0.0.1", 0)
    app = create_app(host="127.0.0.1", port=port)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, proxy_headers=False, log_level="warning"
    )
    server = uvicorn.Server(config)
    app.ui_attach_owned_server(server)

    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        # Wait for the server to actually serve (our own socket).
        deadline = time.monotonic() + 15.0
        body = None
        while time.monotonic() < deadline and body is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/ui/capabilities", timeout=1.0
                ) as resp:
                    body = resp.read().decode("utf-8")
            except OSError:  # not serving yet
                time.sleep(0.1)
        assert body is not None and "default_engine" in body

        # Controlled stop: set the server flag the way the endpoint would.
        server.should_exit = True
        thread.join(timeout=15.0)
        assert not thread.is_alive()
    finally:
        server.should_exit = True
        try:
            sock.close()
        except OSError:
            pass


def test_occupied_port_moves_to_a_free_one(temp_store):
    """A busy preferred port does not stop the launch; it moves on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        port, sock = reserve_port("127.0.0.1", busy_port)
        try:
            assert port != busy_port
        finally:
            sock.close()
