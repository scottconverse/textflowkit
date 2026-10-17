"""Keep console-child windows hidden when the desktop app has no console."""

import subprocess
import sys
from pathlib import Path


def hidden_process_kwargs() -> dict[str, int]:
    """Leave ordinary CLI launches and other operating systems unchanged."""
    if sys.platform == "win32" and (
        Path(sys.executable).name.lower() == "pythonw.exe"
        or sys.stdout is None or sys.stderr is None
    ):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def streaming_python() -> str:
    """A pythonw parent needs the console interpreter for worker stdio."""
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    if executable.name.lower() == "pythonw.exe" and console.is_file():
        return str(console)
    return sys.executable
