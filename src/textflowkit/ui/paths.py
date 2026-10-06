"""Per-user data locations for the local browser interface.

The local UI is a fresh entry point that is not the developer HTTP launcher, so
it owns its own default storage locations. They must be *durable* by default -
a transcription that took twenty minutes must be resumable after the window is
closed - and they must live under the operating system's conventional per-user
data directory, never inside the repository or the current working directory.

Precedence, per the approved scope:

- An **explicit override already in the environment wins**. If the operator set
  ``TEXTFLOWKIT_DB`` / ``TEXTFLOWKIT_WORK_ROOT`` / ``TEXTFLOWKIT_OUTPUT_ROOT`` /
  ``TEXTFLOWKIT_INPUT_ROOT``, the UI must not silently move their data. This
  module only fills in what was left unset.
- Otherwise the UI's own default under ``%LOCALAPPDATA%\\TextFlowKit\\ui`` on
  Windows (``$XDG_DATA_HOME/textflowkit/ui`` or ``~/.local/share/...`` on POSIX,
  ``~/Library/Application Support/...`` on macOS).

There is deliberately **no global config file**. The environment is the only
configuration, so a UI started from a shortcut and one started from a terminal
read the same settings and nothing is written where an operator would not look.
Every value here is process-local: ``apply_defaults`` writes into this process's
``os.environ`` and nothing else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Appended to the platform data dir. Kept as two segments so a future product
#: name change is one edit, and so the directory is namespaced under the same
#: "TextFlowKit" folder a person would expect to find on disk.
APP_DIR_NAME = "TextFlowKit"
SUBDIR = "ui"

#: Environment variables the UI fills in *only when they are not already set*.
#: These are the same names the core already reads - the UI adds no new names
#: for storage, so a setting that works for the CLI works here.
ENV_DB = "TEXTFLOWKIT_DB"
ENV_WORK_ROOT = "TEXTFLOWKIT_WORK_ROOT"
ENV_OUTPUT_ROOT = "TEXTFLOWKIT_OUTPUT_ROOT"
ENV_INPUT_ROOT = "TEXTFLOWKIT_INPUT_ROOT"


def user_data_dir() -> Path:
    """The per-user data directory for this application.

    Windows: ``%LOCALAPPDATA%\\TextFlowKit`` (falling back to
    ``%APPDATA%`` and then ``~`` when neither is set - a service account may
    have neither). POSIX: ``$XDG_DATA_HOME/textflowkit`` or
    ``~/.local/share/textflowkit``. macOS: ``~/Library/Application
    Support/TextFlowKit``.

    The value is *not* created here; callers create only the subdirectories they
    actually use, so starting the UI never litters a directory it does not use.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home()
        return root / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "textflowkit"


def ui_data_dir() -> Path:
    """``<user_data_dir>/ui`` - the UI's own root for durable state."""
    return user_data_dir() / SUBDIR


def default_db_path() -> Path:
    return ui_data_dir() / "jobs.sqlite3"


def default_work_dir() -> Path:
    #: Scratch: decoded audio and staged uploads. Not where finished output goes.
    return ui_data_dir() / "work"


def default_uploads_dir() -> Path:
    """Where streamed uploads are persisted.

    Inside ``work`` on purpose: a staged upload is scratch the pipeline decodes
    from, not a deliverable, so it belongs with the other scratch and is cleaned
    up with it rather than presented as a saved file.

    It follows the *effective* work root: an operator who redirected
    ``TEXTFLOWKIT_WORK_ROOT`` gets uploads under that root, not under the
    built-in default beside it, so a staged upload always lives with the scratch
    the pipeline will read it from.
    """
    raw = os.environ.get(ENV_WORK_ROOT)
    base = Path(raw).expanduser() if raw else default_work_dir()
    return base / "uploads"


def default_output_dir() -> Path:
    return ui_data_dir() / "outputs"


def apply_defaults() -> dict[str, str]:
    """Fill in the UI's durable defaults for any storage variable left unset.

    Returns a mapping of ``name -> value`` for the variables this call actually
    set (empty when every variable was already present), which the launcher
    prints so an operator can see where their data will land. An explicit
    environment override is left exactly as it was - the value is returned
    unchanged in neither the set nor the unset case, only *set* variables are
    reported.

    ``TEXTFLOWKIT_DB`` is set to a durable SQLite path, so jobs and checkpoints
    survive a restart. The work and output roots are pointed at the per-user
    directory too, so a UI run does not drop decoded WAVs or rendered subtitles
    into whatever directory it happened to start in.
    """
    applied: dict[str, str] = {}
    defaults = {
        ENV_DB: str(default_db_path()),
        ENV_WORK_ROOT: str(default_work_dir()),
        ENV_OUTPUT_ROOT: str(default_output_dir()),
    }
    for name, value in defaults.items():
        if not os.environ.get(name):
            os.environ[name] = value
            applied[name] = value
    return applied


def ensure_data_dirs() -> dict[str, Path]:
    """Create the UI's data directories and return the resolved paths.

    Only the directories named by the *effective* environment are created, so an
    operator who redirected ``TEXTFLOWKIT_OUTPUT_ROOT`` somewhere else does not
    get an empty default output directory beside it.
    """
    dirs: dict[str, Path] = {}
    for name, default in (
        (ENV_WORK_ROOT, default_work_dir()),
        (ENV_OUTPUT_ROOT, default_output_dir()),
    ):
        raw = os.environ.get(name)
        path = Path(raw).expanduser() if raw else default
        path.mkdir(parents=True, exist_ok=True)
        dirs[name] = path
    # The DB lives in a directory that is its own, independent of the work root.
    raw_db = os.environ.get(ENV_DB)
    if raw_db:
        Path(raw_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return dirs


def max_upload_bytes() -> int:
    """The hard cap on a single uploaded file, in bytes.

    The UI's own bound, distinct from the production media-size limit: this one
    bounds what a browser is allowed to send to the local server *before* the
    pipeline ever sees it. Default 2 GiB; override with
    ``TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES`` (a positive integer). A malformed value
    is refused loudly rather than silently replaced, because a limit that does
    not do what an operator set it to is worse than no limit.
    """
    raw = os.environ.get(ENV_MAX_UPLOAD_BYTES)
    default = 2 * 1024 * 1024 * 1024
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_MAX_UPLOAD_BYTES} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{ENV_MAX_UPLOAD_BYTES} must be a positive integer")
    return value


ENV_MAX_UPLOAD_BYTES = "TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES"
