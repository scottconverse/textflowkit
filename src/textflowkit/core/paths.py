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


class UnsafeOutputPathError(ValueError):
    """Raised when a requested output directory escapes the allowed root."""


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
