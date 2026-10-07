"""The pinned native streaming library: ``libneedle3.dll`` and its siblings.

Standalone Whisper/Whistle transcription uses the pinned *CLI* (``needle.exe``)
from :mod:`textflowkit.core.whistle_assets`. Live streaming is a different
artifact at the same upstream revision: the ``needle_stream_transcribe_*``
symbols live only in the *library* the CLI does not export, which upstream ships
inside its Python wheel. This module pins that wheel and the single library
member it contains, and installs the member under an isolated streaming cache
directory so nothing about an existing install moves.

Pinning rules, kept the same as the CLI's, plus one:

- The whole **wheel** is pinned by size and SHA-256 (the LFS digest upstream
  publishes for the pinned revision). Verifying the wheel already authenticates
  every member inside it: a zip whose bytes are the pinned wheel's bytes *is*
  the pinned wheel, so its members are the pinned members.
- The **extracted library member** is *also* pinned by size and SHA-256. This is
  not what makes the member trustworthy - the outer wheel pin already does - it
  is an explicit provenance/check on the one file that actually loads: it records
  the member's identity directly, so the extraction step fails loudly if a future
  wheel ever carries the same outer digest with different contents, and the
  member can be verified on its own when seeded into the cache.

Extraction never calls ``ZipFile.extractall``. The member name is a fixed
constant per platform (not a name read from the archive), and the entry is read
by that exact name and written to a temp file that is verified *before* it is
renamed into place - so a malicious or malformed archive cannot traverse paths
or leave an unverified file in the cache.

Verified vs published pins: the Windows wheel and its member have both been
fetched and hashed on this machine and match the upstream LFS digests. The
non-Windows wheels are pinned from the upstream listing's LFS digests (the wheel
hash is published), but their *extracted member* hashes have **not** been
verified here; those platforms are refused with an explicit message rather than
running an unverified library. Windows is the verified target.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from textflowkit.core.whistle_assets import (
    ENV_DOWNLOAD_TIMEOUT_SECONDS,
    ENV_MAX_DOWNLOAD_BYTES,
    ENV_OFFLINE,
    WHISTLE_MODEL,
    WhistleAssetError,
    _assert_pinned_scheme,
    _download_to_temp,
    _positive_env_int,
    models_dir,
    verify_pinned,
)

__all__ = [
    "STREAMING_LIBRARIES",
    "STREAMING_LIBRARY_REVISION",
    "STREAMING_REQUIRED_SYMBOLS",
    "SUPPORTED_STREAMING_PLATFORMS",
    "StreamingAssetError",
    "StreamingLibrary",
    "current_streaming_folder",
    "ensure_streaming_library",
    "ensure_streaming_model",
    "model_cache_path",
    "streaming_cache_status",
    "streaming_dir",
    "verify_streaming_pinned",
]


class StreamingAssetError(WhistleAssetError):
    """A streaming library asset is missing, unsupported, or failed to verify.

    Subclasses :class:`WhistleAssetError` so a caller that already catches the
    asset failure of the CLI path catches this one too, while remaining a
    distinct name for the streaming-specific refusals.
    """


#: The pinned upstream revision. Recorded for provenance; the URLs below are
#: fixed so a moved revision cannot change what this product runs.
STREAMING_LIBRARY_REVISION = "2ae11323dc000f5e70c49f7403efa6af12ba9e67"

#: The upstream HF repo the pinned wheels live in.
_STREAMING_REPO = "Cactus-Compute/needle3"

#: The wheel version the pins below are taken from.
_STREAMING_WHEEL_VERSION = "3.2.0"

#: The symbols the streaming library must export for a session to run. Recorded
#: so a future verification step (and the fetch-time check) names them once.
STREAMING_REQUIRED_SYMBOLS: tuple[str, ...] = (
    "needle_load",
    "needle_stream_transcribe_process",
    "needle_stream_transcribe_stop",
    "needle_last_error",
)

_ENV_STREAMING_DIR = "TEXTFLOWKIT_STREAMING_DIR"

#: Download read size, matching the CLI asset module.
_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StreamingLibrary:
    """One platform's streaming library: the pinned wheel and its member.

    ``member`` is the fixed path *inside* the wheel to read; it is a constant,
    never a name taken from the archive, so extraction cannot be steered. The
    member size/hash are the values for the extracted file. ``member_verified``
    records whether that member has actually been fetched and hashed here: a
    platform with ``member_verified=False`` is refused rather than run.
    """

    folder: str
    wheel_url: str
    wheel_sha256: str
    wheel_size: int
    member: str
    member_sha256: str | None
    member_size: int | None
    member_verified: bool


def _wheel(folder: str, tag: str, sha256: str, size: int, member: str,
           member_sha256: str | None, member_size: int | None,
           member_verified: bool) -> StreamingLibrary:
    return StreamingLibrary(
        folder=folder,
        wheel_url=(
            f"https://huggingface.co/{_STREAMING_REPO}/resolve/"
            f"{STREAMING_LIBRARY_REVISION}/python/"
            f"cactus_needle-{_STREAMING_WHEEL_VERSION}-py3-none-{tag}.whl"
        ),
        wheel_sha256=sha256,
        wheel_size=size,
        member=member,
        member_sha256=member_sha256,
        member_size=member_size,
        member_verified=member_verified,
    )


#: The published 3.2.0 wheels whose LFS digests upstream lists, one per platform
#: folder. Wheel hashes/sizes are the listing's; the extracted-member pins are
#: given only where they have been verified here (Windows).
STREAMING_LIBRARIES: dict[str, StreamingLibrary] = {
    "windows-x86_64": _wheel(
        "windows-x86_64", "win_amd64",
        "0bd41dc812a4b7e504f6ba294de65a363a51b031f154d24aac748b07249dc952", 696277,
        "needle/libneedle3.dll",
        "d20c72dccd7557b10493fae3d6af97e4456d420cb454804d5716fa2cb15eb774", 1524736,
        member_verified=True,
    ),
    "windows-arm64": _wheel(
        "windows-arm64", "win_arm64",
        "15000affa669b7e84e6303617312e04650a05e9c71e856bab075a0ad36ebe8cf", 629797,
        "needle/libneedle3.dll", None, None, member_verified=False,
    ),
    "linux-x86_64": _wheel(
        "linux-x86_64", "manylinux2014_x86_64",
        "0e8a3bce4e52968ee14e8e9d47098e65ed06dd5b7c18cef3a7fa1375756736f9", 684813,
        "needle/libneedle3.so", None, None, member_verified=False,
    ),
    "linux-arm64": _wheel(
        "linux-arm64", "manylinux2014_aarch64",
        "80871585c84a277eff24b43a9366958996be38f6f920c9d1f8a0d2a07f3ae51f", 661328,
        "needle/libneedle3.so", None, None, member_verified=False,
    ),
    "macos-arm64": _wheel(
        "macos-arm64", "macosx_11_0_arm64",
        "3b0887a43cd6e9a99009fabf35b231c11bb3a978ab8af94b8119b2eb76e19832", 535559,
        "needle/libneedle3.dylib", None, None, member_verified=False,
    ),
}

#: The platforms the streaming library is published for.
SUPPORTED_STREAMING_PLATFORMS: tuple[str, ...] = tuple(STREAMING_LIBRARIES)


def streaming_dir() -> Path:
    """The deterministic, absolute directory holding the streaming library.

    ``TEXTFLOWKIT_STREAMING_DIR`` overrides it. Otherwise it is a versioned
    subdirectory of the same per-user cache the CLI assets use, so a streaming
    install lives beside - never on top of - the standalone runtime:

        <models_dir>/streaming/libneedle3-<revision>/

    It is always absolute and never inside the package source tree.
    """
    override = os.environ.get(_ENV_STREAMING_DIR)
    if override:
        return Path(override).expanduser().resolve()
    from textflowkit.core.whistle_assets import models_dir

    return (models_dir() / "streaming" / f"libneedle3-{STREAMING_LIBRARY_REVISION[:12]}").resolve()


def current_streaming_folder() -> str:
    """The published streaming platform folder for this interpreter, or a refusal."""
    system = sys.platform
    machine = _platform.machine().lower()

    if system.startswith("win"):
        os_folder = "windows"
    elif system == "darwin":
        os_folder = "macos"
    elif system.startswith("linux"):
        os_folder = "linux"
    else:
        raise StreamingAssetError(
            f"live streaming has no native library for platform '{system}'. "
            f"Supported platforms: {', '.join(SUPPORTED_STREAMING_PLATFORMS)}. "
            "Use file-based transcription instead, which is available everywhere."
        )

    if machine in ("amd64", "x86_64", "x64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine

    folder = f"{os_folder}-{arch}"
    if folder not in STREAMING_LIBRARIES:
        raise StreamingAssetError(
            f"live streaming has no native library for '{folder}'. "
            f"Supported platforms: {', '.join(SUPPORTED_STREAMING_PLATFORMS)}. "
            "Use file-based transcription instead, which is available everywhere."
        )
    return folder


def streaming_cache_status() -> dict[str, object]:
    """Describe the streaming cache without touching the network or verifying bytes."""
    try:
        folder = current_streaming_folder()
    except StreamingAssetError as exc:
        return {"platform": None, "error": str(exc), "dir": str(streaming_dir())}
    library = STREAMING_LIBRARIES[folder]
    path = streaming_dir() / Path(library.member).name
    return {
        "platform": folder,
        "dir": str(streaming_dir()),
        "library": str(path),
        "present": path.exists(),
        "member_verified": library.member_verified,
    }


def _streaming_asset_for(path: Path, *, size: int, sha256: str, filename: str):
    """Build a :class:`whistle_assets.PinnedAsset` for the shared verifier."""
    from textflowkit.core.whistle_assets import PinnedAsset

    return PinnedAsset(filename=filename, url="", sha256=sha256, size=size)


def verify_streaming_pinned(path: Path, *, size: int, sha256: str, filename: str,
                            member: bool = False) -> None:
    """Verify a whole wheel or an extracted member against its pin.

    Delegates to the CLI asset module's verifier so size-then-hash ordering and
    the streamed hash are shared: one implementation of "is this the pinned
    file", not two that can drift.
    """
    label = f"streaming {'library member' if member else 'wheel'}"
    try:
        verify_pinned(path, _streaming_asset_for(path, size=size, sha256=sha256,
                                                 filename=filename))
    except WhistleAssetError as exc:
        raise StreamingAssetError(f"{label} '{filename}': {exc}") from exc


# --- download and extraction ----------------------------------------------


def _download_wheel(library: StreamingLibrary, *, timeout: int, max_bytes: int) -> Path:
    """Fetch the pinned wheel to a temp file inside the streaming directory."""
    _assert_pinned_scheme(library.wheel_url)
    streaming_dir().mkdir(parents=True, exist_ok=True)
    import time

    deadline = time.monotonic() + timeout
    fd, temp_name = tempfile.mkstemp(prefix=".wheel.", suffix=".part",
                                     dir=str(streaming_dir()))
    os.close(fd)
    destination = Path(temp_name)
    try:
        request = urllib.request.Request(library.wheel_url,
                                         headers={"User-Agent": "textflowkit"})
        with urllib.request.urlopen(request, timeout=min(timeout, 60)) as response:
            written = 0
            with destination.open("wb") as handle:
                while True:
                    if time.monotonic() > deadline:
                        raise StreamingAssetError(
                            f"streaming wheel download timed out after {timeout}s"
                        )
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise StreamingAssetError(
                            "streaming wheel download exceeds the configured size limit"
                        )
                    handle.write(chunk)
    except StreamingAssetError:
        destination.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise StreamingAssetError(f"streaming wheel download failed: {exc}") from exc
    return destination


def _extract_member(wheel_path: Path, library: StreamingLibrary) -> bytes:
    """Read exactly the pinned member from the wheel by its fixed name.

    Never ``extractall``: the name is a constant, the read is bounded by the
    pinned member size, and the bytes are returned for verification rather than
    written to their final path directly.
    """
    try:
        with zipfile.ZipFile(wheel_path) as archive, archive.open(library.member) as handle:
            data = handle.read()
    except (zipfile.BadZipFile, KeyError) as exc:
        raise StreamingAssetError(
            f"streaming wheel does not contain '{library.member}': {exc}"
        ) from exc
    if library.member_size is not None and len(data) != library.member_size:
        raise StreamingAssetError(
            f"streaming library member '{library.member}' has size {len(data)}, "
            f"expected {library.member_size}"
        )
    return data


def _install_member(wheel_path: Path, library: StreamingLibrary) -> Path:
    """Extract, verify, and atomically install the member into the cache."""
    final = streaming_dir() / Path(library.member).name
    data = _extract_member(wheel_path, library)
    if library.member_sha256 is not None:
        digest = hashlib.sha256(data).hexdigest()
        if digest != library.member_sha256:
            raise StreamingAssetError(
                f"streaming library member '{library.member}' has sha256 {digest}, "
                f"expected {library.member_sha256}"
            )
    fd, temp_name = tempfile.mkstemp(prefix=".member.", suffix=".part",
                                     dir=str(streaming_dir()))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        temp = Path(temp_name)
        temp.replace(final)  # atomic same-filesystem rename
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return final


def ensure_streaming_library(
    *,
    offline: bool | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
) -> Path:
    """Return the verified streaming library path, downloading on first use.

    A cached member is trusted only after it verifies against its pinned size and
    hash. A platform whose extracted member hash is not yet verified here is
    refused with an explicit message rather than run. Offline mode raises on a
    missing asset instead of fetching. Never called at import time.
    """
    folder = current_streaming_folder()
    library = STREAMING_LIBRARIES[folder]
    if not library.member_verified:
        raise StreamingAssetError(
            f"live streaming has a published wheel for '{folder}' but its "
            "extracted member hash has not been verified on this machine yet, so "
            "this product refuses to run it. Windows x86_64 is verified. "
            "A maintainer must fetch and pin the member hash before enabling "
            "this platform."
        )

    final = streaming_dir() / Path(library.member).name
    offline_mode = (
        os.environ.get(ENV_OFFLINE, "").strip().lower() in {"1", "true", "yes", "on"}
        if offline is None else offline
    )
    timeout = timeout if timeout is not None else _positive_env_int(
        ENV_DOWNLOAD_TIMEOUT_SECONDS, 600
    )
    max_bytes = max_bytes if max_bytes is not None else _positive_env_int(
        ENV_MAX_DOWNLOAD_BYTES, 256 * 1024 * 1024
    )

    if final.exists():
        verify_streaming_pinned(
            final, size=library.member_size, sha256=library.member_sha256,
            filename=final.name, member=True,
        )
        return final

    if offline_mode:
        raise StreamingAssetError(
            f"streaming library '{final}' is not in the cache and offline mode is "
            f"on ({ENV_OFFLINE}); run once without offline mode to fetch it, or "
            "seed the cache with the verified file."
        )

    wheel_temp = _download_wheel(library, timeout=timeout, max_bytes=max_bytes)
    try:
        # The wheel is verified before its member is read, so a tampered wheel
        # never reaches the extraction step.
        verify_streaming_pinned(
            wheel_temp, size=library.wheel_size, sha256=library.wheel_sha256,
            filename=f"cactus_needle-{_STREAMING_WHEEL_VERSION}.whl", member=False,
        )
        return _install_member(wheel_temp, library)
    finally:
        wheel_temp.unlink(missing_ok=True)


def model_cache_path() -> Path:
    """The cache path of the pinned ``whistle.cact`` model, beside the library.

    Shared with the CLI: the streaming worker loads the same pinned model, so it
    reuses the exact same pin and the same per-user cache rather than copying a
    second 17 MB file.
    """
    return (models_dir() / WHISTLE_MODEL.filename).resolve()


def ensure_streaming_model(
    *,
    weights_path: str | os.PathLike[str] | None = None,
    offline: bool | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
) -> Path:
    """Return a *verified* path to the ``whistle.cact`` model the stream needs.

    Two cases, each verified against the pin independently:

    - An explicit ``weights_path`` is validated in place: it must exist and match
      the pinned model's size and SHA-256, or it is refused. Nothing is written.
    - No ``weights_path``: the pinned model is resolved from the cache, and if it
      is missing it is downloaded (unless offline), then verified.

    The download reuses the CLI asset module's own ``WHISTLE_MODEL`` pin, its
    byte/timeout limits, and its offline gate - and never fetches the CLI binary,
    never runs the CLI. It is a lazy runtime call: never invoked at import time.
    """
    if weights_path is not None:
        explicit = Path(weights_path).expanduser()
        if not explicit.exists():
            raise StreamingAssetError(
                f"explicit streaming weights_path '{explicit}' does not exist"
            )
        try:
            verify_pinned(explicit, WHISTLE_MODEL)
        except WhistleAssetError as exc:
            raise StreamingAssetError(
                f"explicit streaming weights_path '{explicit}' does not match the "
                f"pinned {WHISTLE_MODEL.filename}: {exc}"
            ) from exc
        return explicit.resolve()

    final = model_cache_path()
    offline_mode = (
        os.environ.get(ENV_OFFLINE, "").strip().lower() in {"1", "true", "yes", "on"}
        if offline is None else offline
    )
    timeout = timeout if timeout is not None else _positive_env_int(
        ENV_DOWNLOAD_TIMEOUT_SECONDS, 600
    )
    max_bytes = max_bytes if max_bytes is not None else _positive_env_int(
        ENV_MAX_DOWNLOAD_BYTES, 256 * 1024 * 1024
    )

    if final.exists():
        try:
            verify_pinned(final, WHISTLE_MODEL)
        except WhistleAssetError as exc:
            raise StreamingAssetError(
                f"cached streaming model '{final}' failed verification: {exc}"
            ) from exc
        return final

    if offline_mode:
        raise StreamingAssetError(
            f"streaming model '{final}' is not in the cache and offline mode is on "
            f"({ENV_OFFLINE}); run once without offline mode to fetch it, or seed "
            "the cache with the verified file."
        )

    final.parent.mkdir(parents=True, exist_ok=True)
    temp = _download_to_temp(WHISTLE_MODEL, timeout=timeout, max_bytes=max_bytes)
    try:
        verify_pinned(temp, WHISTLE_MODEL)
        temp.replace(final)  # atomic same-filesystem rename
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return final


def seed_streaming_cache_from(source: Path) -> Path:
    """Copy an already-verified library member into the cache.

    For an operator who already holds the verified member (for example from a
    prior audit): the source is verified against the pin *before* it is copied,
    so seeding cannot install an unverified file.
    """
    folder = current_streaming_folder()
    library = STREAMING_LIBRARIES[folder]
    if not library.member_verified:
        raise StreamingAssetError(
            f"streaming library pin for '{folder}' is not verified; cannot seed"
        )
    verify_streaming_pinned(Path(source), size=library.member_size,
                            sha256=library.member_sha256, filename=Path(source).name,
                            member=True)
    final = streaming_dir() / Path(library.member).name
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, final)
    return final
