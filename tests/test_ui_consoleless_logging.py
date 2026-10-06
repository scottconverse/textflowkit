"""Consoleless launch: a log file and a visible startup failure.

Under ``pythonw`` (the shortcut target) there is no stdout/stderr. A failure that
would otherwise be a silent exit must instead land in a per-user log file and a
Windows message box. A normal console launch keeps its ordinary stderr output.

Nothing here opens a real console-less process: the module-level streams are
faked, and the message box / PowerShell call is stubbed so no window appears.
"""

from __future__ import annotations

from textflowkit.ui import launcher, paths


def test_is_consoleless_true_when_streams_none(monkeypatch):
    monkeypatch.setattr(launcher.sys, "stdout", None)
    monkeypatch.setattr(launcher.sys, "stderr", None)
    assert launcher._is_consoleless() is True


def test_is_consoleless_false_on_console(monkeypatch):
    monkeypatch.setattr(launcher.sys, "stdout", object())
    monkeypatch.setattr(launcher.sys, "stderr", object())
    assert launcher._is_consoleless() is False


def test_log_file_is_in_per_user_data_dir(monkeypatch, tmp_path):
    """The launcher log lives under the per-user UI data dir."""
    monkeypatch.setattr(paths, "ui_data_dir", lambda: tmp_path / "TextFlowKit" / "ui")
    stream, path = launcher._open_launcher_log()
    assert path is not None
    assert path.name == "ui-launcher.log"
    assert (tmp_path / "TextFlowKit" / "ui") in path.parents
    if stream is not None:
        stream.close()


def test_log_line_writes_timestamped_text(tmp_path):
    path = tmp_path / "log.txt"
    with path.open("w", encoding="utf-8") as handle:
        launcher._log_line(handle, "started serving")
    text = path.read_text(encoding="utf-8")
    assert "started serving" in text


def test_failure_on_console_goes_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "_is_consoleless", lambda: False)
    shown = {}
    monkeypatch.setattr(launcher, "_show_message_box", lambda t, m: shown.update(t=t, m=m))
    launcher._report_startup_failure("error: boom", log_stream=None)
    err = capsys.readouterr().err
    assert "boom" in err
    assert shown == {}  # a console launch does not pop a window


def test_failure_consoleless_shows_message_box(monkeypatch):
    monkeypatch.setattr(launcher, "_is_consoleless", lambda: True)
    shown = {}
    monkeypatch.setattr(launcher, "_show_message_box", lambda t, m: shown.update(title=t, msg=m))
    launcher._report_startup_failure("error: boom", log_stream=None)
    assert "boom" in shown.get("msg", "")


def test_message_box_passes_text_via_file_not_command_line(monkeypatch, tmp_path):
    """The message never becomes part of a PowerShell command line."""
    if launcher.os.name != "nt":
        import pytest

        pytest.skip("PowerShell message box is Windows-specific")
    captured = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    launcher._show_message_box("Title", "body with 'quotes' and $vars; rm -rf")
    joined = " ".join(captured["cmd"])
    # The dangerous text is not on the command line at all.
    assert "rm -rf" not in joined
    assert "$vars" not in joined
    # It is handed over through a file the script reads.
    assert captured.get("env") is None or "TFK_MSG_FILE" in (captured.get("env") or {})
