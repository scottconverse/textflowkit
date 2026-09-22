"""Media acquisition: local files pass through, URLs go through yt-dlp."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

from textflowkit.core.cancel import CancelledError
from textflowkit.core.paths import UnsafeInputPathError, opened_file_path
from textflowkit.core.service import (
    ENV_EGRESS_PROXY,
    ENV_FFMPEG_TIMEOUT_SECONDS,
    ENV_MAX_DURATION_SECONDS,
    ENV_MAX_MEDIA_BYTES,
    positive_limit,
    production_enabled,
)
from textflowkit.sources.detect import SourceRef, UnsafeUrlError, assert_url_is_fetchable
from textflowkit.sources.scratch import ScratchPaths


class AcquisitionError(RuntimeError):
    """Raised when media cannot be obtained."""


def stage_confined_local_media(
    source: str | Path, *, work_dir: Path, input_root: str | Path
) -> Path:
    """Copy a handle-verified local input into isolated scratch before ffmpeg."""
    base = Path(input_root).expanduser().resolve()
    path = Path(source)
    out = ScratchPaths(Path(work_dir)).staged_local(path.suffix)
    maximum = positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024) if production_enabled() else None
    with path.open("rb") as opened:
        actual = opened_file_path(opened.fileno(), path)
        if actual != base and base not in actual.parents:
            raise UnsafeInputPathError(
                f"opened input file '{actual}' is outside the allowed root '{base}'"
            )
        written = 0
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as destination:
            while chunk := opened.read(1024 * 1024):
                written += len(chunk)
                if maximum is not None and written > maximum:
                    raise AcquisitionError("media exceeds the configured size limit")
                destination.write(chunk)
    return out


def require_tool(name: str, *, module: str | None = None) -> str | None:
    """Locate an external tool.

    Checks PATH first, then the interpreter's own Scripts/bin directory. The
    second check matters for the common case where a dependency was installed
    into the active environment (or a venv) but the environment is not
    activated, so its entry point is not on PATH.

    Returns None when the tool is absent and a viable in-process module exists;
    callers then use the module instead of shelling out.
    """
    found = shutil.which(name)
    if found:
        return found

    # Look beside the running interpreter (venv/Scripts, venv/bin, ...).
    exe = name + (".exe" if os.name == "nt" else "")
    scripts_dir = Path(sys.executable).parent
    candidate = scripts_dir / exe
    if candidate.exists():
        return str(candidate)

    if module:
        try:
            __import__(module)
        except ImportError:
            pass
        else:
            return None  # module usable in-process

    raise AcquisitionError(
        f"required tool '{name}' not found. Install it (pip install {module or name}) and retry."
    )


# JavaScript runtimes yt-dlp can use, in its own priority order. yt-dlp enables
# only "deno" by default, so a machine that has Node (or bun/quickjs) still emits
# "No supported JavaScript runtime could be found". Detecting what is actually
# present and enabling it explicitly avoids requiring a specific runtime.
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")


def detect_js_runtime() -> str | None:
    """Return the highest-priority JavaScript runtime available, or None.

    Mirrors yt-dlp's own priority order. Returning None is safe: yt-dlp falls
    back to non-JS extraction, which works for many videos but can leave some
    formats unavailable.
    """
    for name in JS_RUNTIMES:
        if shutil.which(name):
            return name
    return None


def _js_runtime_args() -> list[str]:
    """Build yt-dlp JS-runtime flags for whatever runtime is installed."""
    runtime = detect_js_runtime()
    if not runtime:
        return []
    # yt-dlp enables deno by default; enabling another runtime requires clearing
    # the defaults first so the detected runtime is the one actually used.
    if runtime == "deno":
        return []
    return ["--no-js-runtimes", "--js-runtimes", runtime]

def _fetch_with_module(
    url: str,
    *,
    work_dir: Path,
    cookies_from_browser: str | None,
    check_cancel: Callable[[], None] | None = None,
) -> Path:
    """Download using the yt_dlp Python API so URL checks cover its requests."""
    from yt_dlp import YoutubeDL

    layout = ScratchPaths(work_dir)
    layout.media_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = layout.download_template
    hooks: list[Path] = []
    maximum = positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024) if production_enabled() else None

    def _check_download_size(status: dict) -> None:
        if maximum is None:
            return
        for key in ("downloaded_bytes", "total_bytes", "total_bytes_estimate"):
            value = status.get(key)
            if isinstance(value, (int, float)) and value > maximum:
                raise AcquisitionError("download exceeds the configured size limit")
        # Aggregate fragments and temporary files as well as the final output;
        # a per-fragment counter alone would reset below the cap each time.
        total = sum(p.stat().st_size for p in layout.media_dir.rglob("*") if p.is_file())
        if total > maximum:
            raise AcquisitionError("download exceeds the configured size limit")

    def _hook(status: dict) -> None:
        # yt-dlp calls this frequently during a download. Raising here aborts
        # the download, which is what makes cancellation responsive for the
        # slowest common case instead of waiting for the whole fetch to finish.
        if check_cancel is not None:
            check_cancel()
        _check_download_size(status)
        if status.get("status") == "finished":
            path = status.get("filename") or status.get("_filename")
            if path:
                hooks.append(Path(path))

    runtime = detect_js_runtime()
    opts: dict = {
        "js_runtimes": {runtime: {}} if runtime else {},
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "restrictfilenames": True,
        "progress_hooks": [_hook],
    }
    if maximum is not None:
        opts["max_filesize"] = maximum
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if production_enabled():
        proxy = os.environ.get(ENV_EGRESS_PROXY)
        if not proxy:
            raise AcquisitionError(
                f"production URL acquisition requires an SSRF-filtering {ENV_EGRESS_PROXY}"
            )
        opts["proxy"] = proxy
        # External JS runtimes are separate processes and are not guaranteed to
        # honor yt-dlp's proxy option. Do not let them create an egress bypass.
        opts["js_runtimes"] = {}

    try:
        with YoutubeDL(opts) as ydl:
            original_open = ydl.urlopen

            def checked_open(request):
                requested = request if isinstance(request, str) else request.url
                _validate_fetch_url(requested)
                response = original_open(request)
                if maximum is not None:
                    headers = getattr(response, "headers", None)
                    length = headers.get("Content-Length") if headers is not None else None
                    if length is not None:
                        try:
                            too_large = int(length) > maximum
                        except (TypeError, ValueError):
                            too_large = False
                        if too_large:
                            close = getattr(response, "close", None)
                            if callable(close):
                                close()
                            raise AcquisitionError("download Content-Length exceeds the configured size limit")
                final = getattr(response, "url", None)
                if final:
                    _validate_fetch_url(final)
                return response

            ydl.urlopen = checked_open
            _validate_fetch_url(url)
            info = ydl.extract_info(url, download=False)
            _validate_download_info(info, maximum=maximum)
            ydl.process_info(info)
    except CancelledError:
        raise  # an orderly stop, not a fetch failure
    except AcquisitionError:
        raise
    except Exception as exc:
        raise AcquisitionError(f"yt-dlp failed: {exc}") from exc

    if hooks and hooks[-1].exists():
        if maximum is not None and hooks[-1].stat().st_size > maximum:
            raise AcquisitionError("download exceeds the configured size limit")
        return hooks[-1]

    requested = info.get("requested_downloads") or []
    for item in requested:
        candidate = Path(item.get("filepath", ""))
        if candidate.exists():
            if maximum is not None and candidate.stat().st_size > maximum:
                raise AcquisitionError("download exceeds the configured size limit")
            return candidate

    vid = info.get("id")
    if vid:
        for candidate in sorted(work_dir.glob(f"{vid}.*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if candidate.is_file():
                return candidate

    files = [f for f in sorted(work_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if f.is_file()]
    if files:
        return files[0]
    raise AcquisitionError("yt-dlp reported success but no output file was found")


def _validate_fetch_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise AcquisitionError(f"refusing non-HTTP download destination: {url[:100]}")
    try:
        assert_url_is_fetchable(url)
    except UnsafeUrlError as exc:
        raise AcquisitionError(f"unsafe download destination: {exc}") from exc


def _validate_download_info(info: dict | None, *, maximum: int | None = None) -> None:
    """Recheck final media/fragment URLs selected after yt-dlp extraction."""
    if not isinstance(info, dict) or info.get("_type", "video") != "video":
        raise AcquisitionError("yt-dlp did not resolve a single video")
    selected = [info]
    selected.extend(item for item in info.get("requested_formats") or [] if isinstance(item, dict))
    for item in selected:
        if maximum is not None:
            size = item.get("filesize") or item.get("filesize_approx")
            if isinstance(size, (int, float)) and size > maximum:
                raise AcquisitionError("reported download size exceeds the configured limit")
        for key in ("url", "manifest_url", "fragment_base_url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                _validate_fetch_url(value)
        for fragment in item.get("fragments") or []:
            value = fragment.get("url") if isinstance(fragment, dict) else None
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                _validate_fetch_url(value)


def fetch_media(
    source: SourceRef,
    *,
    work_dir: str | Path,
    cookies_from_browser: str | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> Path:
    """Return a local path to the media.

    Local files are returned unchanged. URLs use yt-dlp's in-process API so
    requested and selected media URLs can be checked before download.
    """
    if source.kind == "file":
        return Path(source.location)

    if source.kind != "url":
        raise AcquisitionError(f"unsupported source kind: {source.kind}")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    return _fetch_with_module(
        source.location,
        work_dir=work,
        cookies_from_browser=cookies_from_browser,
        check_cancel=check_cancel,
    )

def extract_audio(
    media_path: str | Path,
    *,
    work_dir: str | Path,
    sample_rate: int = 16000,
    check_cancel: Callable[[], None] | None = None,
) -> Path:
    """Decode through a bounded pipe, never an unbounded ffmpeg output file."""
    ffmpeg = require_tool("ffmpeg")
    media = Path(media_path)
    if not media.exists():
        raise AcquisitionError(f"media not found: {media}")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    out = ScratchPaths(work).decoded_audio
    out.parent.mkdir(parents=True, exist_ok=True)
    if media.resolve() == out.resolve():
        raise AcquisitionError("decoded audio must not overwrite source media")

    production = production_enabled()
    timeout = positive_limit(ENV_FFMPEG_TIMEOUT_SECONDS, 600)
    max_media = positive_limit(ENV_MAX_MEDIA_BYTES, 1024 * 1024 * 1024) if production else None
    max_duration = positive_limit(ENV_MAX_DURATION_SECONDS, 4 * 3600) if production else None
    max_pcm = max_media - 44 if max_media is not None else None
    duration_pcm = max_duration * sample_rate * 2 if max_duration is not None else None
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
    ]
    if max_duration is not None:
        # The extra second lets the pipe reader distinguish an overlong input
        # from one that ends exactly at the allowed duration. It never lands on
        # disk beyond the byte cap below.
        cmd.extend(["-t", str(max_duration + 1)])
    cmd.extend(["-f", "s16le", "pipe:1"])
    if check_cancel is not None:
        check_cancel()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise AcquisitionError(f"ffmpeg could not start: {exc}") from exc

    failures: list[str] = []
    stderr_tail = bytearray()

    def _read_stdout() -> None:
        written = 0
        try:
            assert proc.stdout is not None
            with wave.open(str(out), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                while chunk := proc.stdout.read(64 * 1024):
                    if duration_pcm is not None and written + len(chunk) > duration_pcm:
                        failures.append("source duration exceeds the configured limit")
                        return
                    if max_pcm is not None and written + len(chunk) > max_pcm:
                        failures.append("decoded output exceeds the configured size limit")
                        return
                    wav.writeframesraw(chunk)
                    written += len(chunk)
        except (OSError, wave.Error) as exc:
            failures.append(f"ffmpeg output failed: {exc}")

    def _read_stderr() -> None:
        assert proc.stderr is not None
        while chunk := proc.stderr.read(4096):
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 8192:
                del stderr_tail[:-8192]

    output_thread = threading.Thread(target=_read_stdout, daemon=True)
    error_thread = threading.Thread(target=_read_stderr, daemon=True)
    output_thread.start()
    error_thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None or output_thread.is_alive():
            if check_cancel is not None:
                check_cancel()
            if failures:
                raise AcquisitionError(failures[0])
            if time.monotonic() >= deadline:
                raise AcquisitionError("ffmpeg timed out during decode")
            time.sleep(0.05)
        output_thread.join(timeout=5)
        error_thread.join(timeout=5)
        if failures:
            raise AcquisitionError(failures[0])
        if proc.returncode != 0 or not out.exists():
            detail = stderr_tail.decode("utf-8", errors="replace").strip()
            raise AcquisitionError(f"ffmpeg failed: {detail[-1000:] or 'unknown error'}")
        if max_media is not None and out.stat().st_size > max_media:
            raise AcquisitionError("decoded output exceeds the configured size limit")
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        output_thread.join(timeout=5)
        error_thread.join(timeout=5)
        out.unlink(missing_ok=True)
        raise
    return out

