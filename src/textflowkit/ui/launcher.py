"""The ``textflowkit-ui`` entry point.

Starts the local browser interface on loopback and opens a browser at it. The
process is the whole server: no daemon, no global service, no system-wide
config. It binds ``127.0.0.1`` by default and **refuses any non-loopback bind**,
even when ``TEXTFLOWKIT_ALLOW_REMOTE=1`` is set - that opt-in widens the
developer HTTP launcher, and the UI deliberately does not honour it, because a
browser interface with no authentication must not be reachable off the machine.

Responsibilities, each handled here:

- **Single owner per database.** The durable job store is owned by one UI
  process. An operating-system owner lock (Windows ``msvcrt`` / POSIX ``flock``,
  see :mod:`.ownership`) is taken *before* startup recovery, so a second launch
  against the same database does not reap the first process's jobs or open a
  competing server - it opens the running UI and exits. The kernel releases the
  lock on exit or crash, so a killed run never blocks the next launch; no pid is
  ever signalled or inspected.
- **Port selection.** ``--port`` names a preferred port. A free port is chosen
  and its socket is **reserved** by this process before anything else, so
  readiness is proven by our own socket, not by "something is listening". An
  occupied port is skipped, never probed-and-assumed-ready.
- **Durable, per-user storage defaults** (see :mod:`.paths`), applied before the
  store is created so the store picks up the durable SQLite path.
- **Browser open.** After the server has actually started serving, the default
  browser is opened once. ``--no-browser`` suppresses it.
- **Consoleless operation.** Under ``pythonw`` there is no console, so a startup
  failure is reported through a log file and a message box rather than a silent
  exit; a normal console launch keeps its ordinary stderr output.
- **Windows shortcut creation** via ``--create-shortcut``, which writes a ``.lnk``
  into the Start Menu (or a named directory) rather than doing anything hidden.
  It uses PowerShell's ``WScript.Shell`` COM object through a *file*, so it needs
  no extra Python dependency and no shell-quoting of operator input.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from textflowkit import __version__
from textflowkit.adapters.streaming_ws import streaming_enabled
from textflowkit.core.bind import is_loopback_host

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756
#: How long to wait, after the server reports it is serving, before opening a
#: browser. The socket is already bound and accepting before this runs, so this
#: is a small grace period, not a probe.
_READY_TIMEOUT_SECONDS = 15.0


# --- port selection -------------------------------------------------------


def _bind_socket(host: str, port: int) -> socket.socket | None:
    """Try to bind and listen on ``host:port``; return the socket or ``None``.

    The socket is bound and put in listen mode here, so ownership of the port is
    **taken** rather than assumed from a connect test. A caller that keeps the
    returned socket and hands it to the server has closed the race where the
    port is grabbed by someone else between a probe and a bind.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        return None
    return sock


def choose_port(host: str, preferred: int, *, span: int = 100) -> int:
    """The first free port at or after ``preferred``.

    Scans upward for ``span`` ports so a busy default does not block startup.
    Raises ``OSError`` if the whole range is taken, which the caller reports as a
    clear failure rather than proceeding to bind something arbitrary.

    This returns the *number* only and releases the socket immediately; the
    launcher reserves the real socket with :func:`reserve_port` before serving,
    so this function is a cheap preference resolver and the reserve is the
    authoritative claim.
    """
    for offset in range(span):
        candidate = preferred + offset
        if candidate > 65535:
            break
        sock = _bind_socket(host, candidate)
        if sock is not None:
            sock.close()
            return candidate
    raise OSError(f"no free port in {preferred}..{preferred + span - 1}")


def reserve_port(host: str, preferred: int, *, span: int = 100) -> tuple[int, socket.socket]:
    """Reserve a listening socket on the first free port, and return both.

    Unlike :func:`choose_port`, the returned socket stays open and is *the* socket
    the server will serve on. That is what makes readiness honest: the port is
    ours from before the first print until shutdown, so a browser pointed at it
    reaches this process, never an unrelated service that happened to hold the
    number a moment earlier.
    """
    for offset in range(span):
        candidate = preferred + offset
        if candidate > 65535:
            break
        sock = _bind_socket(host, candidate)
        if sock is not None:
            # Report the port actually bound: with ``preferred == 0`` the OS chose
            # one, and the caller (and the browser URL) must use that, not "0".
            actual = sock.getsockname()[1]
            return actual, sock
    raise OSError(f"no free port in {preferred}..{preferred + span - 1}")


# --- consoleless logging --------------------------------------------------


