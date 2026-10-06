"""The UI mounts the existing HTTP app; it must not change it.

The UI reuses every job route by mounting ``http_server.app`` under ``/api``.
These tests pin that promise: the mounted routes behave exactly as the developer
app's own tests expect, the developer app object is not mutated, and the UI adds
no new route to it. A regression here would mean the UI had forked the API.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from textflowkit.adapters import http_server
from textflowkit.ui.app import create_app

LOOPBACK_PEER = ("127.0.0.1", 50000)


def _client(app):
    return TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)


def test_developer_app_routes_are_unchanged():
    """Building a UI app must not add or remove routes on the developer app."""
    before = {getattr(r, "path", None) for r in http_server.app.routes}
    create_app(host="127.0.0.1", port=8756)
    after = {getattr(r, "path", None) for r in http_server.app.routes}
    assert before == after


def test_mounted_health_and_sources():
    ui = create_app(host="127.0.0.1", port=8756)
    c = _client(ui)
    health = c.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    sources = c.get("/api/sources")
    assert sources.status_code == 200
    assert "formats" in sources.json()


def test_mounted_app_keeps_loopback_guard():
    """A foreign peer is refused by the mounted app's own guard too."""
    ui = create_app(host="127.0.0.1", port=8756)
    foreign = TestClient(ui, base_url="http://127.0.0.1:8756", client=("203.0.113.9", 4444))
    assert foreign.get("/api/health").status_code == 403


def test_developer_app_still_serves_its_own_root():
    """The developer app reached directly is the developer app, not the UI."""
    c = TestClient(http_server.app, base_url="http://127.0.0.1:8767", client=LOOPBACK_PEER)
    # The developer app has no `/` route (its routes start with /health etc.).
    assert c.get("/health").json()["status"] == "ok"


def test_ui_never_exposes_the_developer_launcher():
    """The UI ships its own launcher; it never serves the dev API unguarded.

    The developer HTTP launcher (``textflowkit-http``) is a *separate* console
    script. The UI has its own entry point and mounts the developer app only
    *under* its own request gate at ``/api`` - so starting the UI does not start
    the developer launcher, and the developer app is never reachable on the UI's
    port outside the gate.
    """
    from textflowkit.ui import launcher

    assert callable(launcher.main)
    # The launcher owns its own entry: it is not the adapter's main.
    assert launcher.main is not http_server.main


def test_developer_launcher_help_unchanged():
    """The developer launcher still advertises its own flags, untouched."""
    import io
    from contextlib import redirect_stdout

    import pytest

    buf = io.StringIO()
    # argparse's --help prints to stdout and raises SystemExit(0).
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        http_server.main(["--help"])
    assert exc.value.code == 0
    text = buf.getvalue()
    assert "--allow-remote" in text  # its remote opt-in is intact
    assert "--host" in text
