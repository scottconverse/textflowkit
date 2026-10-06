"""Whistle runtime assets: the pinned native binary and the model file.

Whistle is Cactus Compute's speech-to-text model. It ships as a single
``.cact`` file loaded by a per-platform native CLI (``needle``/``needle.exe``).
This package therefore needs two files that are *not* part of the wheel and
are never committed to source:

- the **native CLI** for the current OS/architecture, and
- the **model** (``whistle.cact``).

Both are pinned here by exact size and SHA-256 and fetched from HTTPS on first
use. Nothing in this module contacts the network at import time, during name
validation, or in a doctor/self-check: the network is touched only by an
explicit :func:`ensure_assets` call, and never at all when offline mode is on.

Two upstream facts drive the shape of this module:

- Upstream's README documents that the shipping binary enables **telemetry by
  default** and disables it only when ``NEEDLE_TELEMETRY=0`` and
  ``DO_NOT_TRACK=1`` are set. So this product forces both into the child
  environment of every launch, overriding a parent that opted in
  (:func:`whistle_child_env`). The upstream Python telemetry module is
  deliberately *not* imported, vendored, or depended on.

  Honesty boundary: a static-string scan of the pinned Windows x64 binary did
  not find the literal ``NEEDLE_TELEMETRY`` / ``DO_NOT_TRACK`` strings, and the
  only URL it contains is a loopback ``http://127.0.0.1:%d`` server. That is a
  narrow probe, not proof: it does **not** establish that the binary performs
  no network I/O, nor that tracking code is physically absent from any pinned
  build. What is claimed is narrower and checkable - the child environment is
  forced telemetry-off at every launch, and this product sends no telemetry of
  its own. The env gate is applied because the README states it is the gate for
  the shipped binary; it is not claimed to be a verified property of every
  pinned build.
- Upstream publishes native binaries for a fixed set of platform folders and
  **does not publish an Intel-Mac binary**. This product refuses an unsupported
  platform with a clear message and points at the explicitly selectable Whisper
  engine rather than silently substituting another engine.

The download is the only network operation, and it names exactly two pinned
URLs; a local recording path is never placed in a request.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Environment overrides. A caller (or a deployment) can point the cache
#: somewhere explicit, force offline behaviour, or bound the download.
ENV_MODELS_DIR = "TEXTFLOWKIT_MODELS_DIR"
ENV_OFFLINE = "TEXTFLOWKIT_OFFLINE"
ENV_DOWNLOAD_TIMEOUT_SECONDS = "TEXTFLOWKIT_WHISTLE_DOWNLOAD_TIMEOUT_SECONDS"
ENV_MAX_DOWNLOAD_BYTES = "TEXTFLOWKIT_WHISTLE_MAX_DOWNLOAD_BYTES"

#: The pinned upstream revision. Recorded for provenance, not used to build a
#: URL by itself (the URL below is fixed): a revision that moved must not be
#: able to change what this product runs.
WHISTLE_MODEL_REVISION = "b358ddadd89b7a713b5aa131f23032d3cca1b251"
NEEDLE_BINARY_REVISION = "2ae11323dc000f5e70c49f7403efa6af12ba9e67"

#: Wall-clock ceiling for a whole asset download when the caller sets none.
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 600
#: Byte ceiling for a whole asset download when the caller sets none. Both
#: pinned files together are under 20 MB, so this is generous but not unbounded.
DEFAULT_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024

#: Read size for the streaming download.
_CHUNK_BYTES = 1024 * 1024


class WhistleAssetError(RuntimeError):
    """A Whistle runtime asset is missing, unsupported, or failed to verify."""


@dataclass(frozen=True, slots=True)
class PinnedAsset:
    """One pinned file: where it comes from and what it must be."""

    filename: str
    url: str
    sha256: str
    size: int


#: The model. One file, shared by every platform. The size and hash are the
#: values the upstream repository reports for this exact revision, cross-checked
#: against the file itself (see reports/W1.md).
WHISTLE_MODEL = PinnedAsset(
    filename="whistle.cact",
    url=(
        "https://huggingface.co/Cactus-Compute/whistle/resolve/"
        f"{WHISTLE_MODEL_REVISION}/whistle.cact"
    ),
    sha256="b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb",
    size=16919407,
)


@dataclass(frozen=True, slots=True)
class PlatformBinary:
    """The native CLI for one published platform folder."""

    folder: str
    asset: PinnedAsset


def _binary(filename: str, folder: str, sha256: str, size: int) -> PlatformBinary:
    return PlatformBinary(
        folder=folder,
        asset=PinnedAsset(
            filename=filename,
            url=(
                "https://huggingface.co/Cactus-Compute/needle3/resolve/"
                f"{NEEDLE_BINARY_REVISION}/{folder}/{filename}"
            ),
            sha256=sha256,
            size=size,
        ),
    )


#: Exactly the platform folders upstream publishes a native CLI for. There is
#: deliberately no ``macos-x86_64`` entry: upstream publishes none, and this
#: product does not invent a build it cannot verify.
#
#: The SHA-256 values are the LFS digests from the pinned upstream listing
#: (`siblings[].lfs.sha256` in outputs/whistle-integration-20261005/
#: needle3-pinned-api.json) - the digest of the file *content*, not the git
#: tree/blob id. A tree blob id is not a content hash and must never be used
#: here. Only the Windows x64 binary has additionally been hashed locally
#: (reports/W1.md).
PLATFORM_BINARIES: dict[str, PlatformBinary] = {
    "windows-x86_64": _binary(
        "needle.exe", "windows-x86_64",
        "e8863ca0c06a47d406777077f1ba728b58e57d77fedff252dbe6827d588acc8c",
        1563136,
    ),
    "windows-arm64": _binary(
        "needle.exe", "windows-arm64",
        "8a333b4829f4a6f230e26ab1998c17ad442a97c650934448db1c63eed91b0cea",
        1352704,
    ),
    "linux-x86_64": _binary(
        "needle", "linux-x86_64",
        "f38dc4b0345d66b4e385734ad0f12af43ac6e2cfa1752d0795af5c137200c8e4",
        1541680,
    ),
    "linux-arm64": _binary(
        "needle", "linux-arm64",
        "6fc25a97def475e1c7d1933a4a219d4e37076be77974e76dd331d9e0cd97f3cb",
        1431816,
    ),
    "macos-arm64": _binary(
        "needle", "macos-arm64",
        "342fa2c6f140e702354a99c4201c9057535ec908eed35c7382e911a19d1d2724",
        1089896,
    ),
}

#: The platforms upstream publishes, for an error message that names the choice.
SUPPORTED_PLATFORMS: tuple[str, ...] = tuple(PLATFORM_BINARIES)


def current_platform() -> str:
    """The published platform folder for the running interpreter, or a refusal.

    Raises :class:`WhistleAssetError` for a platform upstream does not build
    for. The message is explicit and recommends the Whisper engine, which is
    selectable on that platform - this product never substitutes an engine the
    caller did not name.
    """
    system = sys.platform
    machine = _platform.machine().lower()

    if system.startswith("win"):
        os_folder, arch = "windows", machine
    elif system == "darwin":
        os_folder, arch = "macos", machine
    elif system.startswith("linux"):
        os_folder, arch = "linux", machine
    else:
        raise WhistleAssetError(
            f"Whistle has no native runtime for platform '{system}'. "
            f"Supported platforms: {', '.join(SUPPORTED_PLATFORMS)}. "
            "Select the Whisper engine explicitly instead (engine='whisper')."
        )

    # Normalise the machine names the platforms actually report.
    if arch in ("amd64", "x86_64", "x64"):
        arch = "x86_64"
    elif arch in ("arm64", "aarch64"):
        arch = "arm64"

    if os_folder == "macos" and arch == "x86_64":
        # Called out on its own: this is the common "Intel Mac" case, and the
        # upstream project does not publish a binary for it. Say so, and name
        # the engine that does work there, rather than a generic "unsupported".
        raise WhistleAssetError(
            "Whistle has no published native runtime for Intel Mac "
            "(macos-x86_64). Apple Silicon (macos-arm64) is supported. "
            "Select the Whisper engine explicitly instead (engine='whisper'); "
            "this product will not silently run a different engine for you."
        )

    folder = f"{os_folder}-{arch}"
    if folder not in PLATFORM_BINARIES:
        raise WhistleAssetError(
            f"Whistle has no native runtime for '{folder}'. "
            f"Supported platforms: {', '.join(SUPPORTED_PLATFORMS)}. "
            "Select the Whisper engine explicitly instead (engine='whisper')."
        )
    return folder


# --- cache discovery -------------------------------------------------------


def models_dir() -> Path:
    """The deterministic, absolute directory holding Whistle assets.

    ``TEXTFLOWKIT_MODELS_DIR`` overrides it. Otherwise it is a per-user cache:
    ``%LOCALAPPDATA%\\TextFlowKit\\whistle`` on Windows, else
    ``$XDG_CACHE_HOME/textflowkit/whistle`` or ``~/.cache/textflowkit/whistle``.
    The path is always absolute and never inside this package's source tree, so
    assets are never written into an installed (possibly read-only) wheel.
    """
    override = os.environ.get(ENV_MODELS_DIR)
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return (Path(base) / "TextFlowKit" / "whistle").resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return (base / "textflowkit" / "whistle").resolve()


def _offline() -> bool:
    return os.environ.get(ENV_OFFLINE, "").strip().lower() in {"1", "true", "yes", "on"}


def is_offline() -> bool:
    """Whether this process has been told not to use the network."""
    return _offline()


def _pinned_paths(folder: str) -> tuple[Path, Path]:
    binary = PLATFORM_BINARIES[folder]
    root = models_dir()
    return root / binary.folder / binary.asset.filename, root / WHISTLE_MODEL.filename


def verify_pinned(path: Path, asset: PinnedAsset) -> None:
    """Raise unless ``path`` is exactly the pinned file (size, then SHA-256).

    Size is checked first: it is a cheap refusal for a truncated or swapped
    file before any hashing. Hashing is streamed, so a large file is never held
    in memory.
    """
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise WhistleAssetError(f"cannot read asset '{path}': {exc}") from exc
    if actual_size != asset.size:
        raise WhistleAssetError(
            f"asset '{path}' has size {actual_size}, expected {asset.size}"
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise WhistleAssetError(f"cannot read asset '{path}': {exc}") from exc
    actual = digest.hexdigest()
    if actual != asset.sha256:
        raise WhistleAssetError(
            f"asset '{path}' has sha256 {actual}, expected {asset.sha256}"
        )


def cache_status(folder: str | None = None) -> dict[str, object]:
    """Describe the cache without touching the network or verifying bytes.

    Cheap enough for a doctor/self-check: it reports what is *present*, not
    whether it is valid. Callers that need validity call :func:`ensure_assets`.
    """
    folder = folder or current_platform()
    binary_path, model_path = _pinned_paths(folder)
    return {
        "platform": folder,
        "models_dir": str(models_dir()),
        "offline": _offline(),
        "binary": str(binary_path),
        "binary_present": binary_path.exists(),
        "model": str(model_path),
        "model_present": model_path.exists(),
    }


# --- download --------------------------------------------------------------


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WhistleAssetError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise WhistleAssetError(f"{name} must be a positive integer")
    return value


def _assert_pinned_scheme(url: str) -> None:
    """Every asset URL this product fetches must be HTTPS.

    Kept as its own function so the one refusal is stated and tested in one
    place; a test that must exercise a loopback HTTP server patches this rather
    than the production check being weakened.
    """
    if not url.startswith("https://"):
        raise WhistleAssetError(f"refusing non-HTTPS asset URL: {url[:80]}")


def _download_to_temp(asset: PinnedAsset, *, timeout: int, max_bytes: int) -> Path:
    """Fetch ``asset`` to a temp file beside its final home.

    Bounded twice: an overall wall-clock ``timeout`` and a running byte cap
    ``max_bytes``. The ``Content-Length`` header, when present, is checked
    before any body is read. Only the pinned HTTPS URL is requested; no local
    path ever enters the request.
    """
    _assert_pinned_scheme(asset.url)

    deadline = time.monotonic() + timeout
    try:
        request = urllib.request.Request(asset.url, headers={"User-Agent": "textflowkit"})
        with urllib.request.urlopen(request, timeout=min(timeout, 60)) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    too_large = int(declared) > max_bytes
                except (TypeError, ValueError):
                    too_large = False
                if too_large:
                    raise WhistleAssetError(
                        "asset download Content-Length exceeds the configured size limit"
                    )
            # The temp file lives in the same directory as the final file so
            # the final placement is an atomic same-filesystem rename.
            # `mkstemp` returns an *open* descriptor; close it immediately, or
            # the later `os.replace` fails on Windows with a sharing violation.
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{asset.filename}.", suffix=".part", dir=str(models_dir())
            )
            os.close(fd)
            destination = Path(temp_name)
            written = 0
            try:
                with destination.open("wb") as handle:
                    while True:
                        if time.monotonic() > deadline:
                            raise WhistleAssetError(
                                f"asset download timed out after {timeout}s"
                            )
                        chunk = response.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise WhistleAssetError(
                                "asset download exceeds the configured size limit"
                            )
                        handle.write(chunk)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
    except WhistleAssetError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise WhistleAssetError(f"asset download failed: {exc}") from exc
    return destination


def _install_pinned(asset: PinnedAsset, final: Path, *, timeout: int, max_bytes: int) -> Path:
    """Download, verify, and atomically install one pinned asset at ``final``."""
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = _download_to_temp(asset, timeout=timeout, max_bytes=max_bytes)
    try:
        # Verify the bytes on disk before they become visible under the final
        # name: a truncated or swapped download never reaches the cache.
        verify_pinned(temp, asset)
        os.replace(temp, final)
        if os.name != "nt" and final.name.startswith("needle"):
            # The native CLI is executed directly; make the bit explicit rather
            # than relying on the umask.
            final.chmod(final.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    finally:
        temp.unlink(missing_ok=True)
    return final


def ensure_assets(
    *,
    offline: bool | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
) -> tuple[Path, Path]:
    """Return ``(binary_path, model_path)``, downloading on first use.

    A file already in the cache is trusted only after it verifies against its
    pinned size and hash; a cached file that fails verification is refused
    rather than replaced silently in offline mode, and re-downloaded otherwise.

    Never called at import time, during name validation, or by a doctor. In
    offline mode a missing asset raises instead of fetching.
    """
    folder = current_platform()
    binary_path, model_path = _pinned_paths(folder)
    binary = PLATFORM_BINARIES[folder].asset

    timeout = timeout if timeout is not None else _positive_env_int(
        ENV_DOWNLOAD_TIMEOUT_SECONDS, DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    )
    max_bytes = max_bytes if max_bytes is not None else _positive_env_int(
        ENV_MAX_DOWNLOAD_BYTES, DEFAULT_MAX_DOWNLOAD_BYTES
    )
    is_offline_mode = _offline() if offline is None else offline

    def _resolve(path: Path, asset: PinnedAsset) -> Path:
        if path.exists():
            verify_pinned(path, asset)
            return path
        if is_offline_mode:
            raise WhistleAssetError(
                f"Whistle asset '{path}' is not in the cache and offline mode is on "
                f"({ENV_OFFLINE}); run once without offline mode to fetch it, or seed "
                "the cache with the verified file."
            )
        return _install_pinned(asset, path, timeout=timeout, max_bytes=max_bytes)

    # The model is the smaller and more reused file; fetching it first means a
    # failed binary fetch has still left a valid model cached.
    resolved_model = _resolve(model_path, WHISTLE_MODEL)
    resolved_binary = _resolve(binary_path, binary)
    return resolved_binary, resolved_model


# --- child environment -----------------------------------------------------


def whistle_child_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a native Whistle launch, telemetry forced off.

    Upstream's README documents that the shipping binary enables telemetry by
    default and that it is disabled by ``NEEDLE_TELEMETRY=0`` and
    ``DO_NOT_TRACK=1``. A parent that opted in is overridden: this product does
    not forward a telemetry opt-in. ``CI`` is set as well, since upstream's own
    gate also disables tracking under CI.

    This is the documented gate, applied unconditionally - it is not a claim
    that tracking code is physically absent from any binary. The upstream Python
    telemetry layer is not imported or vendored anywhere in this product.
    """
    env = dict(os.environ if parent is None else parent)
    env["NEEDLE_TELEMETRY"] = "0"
    env["DO_NOT_TRACK"] = "1"
    env["CI"] = "1"
    return env


def seed_cache_from(binary_source: Path, model_source: Path) -> tuple[Path, Path]:
    """Copy already-verified files into the cache. Returns the cache paths.

    For a coordinator or operator who already holds the verified pinned files
    (for example from a prior offline audit): each source is verified against
    its pin *before* it is copied, so seeding cannot install an unverified file.
    """
    folder = current_platform()
    binary_path, model_path = _pinned_paths(folder)
    binary = PLATFORM_BINARIES[folder].asset

    verify_pinned(Path(binary_source), binary)
    verify_pinned(Path(model_source), WHISTLE_MODEL)

    binary_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary_source, binary_path)
    shutil.copyfile(model_source, model_path)
    return binary_path, model_path