def _is_consoleless() -> bool:
    """Whether this process has no usable standard streams.

    A ``pythonw.exe`` process has ``sys.stdout`` and ``sys.stderr`` set to
    ``None``. uvicorn's own logging config opens a handler on ``sys.stderr`` and
    would fail on a ``None`` stream, so the launcher takes over logging entirely
    in that case rather than letting the library crash on the way in.
    """
    return sys.stdout is None or sys.stderr is None


def _open_launcher_log() -> tuple[object | None, Path | None]:
    """Open the launcher's log file in the per-user UI data dir.

    Returns ``(stream, path)``; ``(None, None)`` if the file cannot be opened -
    logging is a diagnostic and must never itself stop startup. The file records
    the launcher's own lifecycle lines only: no capability token, no source path,
    no media name, no transcript text is ever written here.
    """
    try:
        from textflowkit.ui import paths

        directory = paths.ui_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "ui-launcher.log"
        # Held open for the process lifetime on purpose: it is the sink that
        # replaces the missing console streams, not a scoped read/write.
        stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return None, None
    return stream, path


def _log_line(stream: object | None, message: str) -> None:
    """Write one timestamped line to the launcher log, if there is one."""
    if stream is None:
        return
    try:
        stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except (OSError, ValueError):  # pragma: no cover - a closed pipe is not fatal
        pass


def _report_startup_failure(message: str, *, log_stream: object | None) -> None:
    """Report a startup failure where a person will actually see it.

    On a console launch this is stderr, exactly as before. On a consoleless
    (``pythonw``) launch there is no stderr, so the message is written to the
    launcher log **and** shown in a native Windows message box, so a shortcut that
    fails to start is not a window that vanishes with no explanation.
    """
    _log_line(log_stream, message)
    if not _is_consoleless():
        try:
            print(message, file=sys.stderr)
        except (OSError, ValueError):  # pragma: no cover
            _show_message_box("TextFlowKit UI", message)
        return
    _show_message_box("TextFlowKit UI did not start", message)


def _show_message_box(title: str, message: str) -> None:
    """A native Windows message box, or a no-op off Windows.

    Implemented through PowerShell and the WinForms ``MessageBox`` entry point so
    it needs no Python dependency. The title and message are passed to PowerShell
    **in a file** named by an environment variable, never interpolated into a
    command line or a script string, so no part of them can be read as code.
    """
    if os.name != "nt":
        return
    script_file = _write_ps_file("msgbox", _MESSAGE_BOX_SCRIPT)
    data_file = _write_ps_file("msgbox-data", f"{title}\n{message}")
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_file),
            ],
            capture_output=True,
            timeout=120,
            check=False,
            env={**os.environ, "TFK_MSG_FILE": str(data_file)},
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pass
    finally:
        _unlink_quietly(script_file)
        _unlink_quietly(data_file)


#: The message box script. The text is read from the file named by
#: ``TFK_MSG_FILE`` (first line is the title, the rest the body), so nothing from
#: the caller is parsed as PowerShell.
_MESSAGE_BOX_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$data = Get-Content -LiteralPath $env:TFK_MSG_FILE -Raw;"
    '$lines = $data -split "`n", 2;'
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[System.Windows.Forms.MessageBox]::Show($lines[1], $lines[0],"
    " 'OK', 'Error') | Out-Null"
)


