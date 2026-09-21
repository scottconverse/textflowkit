"""Acquisition helpers: external-tool resolution and media handling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from textflowkit.sources.acquire import AcquisitionError, require_tool
from textflowkit.sources.detect import SourceRef, resolve_source


def test_require_tool_finds_ffmpeg():
    # ffmpeg is a documented prerequisite and present in the dev environment.
    assert require_tool("ffmpeg")


def test_require_tool_finds_tool_beside_interpreter():
    """Tools installed into the active environment but absent from PATH must resolve.

    This is a regression test: yt-dlp installs an entry point into the venv's
    Scripts/bin directory, which is NOT on PATH when the venv is not activated.
    """
    exe = Path(sys.executable).parent / ("python" + (".exe" if sys.platform == "win32" else ""))
    assert exe.exists()
    assert require_tool("python", module="json") is not None


def test_require_tool_returns_none_when_module_available(monkeypatch):
    """With no binary but an importable module, return None so callers use the API."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert require_tool("yt-dlp", module="yt_dlp") is None


def test_require_tool_raises_when_truly_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(AcquisitionError):
        require_tool("definitely-not-a-real-tool-xyz")


def test_local_source_passes_through_without_network(tmp_path):
    from textflowkit.sources.acquire import fetch_media

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not-really-video")
    ref = resolve_source(str(media))
    got = fetch_media(ref, work_dir=tmp_path)
    assert got == media


def test_unsupported_source_kind_rejected(tmp_path):
    from textflowkit.sources.acquire import fetch_media

    ref = SourceRef(kind="carrier-pigeon", location="somewhere", platform="direct")
    with pytest.raises(AcquisitionError):
        fetch_media(ref, work_dir=tmp_path)
