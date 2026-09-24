"""Opt-in production profile; localhost owner mode stays unconfined."""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

ENV_PROFILE = "TEXTFLOWKIT_PROFILE"
ENV_API_TOKEN = "TEXTFLOWKIT_API_TOKEN"
ENV_WORK_ROOT = "TEXTFLOWKIT_WORK_ROOT"
ENV_MAX_REQUEST_BYTES = "TEXTFLOWKIT_MAX_REQUEST_BYTES"
ENV_RATE_PER_MINUTE = "TEXTFLOWKIT_RATE_PER_MINUTE"
ENV_MAX_DURATION_SECONDS = "TEXTFLOWKIT_MAX_DURATION_SECONDS"
ENV_MAX_OUTPUT_BYTES = "TEXTFLOWKIT_MAX_OUTPUT_BYTES"
ENV_MAX_MEDIA_BYTES = "TEXTFLOWKIT_MAX_MEDIA_BYTES"
ENV_EGRESS_PROXY = "TEXTFLOWKIT_EGRESS_PROXY"
ENV_FFMPEG_TIMEOUT_SECONDS = "TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS"
# Wall-clock ceiling for one ffmpeg decode. It is not a production-only bound:
# local CLI, MCP, and HTTP jobs all decode under it, so the timeout error names
# this default to tell an operator how to raise it.
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 600


class ServiceConfigurationError(ValueError):
    """The production profile cannot safely start with this configuration."""


def production_enabled() -> bool:
    value = os.environ.get(ENV_PROFILE, "developer").strip().lower()
    if value not in {"developer", "production"}:
        raise ServiceConfigurationError(
            f"{ENV_PROFILE} must be 'developer' or 'production', not '{value}'"
        )
    return value == "production"


def reject_browser_cookie_requests(cookies_from_browser: str | None) -> None:
    """Refuse browser-cookie requests while the production profile is active.

    The production profile serves callers who are not the Windows account owner,
    and the server cannot tell an owner-run CLI or stdio MCP session from a remote
    HTTP caller. So the fail-closed rule is applied to every submission that
    reaches the shared contract, including resume of a request saved earlier with
    the option. Developer mode on the owner's own machine is unaffected.
    """
    if not cookies_from_browser:
        return
    if production_enabled():
        raise ServiceConfigurationError(
            "cookies_from_browser is rejected in the production profile: the server must "
            "not read the account owner's browser cookies for a remote caller. Use the "
            "developer profile on the owner's machine."
        )


def positive_limit(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ServiceConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ServiceConfigurationError(f"{name} must be a positive integer")
    return value


def validate_production_config() -> None:
    """Fail closed before handling any production HTTP request."""
    if not production_enabled():
        return
    from textflowkit.core.paths import ENV_INPUT_ROOT, ENV_OUTPUT_ROOT

    required = (ENV_API_TOKEN, ENV_INPUT_ROOT, ENV_OUTPUT_ROOT, "TEXTFLOWKIT_DB", ENV_WORK_ROOT)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ServiceConfigurationError(
            f"production profile requires: {', '.join(missing)}"
        )
    if len(os.environ[ENV_API_TOKEN]) < 16:
        raise ServiceConfigurationError(f"{ENV_API_TOKEN} must have at least 16 characters")
    if os.environ["TEXTFLOWKIT_DB"] == ":memory:":
        raise ServiceConfigurationError("production requires an on-disk TEXTFLOWKIT_DB")
    from textflowkit.core.executor import get_default_executor
    from textflowkit.core.jobs import get_default_store
    from textflowkit.core.sqlite_store import SqliteJobStore

    store = get_default_store()
    db_path = Path(os.environ["TEXTFLOWKIT_DB"]).expanduser().resolve()
    if not isinstance(store, SqliteJobStore) or Path(store.path).resolve() != db_path:
        raise ServiceConfigurationError("production requires the active store to use TEXTFLOWKIT_DB")
    if get_default_executor().store is not store:
        raise ServiceConfigurationError("production executor and durable store must match")
    input_root = Path(os.environ[ENV_INPUT_ROOT]).expanduser().resolve()
    if not input_root.is_dir():
        raise ServiceConfigurationError(f"{ENV_INPUT_ROOT} must name an existing directory")
    for name in (ENV_OUTPUT_ROOT, ENV_WORK_ROOT):
        path = Path(os.environ[name]).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ServiceConfigurationError(f"{name} must name a directory")
    positive_limit(ENV_MAX_REQUEST_BYTES, 64 * 1024)
    positive_limit(ENV_RATE_PER_MINUTE, 60)
    positive_limit(ENV_MAX_DURATION_SECONDS, 4 * 3600)
    positive_limit(ENV_MAX_OUTPUT_BYTES, 50 * 1024 * 1024)
    positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024)
    positive_limit(ENV_FFMPEG_TIMEOUT_SECONDS, DEFAULT_FFMPEG_TIMEOUT_SECONDS)
    positive_limit("TEXTFLOWKIT_MAX_PENDING_JOBS", 100)
    # Optional, but never silently ignored: an operator who mistypes a proxy
    # address and believes it is trusted would be left counting every client
    # behind that proxy as one. Fail closed instead.
    from textflowkit.core.bind import TrustedProxyConfigError, trusted_proxy_networks

    try:
        trusted_proxy_networks()
    except TrustedProxyConfigError as exc:
        raise ServiceConfigurationError(str(exc)) from exc


def service_work_root() -> str | None:
    if not production_enabled():
        return None
    validate_production_config()
    return str(Path(os.environ[ENV_WORK_ROOT]).expanduser().resolve())


def _probe_duration(media: Path) -> float | None:
    """Return known duration using a bounded probe; unknown is handled at decode."""
    from textflowkit.sources.acquire import require_tool

    ffprobe = require_tool("ffprobe")
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceConfigurationError("ffprobe timed out before decode") from exc
    if proc.returncode != 0:
        return None
    try:
        duration = float(proc.stdout.strip())
    except ValueError:
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def enforce_predecode_limits(media: Path) -> None:
    """Reject known oversize/overlong media before launching full extraction."""
    if not production_enabled():
        return
    maximum = positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024)
    if media.stat().st_size > maximum:
        raise ServiceConfigurationError("media exceeds the configured size limit")
    duration = _probe_duration(media)
    if duration is not None and duration > positive_limit(ENV_MAX_DURATION_SECONDS, 4 * 3600):
        raise ServiceConfigurationError("source duration exceeds the configured limit")


def enforce_media_limits(media: Path, audio: Path) -> None:
    """Defense in depth after bounded acquisition and decode."""
    if not production_enabled():
        return
    maximum = positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024)
    if media.stat().st_size > maximum or audio.stat().st_size > maximum:
        raise ServiceConfigurationError("media exceeds the configured size limit")
    duration = _probe_duration(audio)
    if duration is None:
        raise ServiceConfigurationError("cannot verify source duration with ffprobe")
    if duration > positive_limit(ENV_MAX_DURATION_SECONDS, 4 * 3600):
        raise ServiceConfigurationError("source duration exceeds the configured limit")


def enforce_output_limit(size: int) -> None:
    if production_enabled() and size > positive_limit(ENV_MAX_OUTPUT_BYTES, 50 * 1024 * 1024):
        raise ServiceConfigurationError("rendered output exceeds the configured size limit")