def _write_ps_file(prefix: str, script: str) -> Path:
    """Write a PowerShell script to the per-user temp area and return its path."""
    import tempfile

    fd, name = tempfile.mkstemp(prefix=f"tfk-ui-{prefix}-", suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(script)
    return Path(name)


# --- browser open ---------------------------------------------------------


def _open_browser_once(url: str) -> None:
    """Open ``url`` in the default browser. Best-effort: a missing browser is not
    an error, and the URL is printed/logged regardless so it is reachable by hand.
    """
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001, S110 - a headless box has no browser
        pass


# --- Windows shortcut -----------------------------------------------------


def _create_shortcut(*, host: str, port: int, destination: Path | None) -> Path:
    """Write a Windows ``.lnk`` that launches the UI with pythonw (no console).

    The shortcut targets **this interpreter's** ``pythonw.exe`` (falling back to
    ``python.exe``) and passes ``-m textflowkit.ui.launcher`` plus the chosen
    host/port, so it always starts the environment the UI is running in. It is
    built with PowerShell's ``WScript.Shell`` COM object, driven from a script
    **file**, and every value the script needs is handed over in a **JSON data
    file** that the script reads - not interpolated into a command line or a
    script string. Operator-supplied paths therefore never become shell syntax,
    and the launcher needs **no pywin32**: a plain ``textflowkit[http]`` install
    can create the shortcut. On non-Windows this raises ``OSError`` - the CLI flag
    is still accepted so the option is discoverable, but the honest answer is that
    only Windows shortcuts exist.

    The shortcut is only ever written to a Start Menu path or a directory the
    caller named; this function never hides a file.
    """
    if os.name != "nt":
        raise OSError("--create-shortcut is a Windows-only feature")

    target = Path(sys.executable).with_name("pythonw.exe")
    if not target.exists():
        target = Path(sys.executable)

    if destination is None:
        appdata = os.environ.get("APPDATA")
        base = (
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if appdata
            else Path.home() / "Start Menu" / "Programs"
        )
        destination = base / "TextFlowKit UI.lnk"
    destination.parent.mkdir(parents=True, exist_ok=True)

    arguments = f"-m textflowkit.ui.launcher --host {host} --port {port}"
    payload = {
        "target": str(target),
        "arguments": arguments,
        "working_directory": str(Path.home()),
        "description": "TextFlowKit local transcription interface",
        "shortcut_path": str(destination),
    }
    script = _shortcut_script()
    script_file = _write_ps_file("shortcut", script)
    data_file = _write_ps_file("shortcut-data", json.dumps(payload))
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_file),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "TFK_SHORTCUT_DATA": str(data_file)},
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not run PowerShell to create the shortcut: {exc}") from exc
    finally:
        _unlink_quietly(script_file)
        _unlink_quietly(data_file)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise OSError(f"shortcut creation failed: {detail or 'PowerShell error'}")
    if not destination.exists():
        raise OSError("shortcut creation reported success but wrote no file")
    return destination


def _shortcut_script() -> str:
    """The PowerShell that writes the ``.lnk`` from the JSON data file.

    The data file path arrives in the ``TFK_SHORTCUT_DATA`` environment variable;
    the object is read with ``ConvertFrom-Json`` and its fields are used as
    *values*. Nothing from the caller is parsed as code or as command-line syntax.
    """
    return (
        "$ErrorActionPreference='Stop';"
        "$data = Get-Content -LiteralPath $env:TFK_SHORTCUT_DATA -Raw | ConvertFrom-Json;"
        "$shell = New-Object -ComObject WScript.Shell;"
        "$sc = $shell.CreateShortcut($data.shortcut_path);"
        "$sc.TargetPath = $data.target;"
        "$sc.Arguments = $data.arguments;"
        "$sc.WorkingDirectory = $data.working_directory;"
        "$sc.Description = $data.description;"
        "$sc.Save();"
    )


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


# --- argument parsing -----------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="textflowkit-ui",
        description=(
            "Start the TextFlowKit local browser interface on loopback and open "
            "it in a browser. This is not the developer HTTP API launcher; that "
            "is `textflowkit-http`."
        ),
    )
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind host (default {DEFAULT_HOST}; only loopback is accepted)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"preferred port; the next free one is used if taken (default {DEFAULT_PORT})",
    )
    p.add_argument("--no-browser", action="store_true", help="do not open a browser; just serve")
    p.add_argument(
        "--open-existing",
        action="store_true",
        help="if a UI already owns this database (its OS lock is held), open it and exit (the default)",
    )
    p.add_argument(
        "--create-shortcut",
        action="store_true",
        help="write a Windows Start Menu shortcut that launches the UI, then exit",
    )
    p.add_argument(
        "--shortcut-dir",
        default=None,
        help="directory for --create-shortcut (default: the Start Menu Programs folder)",
    )
    p.add_argument("--version", action="version", version=f"textflowkit-ui {__version__}")
    return p


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Loopback only. The UI does not honour TEXTFLOWKIT_ALLOW_REMOTE: a
    # browser-facing, unauthenticated interface must not be exposable by an
    # environment variable meant for the developer API.
    if not is_loopback_host(args.host):
        print(
            f"error: the local UI serves loopback only; refusing to bind '{args.host}'. "
            "Use 127.0.0.1 (or ::1). To expose the API instead, use "
            "`textflowkit-http --allow-remote`.",
            file=sys.stderr,
        )
        return 2

    if args.create_shortcut:
        return _run_create_shortcut(args)

    # Durable per-user storage defaults, before the store is built.
    from textflowkit.ui import paths as ui_paths

    applied = ui_paths.apply_defaults()
    try:
        ui_paths.ensure_data_dirs()
    except OSError as exc:
        print(f"error: could not create the data directory: {exc}", file=sys.stderr)
        return 2

    consoleless = _is_consoleless()
    log_stream, log_path = _open_launcher_log() if consoleless else (None, None)

    # One owner per database. Taken before recovery so a second launch cannot
    # reap the first launch's interrupted jobs or serve a competing UI.
    from textflowkit.ui import ownership

    db_path = os.environ.get(ui_paths.ENV_DB) or str(ui_paths.default_db_path())
    try:
        lock = ownership.acquire_owner_lock(db_path)
    except ownership.AlreadyRunningError as exc:
        if exc.url and not args.no_browser:
            print(f"note: {exc}", file=sys.stderr if not consoleless else None)
            _log_line(log_stream, str(exc))
            _open_browser_once(exc.url)
            return 0
        message = f"error: {exc}"
        _report_startup_failure(message, log_stream=log_stream)
        return 2
    except OSError as exc:
        message = f"error: could not establish the UI owner lock: {exc}"
        _report_startup_failure(message, log_stream=log_stream)
        return 2

    try:
        return _serve(
            args,
            applied,
            consoleless=consoleless,
            log_stream=log_stream,
            log_path=log_path,
            lock=lock,
        )
    finally:
        lock.release()


