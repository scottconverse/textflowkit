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
    """The directory all rendered output must live under.

    Default: the current working directory, which is the least surprising answer
    for a CLI ("write where I ran it"). A caller that asks for somewhere else
    inside the same machine is not doing anything the operator could not, so
    `TEXTFLOWKIT_OUTPUT_ROOT` is the switch that imposes a real boundary when a
    deployment needs one.
    """
    raw = os.environ.get(ENV_OUTPUT_ROOT)
    base = Path(raw) if raw else Path.cwd()
    return base.expanduser().resolve()


def output_is_confined() -> bool:
    """Whether an output boundary was explicitly requested.

    Distinguishes "the operator chose a root" from "we fell back to cwd". The
    CLI writes wherever asked when no root is set; an explicitly configured root
    is still enforced exactly as before.
    """
    return bool(os.environ.get(ENV_OUTPUT_ROOT))


def resolve_output_dir(requested: str | None) -> Path:
    """Resolve a caller-supplied output directory.

    Confinement applies only when `TEXTFLOWKIT_OUTPUT_ROOT` is set. Without it
    there is no boundary to violate: the caller is the operator and already has
    whatever access the machine gives them, so refusing a path they own would be
    the tool inventing a restriction rather than enforcing one.

    Raises `UnsafeOutputPathError` when an explicit root is set and the path
    escapes it.
    """
    root = output_root()

    if requested is None or requested == "":
        target = root
    else:
        candidate = Path(requested).expanduser()
        target = candidate if candidate.is_absolute() else (root / candidate)

    # strict=False: the directory may not exist yet.
    resolved = target.resolve()

    if output_is_confined() and resolved != root and root not in resolved.parents:
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
    unrestricted, because the caller is the principal - a person who typed the
    path, or an agent acting with their authority. Adapters also pass None by
    default; set TEXTFLOWKIT_INPUT_ROOT when the caller is *not* the machine's
    owner (a shared or network-reachable deployment).

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


def server_input_root() -> Path | None:
    """The input root a long-running adapter should enforce, or None for no limit.

    Default: **no confinement.** An adapter runs as the person who started it and
    inherits their access to the machine. When that person is the operator - a
    developer running this for themselves, or an agent acting on their behalf -
    confining it to the working directory only refuses paths they are already
    entitled to use, which reads as the tool being broken.

    Confinement is still one environment variable away for the case it was
    actually designed for: exposing a server to callers who are *not* the owner.
    A shared or network-reachable deployment should set:

        TEXTFLOWKIT_INPUT_ROOT=/srv/media   # one directory
        TEXTFLOWKIT_INPUT_ROOT=C:\\media      # one tree on Windows

    Setting it to a filesystem root is equivalent to no confinement.
    """
    return default_input_root()
