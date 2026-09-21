"""Media acquisition: local files pass through, URLs go through yt-dlp."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from textflowkit.core.cancel import CancelledError
from textflowkit.sources.detect import SourceRef


class AcquisitionError(RuntimeError):
    """Raised when media cannot be obtained."""


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
    """Download using the yt_dlp Python API (used when no CLI binary is on PATH)."""
    from yt_dlp import YoutubeDL

    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    hooks: list[Path] = []

    def _hook(status: dict) -> None:
        # yt-dlp calls this frequently during a download. Raising here aborts
        # the download, which is what makes cancellation responsive for the
        # slowest common case instead of waiting for the whole fetch to finish.
        if check_cancel is not None:
            check_cancel()
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
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except CancelledError:
        raise  # an orderly stop, not a fetch failure
    except Exception as exc:
        raise AcquisitionError(f"yt-dlp failed: {exc}") from exc

    if hooks and hooks[-1].exists():
        return hooks[-1]

    requested = info.get("requested_downloads") or []
    for item in requested:
        candidate = Path(item.get("filepath", ""))
        if candidate.exists():
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


def fetch_media(
    source: SourceRef,
    *,
    work_dir: str | Path,
    cookies_from_browser: str | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> Path:
    """Return a local path to the media.

    Local files are returned unchanged. URLs are downloaded with yt-dlp, using
    the CLI when available and the Python API otherwise.
    """
    if source.kind == "file":
        return Path(source.location)

    if source.kind != "url":
        raise AcquisitionError(f"unsupported source kind: {source.kind}")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    yt_dlp = require_tool("yt-dlp", module="yt_dlp")
    # The CLI cannot be interrupted mid-download, so when the caller wants
    # cancellation we use the Python API even if a binary is available.
    if yt_dlp is None or check_cancel is not None:
        return _fetch_with_module(
            source.location,
            work_dir=work,
            cookies_from_browser=cookies_from_browser,
            check_cancel=check_cancel,
        )

    outtmpl = str(work / "%(id)s.%(ext)s")
    cmd = [
        yt_dlp,
        *_js_runtime_args(),
        "--no-playlist",
        "--no-progress",
        "--restrict-filenames",
        "-f", "bestaudio/best",
        "-o", outtmpl,
        "--print", "after_move:filepath",
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(source.location)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AcquisitionError("download timed out after 1 hour") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " | ".join(detail[-4:]) if detail else "unknown error"
        raise AcquisitionError(f"yt-dlp failed: {tail}")

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        candidate = Path(line)
        if candidate.exists():
            return candidate

    matches = sorted(work.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [m for m in matches if m.is_file()]
    if files:
        return files[0]
    raise AcquisitionError("yt-dlp reported success but no output file was found")

def extract_audio(media_path: str | Path, *, work_dir: str | Path, sample_rate: int = 16000) -> Path:
    """Extract mono PCM WAV via ffmpeg - what Whisper wants."""
    ffmpeg = require_tool("ffmpeg")
    media = Path(media_path)
    if not media.exists():
        raise AcquisitionError(f"media not found: {media}")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    out = work / (media.stem + ".wav")

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or "").strip().splitlines()
        detail = " | ".join(tail[-4:]) if tail else "unknown error"
        raise AcquisitionError(f"ffmpeg failed: {detail}")
    return out