def _serve(
    args: argparse.Namespace,
    applied: dict[str, str],
    *,
    consoleless: bool,
    log_stream: object | None,
    log_path: Path | None,
    lock: object,
) -> int:
    # Reserve the socket *before* anything else, so the port is genuinely ours and
    # readiness is proven by this socket rather than by a probe of some other
    # process's listener.
    try:
        port, reserved = reserve_port(args.host, args.port)
    except OSError as exc:
        _report_startup_failure(f"error: {exc}", log_stream=log_stream)
        return 2
    if port != args.port:
        note = f"note: port {args.port} is in use; using {port}"
        if consoleless:
            _log_line(log_stream, note)
        else:
            print(note, file=sys.stderr)

    # The app must be built for the *chosen* port, because that defines the exact
    # origin the request gate accepts.
    from textflowkit.ui.app import create_app

    try:
        app = create_app(host=args.host, port=port)
    except Exception as exc:  # noqa: BLE001 - report a clear startup failure
        reserved.close()
        _report_startup_failure(
            f"error: could not build the UI: {type(exc).__name__}: {exc}",
            log_stream=log_stream,
        )
        return 1

    # If live streaming is opted into, the WebSocket extra must be present, or the
    # UI would advertise a stream endpoint it cannot complete a handshake on. Fail
    # clearly here rather than serving a broken route.
    if streaming_enabled():
        from textflowkit.adapters.http_server import streaming_dependency_problem

        problem = streaming_dependency_problem()
        if problem is not None:
            reserved.close()
            _report_startup_failure(f"error: {problem}", log_stream=log_stream)
            return 2

    url = f"http://{args.host}:{port}/"
    lines = [
        f"textflowkit {__version__} local UI",
        f"  serving : {url}",
        f"  store   : {os.environ.get('TEXTFLOWKIT_DB')}",
        f"  output  : {os.environ.get('TEXTFLOWKIT_OUTPUT_ROOT')}",
        f"  work    : {os.environ.get('TEXTFLOWKIT_WORK_ROOT')}",
    ]
    lines += [f"  default {name}={value}" for name, value in applied.items()]
    if streaming_enabled():
        lines.append(f"  stream  : {url.rstrip('/')}/api/streaming-example")
    lines += [
        "  stop    : press Ctrl+C, or use 'Stop server' in the workspace",
        "  note    : do not point a second process at this same database file",
    ]
    for line in lines:
        if consoleless:
            _log_line(log_stream, line)
        else:
            print(line)
    if consoleless and log_path is not None:  # pragma: no cover - consoleless path
        _log_line(log_stream, f"  log     : {log_path}")

    try:
        lock.write_metadata(url=url, version=__version__)
    except Exception:  # noqa: BLE001, S110 - discovery metadata is a convenience
        pass

    return _run_server(
        app,
        args,
        port=port,
        reserved=reserved,
        url=url,
        consoleless=consoleless,
        log_stream=log_stream,
    )


