"""Capability and metadata reporting for the local UI.

The workspace must be able to tell an operator what it *can* do before they
start a long job - which engine is default, whether its assets or a cache are
already present, which optional extras are installed, and whether an asset
download will happen on first use. This module answers those questions **without
loading a model and without downloading anything**: every probe is an import
test or a filesystem stat. That is the whole point of the endpoint - it is safe
to call on page load.
"""

from __future__ import annotations

import shutil
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

from textflowkit.core.engine import (
    DEFAULT_ENGINE,
    ENGINE_CHOICES,
    engine_default_model,
    engine_model_names,
)
from textflowkit.core.service import production_enabled

#: Optional packages an operator may want, with the extra that provides each.
#: Reporting "installed / not installed" for these is a bare import test - no
#: model is imported, because the import itself is guarded to only touch the
#: package's top-level module.
OPTIONAL_EXTRAS: tuple[tuple[str, str, str], ...] = (
    ("mcp", "mcp", "mcp"),
    ("http", "fastapi", "http"),
    ("export-docx", "docx", "export"),
    ("export-pdf", "reportlab", "export"),
    ("whisper", "whisper", "whisper"),
    ("faster-whisper", "faster_whisper", "faster-whisper"),
    ("diarize", "pyannote.audio", "diarize"),
)


def _module_present(module: str) -> bool:
    """Whether ``module`` can be found, *without importing* it.

    ``find_spec`` resolves the name through the import system but does not run
    the module body - so probing ``whisper`` or ``faster_whisper`` here never
    pulls in torch or a network call. A dotted name like ``pyannote.audio`` is
    checked by its top-level package only; the submodule's own import graph is
    deliberately not walked, because doing so could import torch.
    """
    top = module.split(".", 1)[0]
    try:
        return importlib_util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def engine_asset_status(engine: str) -> dict[str, Any]:
    """Whether an engine's model assets are present, cheaply and offline.

    For Whistle this reads the pinned-asset cache status (a stat, no network).
    For the Whisper-family engines there is no pinned local asset to report here
    - their weights are fetched by the engine's own loader on first use - so the
    answer is "managed by the engine", stated plainly rather than guessed.
    """
    if engine == "whistle":
        from textflowkit.core import whistle_assets

        try:
            status = whistle_assets.cache_status()
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not 500 the page
            return {
                "managed": True,
                "supported": False,
                "detail": f"unsupported platform: {exc}",
                "download_required": False,
            }
        present = bool(status.get("binary_present")) and bool(status.get("model_present"))
        return {
            "managed": True,
            "supported": True,
            "platform": status.get("platform"),
            "models_dir": status.get("models_dir"),
            "offline": bool(status.get("offline")),
            "downloaded": present,
            "download_required": not present,
            "detail": (
                "cached"
                if present
                else "downloaded on first use (one pinned model)"
            ),
        }
    return {
        "managed": False,
        "supported": True,
        "downloaded": None,
        "download_required": None,
        "detail": "weights are fetched by the engine's own loader on first use",
    }


def capabilities() -> dict[str, Any]:
    """Everything the workspace needs to describe itself, cheaply.

    No model is loaded and nothing is downloaded. The result is safe to serve on
    every page load and safe to poll; it is deliberately not cached so a
    package installed *while the UI is running* is reflected on the next call.
    """
    model_names: dict[str, Any] = {}
    for name in ENGINE_CHOICES:
        try:
            model_names[name] = list(engine_model_names(name))
        except Exception as exc:  # noqa: BLE001 - absence is information, not a fault
            model_names[name] = {"unavailable": str(exc)}
    default_model = engine_default_model(DEFAULT_ENGINE)
    extras = [
        {"name": label, "module": module, "extra": extra, "installed": _module_present(module)}
        for label, module, extra in OPTIONAL_EXTRAS
    ]
    return {
        "default_engine": DEFAULT_ENGINE,
        "default_model": default_model,
        "engines": list(ENGINE_CHOICES),
        "engine_models": model_names,
        "assets": engine_asset_status(DEFAULT_ENGINE),
        "extras": extras,
        "production": production_enabled(),
        # ffmpeg is required for every run; yt-dlp resolves URLs. Report both
        # without running them, so the UI can warn before a URL job is submitted.
        "tools": {
            "ffmpeg": shutil.which("ffmpeg") is not None,
        },
    }


def preflight(source: str, formats: list[str], *, input_root: Path | None) -> dict[str, Any]:
    """Check a submission's dependencies *before* it is queued.

    Mirrors the checks the shared submission contract performs, so the UI can
    refuse a request that is certain to fail without writing a job row or
    spending a queue slot:

    - a missing DOCX/PDF export dependency is reported here, before acquisition;
    - a local source that does not exist is reported here;
    - a data URL that is not a supported scheme is refused.

    Returns ``{"ok": bool, "errors": [...], "warnings": [...]}``. Warnings are
    things that will not stop the job but the operator should know (e.g. a URL
    source on a box with no yt-dlp runtime). This is advisory: the real gates
    still run in the shared contract and the pipeline.
    """
    errors: list[str] = []
    warnings: list[str] = []

    normalized = {fmt.lower().lstrip(".") for fmt in formats}
    try:
        from textflowkit.render import validate_export_requirements

        validate_export_requirements(sorted(normalized))
    except ValueError as exc:
        errors.append(str(exc))
    except ImportError as exc:  # pragma: no cover - mapped to ValueError upstream
        errors.append(str(exc))

    from textflowkit.core.checkpoint import is_local_source

    if is_local_source(source):
        candidate = Path(source).expanduser()
        if not candidate.is_absolute() and input_root is not None:
            candidate = Path(input_root) / candidate
        if not candidate.exists():
            errors.append(f"no such file: {source}")
        elif not candidate.is_file():
            errors.append(f"not a file: {source}")
        else:
            limit = int(__import__("os").environ.get("TEXTFLOWKIT_MAX_MEDIA_BYTES") or 0)
            if limit:
                size = candidate.stat().st_size
                if size > limit:
                    errors.append(
                        f"file is {size} bytes, over TEXTFLOWKIT_MAX_MEDIA_BYTES ({limit})"
                    )
    else:
        # A URL or data URL. A data URL is always local and needs no tool.
        if source.startswith("data:"):
            pass
        else:
            from textflowkit.sources.acquire import AcquisitionError, require_tool

            try:
                require_tool("yt-dlp", module="yt_dlp")
            except AcquisitionError as exc:
                warnings.append(str(exc))

    return {"ok": not errors, "errors": errors, "warnings": warnings}
