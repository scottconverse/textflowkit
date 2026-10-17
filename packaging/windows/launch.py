"""Consoleless entry point for the private Windows runtime (also --selftest)."""

import ctypes
import os
import sys
import time
import traceback
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

APP_MUTEX = r"Local\TextFlowKit.Desktop"
SETUP_MUTEX = r"Local\TextFlowKit.Setup"


class SetupActiveError(OSError):
    """Setup is still replacing or removing the private runtime."""


def configure_environment(root: Path, environ=None) -> None:
    """Change only this process; explicit product overrides always win."""
    environ = os.environ if environ is None else environ
    environ["PATH"] = str(root / "bin") + os.pathsep + environ.get("PATH", "")
    for key in ("PYTHONHOME", "PYTHONPATH"):
        environ.pop(key, None)
    environ.setdefault("TEXTFLOWKIT_MODELS_DIR", str(root / "models"))
    environ.setdefault("TEXTFLOWKIT_OFFLINE", "1")


def _kernel32():
    if os.name != "nt":
        raise OSError("The installed desktop bootstrap requires Windows.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenMutexW.restype = wintypes.HANDLE
    return api


def _setup_running(api) -> bool:
    handle = api.OpenMutexW(0x100000, False, SETUP_MUTEX)
    if handle:
        api.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error != 2:  # ERROR_FILE_NOT_FOUND is the only proven absence.
        raise ctypes.WinError(error)
    return False


@contextmanager
def app_mutex():
    """Hold shared app presence, then refuse any active install/uninstall.

    Never wait on app presence: UI ownership still handles second launches.
    Creating presence before checking setup closes the upgrade/removal race.
    """
    api = _kernel32()
    handle = api.CreateMutexW(None, False, APP_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _setup_running(api):
            raise SetupActiveError("TextFlowKit installer or uninstaller is running")
        yield
    finally:
        api.CloseHandle(handle)


def wait_for_setup() -> None:
    """Only Inno's nowait postinstall launch waits for the setup process to exit."""
    api = _kernel32()
    deadline = time.monotonic() + 60
    while _setup_running(api):
        if time.monotonic() >= deadline:
            raise SetupActiveError("TextFlowKit installer is still running")
        time.sleep(0.1)


def report_failure(exc: Exception) -> None:
    """Native notification even if importing TextFlowKit itself failed."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    log = base / "TextFlowKit/ui/desktop-startup.log"
    # Do not log exception values, environment, arguments, or media contents.
    frames = traceback.extract_tb(exc.__traceback__)
    detail = type(exc).__name__ + "\n" + "".join(
        f"{frame.filename}:{frame.lineno} in {frame.name}\n" for frame in frames
    )
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(detail + "\n")
        message = (f"TextFlowKit could not start ({type(exc).__name__}).\n"
                   f"Run the installer again to repair the application.\nDetails: {log}")
    except OSError:
        message = (f"TextFlowKit could not start ({type(exc).__name__}). "
                   "Run the installer again to repair the application. Startup log unavailable.")
    if isinstance(exc, SetupActiveError):
        message = "TextFlowKit setup is running. Close the installer or uninstaller and try again."
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif os.name == "nt":
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.UINT]
        api.MessageBoxW.restype = ctypes.c_int
        api.MessageBoxW(None, message, "TextFlowKit did not start", 0x10)


def main(argv=None) -> int:
    sys.dont_write_bytecode = True
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        configure_environment(Path(__file__).resolve().parent)
        if argv and argv[0] == "--after-install":
            wait_for_setup()
            argv.pop(0)
        with app_mutex():
            if argv and argv[0] == "--selftest":
                from textflowkit.cli import main as cli_main

                return cli_main(["selftest", *argv[1:]])
            if argv == ["--version"]:
                from textflowkit import __version__

                print(__version__)
                return 0
            from textflowkit.ui.launcher import main as launcher_main

            return launcher_main(argv)
    except Exception as exc:  # noqa: BLE001 - final consoleless startup error boundary
        report_failure(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
