"""Output path resolution.

Adapters accept a destination directory from a caller - a CLI user, an HTTP
client, or a model. That value must not be able to name an arbitrary location on
the host.

Policy:

- The process chooses an allowed root. `TEXTFLOWKIT_OUTPUT_ROOT` sets it; when
  unset the root is the current working directory. This keeps the CLI's default
  ("write where I ran it") working while giving a server operator one switch to
  confine every write.
- A requested directory must resolve *inside* that root. `..` segments, absolute
  paths elsewhere, and symlinks that escape are all rejected.
- The returned path is the resolved absolute path, so callers never re-interpret
  the raw string.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_OUTPUT_ROOT = "TEXTFLOWKIT_OUTPUT_ROOT"
ENV_INPUT_ROOT = "TEXTFLOWKIT_INPUT_ROOT"


class UnsafeOutputPathError(ValueError):
    """Raised when a requested output directory escapes the allowed root."""


class UnsafeInputPathError(ValueError):
    """Raised when a local input path escapes the allowed input root."""


def output_root() -> Path:
    """The directory all rendered output must live under."""
    raw = os.environ.get(ENV_OUTPUT_ROOT)
    base = Path(raw) if raw else Path.cwd()
    return base.expanduser().resolve()


def resolve_output_dir(requested: str | None) -> Path:
    """Resolve a caller-supplied output directory, confined to the allowed root.

    Raises `UnsafeOutputPathError` when the path escapes the root.
    """
    root = output_root()

    if requested is None or requested == "":
        target = root
    else:
        candidate = Path(requested).expanduser()
        target = candidate if candidate.is_absolute() else (root / candidate)

    # strict=False: the directory may not exist yet.
    resolved = target.resolve()

    if resolved != root and root not in resolved.parents:
        raise UnsafeOutputPathError(
            f"output directory '{requested}' is outside the allowed root "
            f"'{root}'. Set {ENV_OUTPUT_ROOT} to widen the root, or choose a "
            "path inside it."
        )
    return resolved


def ensure_output_dir(requested: str | None) -> Path:
    """Resolve and create the output directory."""
    resolved = resolve_output_dir(requested)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# --- input confinement ----------------------------------------------------

def resolve_input_path(requested: str | Path, *, root: str | Path | None) -> Path:
    """Resolve a local input path, optionally confined to `root`.

    Confinement is opt-in. When `root` is None the path is returned resolved but
    unrestricted, because the caller is the principal - a CLI user who typed the
    path themselves. The adapters pass a root by default, because there the
    caller may be a model acting on untrusted content.

    Raises `UnsafeInputPathError` when the path is outside the root, or when it
    does not name readable regular file.
    """
    candidate = Path(requested).expanduser()

    if root is None:
        resolved = candidate.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"no such file: {requested}")
        if not resolved.is_file():
            raise ValueError(f"not a file: {requested}")
        return resolved

    base = Path(root).expanduser().resolve()
    target = candidate if candidate.is_absolute() else (base / candidate)
    resolved = target.resolve()

    if resolved != base and base not in resolved.parents:
        raise UnsafeInputPathError(
            f"input path '{requested}' is outside the allowed input root "
            f"'{base}'. Set {ENV_INPUT_ROOT} to widen the root, or use a path "
            "inside it."
        )
    if not resolved.exists():
        raise FileNotFoundError(f"no such file: {requested}")
    if not resolved.is_file():
        raise ValueError(f"not a file: {requested}")
    return resolved


def default_input_root() -> Path | None:
    """The configured input root, or None when confinement is not requested."""
    raw = os.environ.get(ENV_INPUT_ROOT)
    return Path(raw).expanduser().resolve() if raw else None


def server_input_root() -> Path:
    """The input root a long-running adapter should enforce.

    Adapters default to confining local inputs to the current working directory,
    because their caller may be a model acting on untrusted content rather than
    the person who owns the machine. Set TEXTFLOWKIT_INPUT_ROOT to widen it:

        TEXTFLOWKIT_INPUT_ROOT=/            # no practical confinement
        TEXTFLOWKIT_INPUT_ROOT=/srv/media   # one directory
    """
    return default_input_root() or Path.cwd().resolve()