def _run_server(
    app,
    args: argparse.Namespace,
    *,
    port: int,
    reserved: socket.socket,
    url: str,
    consoleless: bool,
    log_stream: object | None,
) -> int:
    """Serve on the reserved socket until shutdown, then drain workers.

    uvicorn is driven with ``Server.serve(sockets=[reserved])`` rather than
    ``run(port=...)``: the socket is already bound and listening, so the server
    cannot bind a different port than the one the app was built for. The startup
    hook fires once the socket is actually serving, and *that* is what opens the
    browser - not a periodic connect probe that a stranger's listener could
    satisfy.
    """
    import uvicorn

    config_kwargs = {
        "app": app,
        "host": args.host,
        "port": port,
        "proxy_headers": False,
        # WebSocket bounds, applied whether or not live streaming is opted into (so
        # they also cover the mounted app's own ws surface and cost nothing when
        # idle). ``ws_max_size`` sits just above the one-second audio frame the
        # streaming protocol allows, so an oversize frame is refused at the
        # transport before the handler sees it; ``ws_max_queue`` is small so a peer
        # cannot queue frames without bound while the handler is busy. Per-message
        # deflate is off: the live payload is already-compact PCM.
        "ws_max_size": 32768,
        "ws_max_queue": 5,
        "ws_per_message_deflate": False,
    }
    if consoleless:
        # Under pythonw there is no stdout/stderr for uvicorn's default handlers
        # to attach to. Disable its logging config and let the launcher's log file
        # be the record, so uvicorn never touches a None stream.
        config = uvicorn.Config(log_config=None, log_level="warning", **config_kwargs)
        _redirect_stdio_to_log(log_stream)
    else:
        config = uvicorn.Config(log_level="info", **config_kwargs)

    server = uvicorn.Server(config)

    # Hand this process's own server to the app's shutdown controller, so a
    # "Stop server" request from the workspace exits *this* server through its
    # supported mechanism (draining first) rather than signalling any pid.
    _attach_shutdown_owned_server(app, server)

    opened = {"done": False}

    async def _on_started() -> None:
        # uvicorn awaits this on every main-loop tick, so it must be a coroutine.
        # It fires once the socket is actually serving - readiness by our own
        # server, not a connect probe. Best-effort: a missing browser is not fatal.
        if opened["done"]:
            return
        opened["done"] = True
        if not args.no_browser:
            threading.Thread(
                target=_open_browser_once,
                args=(url,),
                name="textflowkit-ui-open-browser",
                daemon=True,
            ).start()

    server.config.callback_notify = _on_started  # type: ignore[attr-defined]

    try:
        server.run(sockets=[reserved])
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    except Exception as exc:  # noqa: BLE001 - a serve failure must be visible
        _report_startup_failure(
            f"error: the UI server stopped: {type(exc).__name__}: {exc}",
            log_stream=log_stream,
        )
        return 1
    finally:
        try:
            reserved.close()
        except OSError:  # pragma: no cover
            pass
    # The app's lifespan shutdown hook already drained the worker pool; uvicorn
    # runs lifespan shutdown on a clean exit. Nothing extra to stop here.
    return 0


def _attach_shutdown_owned_server(app, server) -> None:
    """Give the app its own server object for the controlled-shutdown endpoint.

    The app's shutdown controller is created inside ``create_app``; it exposes an
    ``attach_server`` used here. Kept as a tiny, tolerant helper so a mismatched or
    embedded app (a test harness) never breaks startup - the endpoint would then
    simply refuse, which is the honest outcome, rather than the launcher failing.
    """
    attach = getattr(app, "ui_attach_owned_server", None) or getattr(
        app.state, "ui_attach_owned_server", None
    )
    if callable(attach):
        attach(server)


def _redirect_stdio_to_log(log_stream: object | None) -> None:
    """Point ``sys.stdout``/``sys.stderr`` at the launcher log for a consoleless run.

    uvicorn and other libraries write to these streams; with ``pythonw`` they are
    ``None`` and a write would raise. Replacing them with the log file keeps the
    process alive and keeps every diagnostic in one place. No token, source, or
    transcript text is written by the launcher, and uvicorn at ``--log-level
    warning`` emits only its own operational lines.
    """
    if log_stream is None:
        return
    try:
        sys.stdout = log_stream  # type: ignore[assignment]
        sys.stderr = log_stream  # type: ignore[assignment]
    except Exception:  # noqa: BLE001, S110 - pragma: no cover - best effort
        pass


def _run_create_shortcut(args: argparse.Namespace) -> int:
    destination = None
    if args.shortcut_dir:
        # A directory means "put the shortcut in here"; a path ending in .lnk is
        # taken as the exact file to write.
        chosen = Path(args.shortcut_dir).expanduser()
        destination = chosen if chosen.suffix.lower() == ".lnk" else chosen / "TextFlowKit UI.lnk"
    try:
        path = _create_shortcut(host=args.host, port=args.port, destination=destination)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"shortcut written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
