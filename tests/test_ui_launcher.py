"""Launcher settings: loopback-only bind, port reservation, shortcut metadata.

Nothing here starts uvicorn for real, opens a browser, or writes a shortcut to
the desktop. ``reserve_port`` is exercised against a *deliberately occupied* port
to prove it moves on, and it also proves the port it returns is genuinely held by
the returned socket (a second bind of the same port fails) - that is the property
that makes readiness honest. The shortcut builder is checked through a fake
PowerShell runner: no COM object, no ``.lnk`` on disk, and **no pywin32**.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from textflowkit.ui import launcher, paths
from textflowkit.ui.launcher import choose_port, main, reserve_port


def test_loopback_host_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "j.sqlite3"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "w"))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "o"))

    # Refuse to actually serve: patch the server runner to a no-op so main()
    # returns after reserving the socket. This still exercises loopback
    # acceptance, owner locking, and port reservation.
    monkeypatch.setattr(launcher, "_run_server", lambda *a, **kw: 0)
    rc = main(["--host", "127.0.0.1", "--port", "8791", "--no-browser"])
    assert rc == 0


def test_non_loopback_bind_refused(capsys):
    rc = main(["--host", "0.0.0.0", "--no-browser"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "loopback only" in err


def test_allow_remote_env_does_not_widen_ui(monkeypatch, capsys):
    monkeypatch.setenv("TEXTFLOWKIT_ALLOW_REMOTE", "1")
    rc = main(["--host", "0.0.0.0", "--no-browser"])
    assert rc == 2  # still refused


def test_reserve_port_holds_the_socket():
    """The returned socket is listening on the returned port.

    A connect to the port succeeds *before the server exists* because the reserved
    socket is already accepting - which is exactly the point: readiness is proven
    by our own socket, not by a probe of whatever else might hold the number.
    """
    port, sock = reserve_port("127.0.0.1", 0)  # 0 -> first free ephemeral
    try:
        assert port > 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            assert probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()
    # After release, the port no longer accepts (nothing is listening).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_reserve_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        port, sock = reserve_port("127.0.0.1", busy)
        try:
            assert port != busy
            assert port > busy
        finally:
            sock.close()


def test_choose_port_skips_occupied():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        chosen = choose_port("127.0.0.1", busy)
        assert chosen != busy
        assert chosen > busy


def test_choose_port_returns_preferred_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert choose_port("127.0.0.1", free) == free


def test_defaults_are_durable_and_per_user(monkeypatch):
    """`apply_defaults` fills unset storage vars and never overwrites a set one.

    It writes into the *live* `os.environ` (correct for the launcher, which wants
    the defaults to reach the store it builds next). That makes this the one test
    in the suite that mutates the process environment directly, so it runs
    against a private copy of `os.environ` rather than the real one: leaking
    `TEXTFLOWKIT_OUTPUT_ROOT` here previously set a confinement root that failed
    every later test publishing outside it. Swapping the module's `os.environ`
    for a throwaway dict keeps the call honest and the process clean.
    """
    sandbox = dict(os.environ)
    monkeypatch.setattr(paths.os, "environ", sandbox)
    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_WORK_ROOT", raising=False)
    monkeypatch.delenv("TEXTFLOWKIT_OUTPUT_ROOT", raising=False)
    for _name in ("TEXTFLOWKIT_DB", "TEXTFLOWKIT_WORK_ROOT", "TEXTFLOWKIT_OUTPUT_ROOT"):
        sandbox.pop(_name, None)
    applied = paths.apply_defaults()
    assert paths.ENV_DB in applied
    assert applied[paths.ENV_DB].endswith("jobs.sqlite3")
    for name in (paths.ENV_DB, paths.ENV_WORK_ROOT, paths.ENV_OUTPUT_ROOT):
        assert sandbox[name]
    sandbox[paths.ENV_DB] = "D:/custom/jobs.sqlite3"
    assert "TEXTFLOWKIT_DB" not in paths.apply_defaults()
    assert sandbox[paths.ENV_DB] == "D:/custom/jobs.sqlite3"


def test_shortcut_is_windows_only(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "posix")
    with pytest.raises(OSError):
        launcher._create_shortcut(host="127.0.0.1", port=8756, destination=None)


class _FakeProc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_shortcut_metadata_targets_pythonw_and_args(monkeypatch, tmp_path):
    """The .lnk is built from a JSON data file; no pywin32 is imported.

    PowerShell is faked, so nothing runs and no file on the desktop is created.
    The test reads the JSON the launcher *would* hand to PowerShell and checks the
    metadata: this interpreter's pythonw, the launcher module, the chosen port.
    """
    if launcher.os.name != "nt":
        pytest.skip("shortcut metadata is Windows-specific")

    captured = {}

    def fake_run(cmd, **kwargs):
        # The script file and the JSON data file are both written before this
        # runs; read the JSON back to assert on the payload.
        data_path = kwargs["env"]["TFK_SHORTCUT_DATA"]
        captured["data"] = __import__("json").loads(
            __import__("pathlib").Path(data_path).read_text(encoding="utf-8")
        )
        # The destination must exist for the launcher to report success.
        __import__("pathlib").Path(captured["data"]["shortcut_path"]).write_text("stub")
        return _FakeProc()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    dest = tmp_path / "TextFlowKit UI.lnk"
    path = launcher._create_shortcut(host="127.0.0.1", port=8799, destination=dest)
    assert path == dest
    data = captured["data"]
    assert data["target"].lower().endswith(("pythonw.exe", "python.exe"))
    assert "-m textflowkit.ui.launcher" in data["arguments"]
    assert "--port 8799" in data["arguments"]


def test_shortcut_uses_powershell_not_pywin32(monkeypatch, tmp_path):
    """The shortcut path must not import pythoncom/win32com (http-only install).

    The check is *differential*: it snapshots the pywin32 modules already present
    (another test in a shared session may have imported one via an unrelated
    `find_spec` probe) and asserts the launcher imports no **new** one. An absolute
    "not in sys.modules" would fail on ordering, not on a real regression.
    """
    if launcher.os.name != "nt":
        pytest.skip("Windows-only")

    def fake_run(cmd, **kwargs):
        assert "powershell" in cmd[0].lower()
        __import__("pathlib").Path(
            __import__("json").loads(
                __import__("pathlib").Path(kwargs["env"]["TFK_SHORTCUT_DATA"]).read_text("utf-8")
            )["shortcut_path"]
        ).write_text("stub")
        return _FakeProc()

    pywin32 = ("pythoncom", "win32com")
    before = {name for name in sys.modules if name.split(".")[0] in pywin32}

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    dest = tmp_path / "x.lnk"
    launcher._create_shortcut(host="127.0.0.1", port=8756, destination=dest)

    after = {name for name in sys.modules if name.split(".")[0] in pywin32}
    assert after <= before, f"the launcher imported pywin32 modules: {after - before}"


def test_shortcut_dir_is_honoured(monkeypatch, tmp_path, capsys):
    """--create-shortcut --shortcut-dir writes where asked (faked PowerShell)."""
    if launcher.os.name != "nt":
        pytest.skip("Windows-only")

    seen = {}

    def fake_run(cmd, **kwargs):
        data = __import__("json").loads(
            __import__("pathlib").Path(kwargs["env"]["TFK_SHORTCUT_DATA"]).read_text("utf-8")
        )
        seen["path"] = data["shortcut_path"]
        __import__("pathlib").Path(data["shortcut_path"]).write_text("stub")
        return _FakeProc()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "j.sqlite3"))
    rc = main(["--create-shortcut", "--shortcut-dir", str(tmp_path)])
    assert rc == 0
    assert seen["path"] == str(tmp_path / "TextFlowKit UI.lnk")
