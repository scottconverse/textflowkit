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

# --- JavaScript runtime auto-detection -------------------------------------

def test_detect_js_runtime_prefers_deno(monkeypatch):
    from textflowkit.sources.acquire import detect_js_runtime

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert detect_js_runtime() == "deno"


def test_detect_js_runtime_falls_back_to_node(monkeypatch):
    """A machine with only Node must still resolve - this was the original bug."""
    from textflowkit.sources.acquire import detect_js_runtime

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert detect_js_runtime() == "node"


def test_detect_js_runtime_returns_none_when_absent(monkeypatch):
    from textflowkit.sources.acquire import detect_js_runtime

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert detect_js_runtime() is None


def test_js_runtime_args_empty_for_deno(monkeypatch):
    """deno is yt-dlp's default, so no flags are needed."""
    from textflowkit.sources.acquire import _js_runtime_args

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/deno" if name == "deno" else None)
    assert _js_runtime_args() == []


def test_js_runtime_args_clear_defaults_for_other_runtimes(monkeypatch):
    """A non-default runtime must clear yt-dlp's defaults to actually take effect."""
    from textflowkit.sources.acquire import _js_runtime_args

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
    args = _js_runtime_args()
    assert args == ["--no-js-runtimes", "--js-runtimes", "node"]


def test_js_runtime_args_empty_when_none_available(monkeypatch):
    from textflowkit.sources.acquire import _js_runtime_args

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert _js_runtime_args() == []


def test_module_path_passes_detected_runtime(monkeypatch, tmp_path):
    """The Python-API path must also receive the detected runtime."""
    from textflowkit.sources import acquire

    monkeypatch.setattr(acquire, "detect_js_runtime", lambda: "node")
    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            out = tmp_path / "vid.webm"
            out.write_bytes(b"x")
            return {"id": "vid", "requested_downloads": [{"filepath": str(out)}]}

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    acquire._fetch_with_module("https://example.com/v", work_dir=tmp_path, cookies_from_browser=None)
    assert captured.get("js_runtimes") == {"node": {}}
