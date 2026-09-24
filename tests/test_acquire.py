"""Acquisition helpers: external-tool resolution and media handling."""

from __future__ import annotations

import http.server
import sys
import threading
import types
from pathlib import Path

import pytest

from textflowkit.sources.acquire import AcquisitionError, require_tool
from textflowkit.sources.detect import SourceRef, resolve_source


def _director_double():
    """A request-director double for tests that replace yt_dlp wholesale.

    A real YoutubeDL builds one lazily and the redirect guard refuses to fetch
    through a build whose HTTP handlers cannot be verified, so a double standing
    in for yt_dlp has to expose a director. These doubles never reach a real
    transport, so it has no HTTP handler to guard.
    """
    return types.SimpleNamespace(handlers={})


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
        _request_director = _director_double()

        def __init__(self, opts):
            captured.update(opts)
            self.urlopen = lambda req: None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "vid", "url": "https://example.com/media"}

        def process_info(self, info):
            out = tmp_path / "vid.webm"
            out.write_bytes(b"x")
            info["requested_downloads"] = [{"filepath": str(out)}]

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    acquire._fetch_with_module("https://example.com/v", work_dir=tmp_path, cookies_from_browser=None)
    assert captured.get("js_runtimes") == {"node": {}}


def test_selected_private_media_url_is_rejected_before_download():
    from textflowkit.sources.acquire import _validate_download_info

    with pytest.raises(AcquisitionError, match="unsafe download destination"):
        _validate_download_info({
            "id": "v", "url": "https://example.com/video",
            "requested_formats": [{"url": "http://127.0.0.1/private.mp4"}],
        })


def test_private_fragment_url_is_rejected_before_download():
    from textflowkit.sources.acquire import _validate_download_info

    with pytest.raises(AcquisitionError, match="unsafe download destination"):
        _validate_download_info({
            "id": "v", "url": "https://example.com/video",
            "fragments": [{"url": "http://169.254.169.254/secret"}],
        })


def test_redirect_to_private_host_is_rejected(monkeypatch, tmp_path):
    import types

    from textflowkit.sources import acquire

    class FakeYDL:
        _request_director = _director_double()

        def __init__(self, opts):
            self.urlopen = lambda req: types.SimpleNamespace(url="http://127.0.0.1/private")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            self.urlopen(url)
            raise AssertionError("the redirect should have been refused")

        def process_info(self, info):
            raise AssertionError("download must not start")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    with pytest.raises(AcquisitionError, match="unsafe download destination"):
        acquire._fetch_with_module(
            "https://example.com/video", work_dir=tmp_path, cookies_from_browser=None,
        )


# --- redirect contact ordering ---------------------------------------------
#
# yt-dlp follows HTTP redirects inside its request handlers, so a response
# object only exists once the redirect target has already been contacted. These
# tests drive the real yt-dlp transport against a loopback site: /private stands
# in for a destination the fetch policy forbids, and the assertion is on what
# the server actually saw rather than on the error message.

@pytest.fixture
def redirect_site():
    """A loopback site whose /start redirects to the disallowed /private."""
    site: dict[str, str] = {}
    contacts: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            contacts.append(self.path)
            location = {
                "/start": f"{site['origin']}/private",
                "/ok": f"{site['origin']}/ok2",
            }.get(self.path)
            if location is not None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"not-really-media"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site["origin"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(origin=site["origin"], contacts=contacts)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def reachable_loopback(monkeypatch, redirect_site):
    """Permit the fixture's own site except /private, keeping the real policy.

    /private is left to textflowkit's real guard, so the refusal under test comes
    from the SSRF policy rather than from a stand-in.
    """
    from textflowkit.sources import acquire

    real_validate = acquire._validate_fetch_url
    refused = f"{redirect_site.origin}/private"

    def validate(url):
        if url != refused and url.startswith(redirect_site.origin):
            return
        real_validate(url)

    monkeypatch.setattr(acquire, "_validate_fetch_url", validate)
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    return redirect_site


def test_redirect_target_is_refused_before_it_is_contacted(reachable_loopback, tmp_path):
    """A redirect to a disallowed URL must not reach that URL.

    Asserting on the error alone is not enough: the pre-fix code produced the
    same error after the redirect target had already been contacted.
    """
    from textflowkit.sources import acquire

    site = reachable_loopback
    with pytest.raises(acquire.AcquisitionError, match="unsafe download destination"):
        acquire._fetch_with_module(
            f"{site.origin}/start", work_dir=tmp_path, cookies_from_browser=None,
        )
    assert "/private" not in site.contacts


def test_ordinary_redirect_is_still_followed(reachable_loopback, tmp_path):
    """The guard must not become a blanket ban on redirects."""
    from textflowkit.sources import acquire

    site = reachable_loopback
    try:
        acquire._fetch_with_module(
            f"{site.origin}/ok", work_dir=tmp_path, cookies_from_browser=None,
        )
    except acquire.AcquisitionError as exc:
        assert "unsafe download destination" not in str(exc), exc
    assert "/ok2" in site.contacts


def test_unverifiable_http_handler_is_refused():
    """A handler this build cannot wrap must not be left running unguarded."""
    from textflowkit.sources import acquire

    class Foreign:
        _SUPPORTED_URL_SCHEMES = ("http", "https")
        _create_instance = staticmethod(lambda **kwargs: object())

    director = types.SimpleNamespace(handlers={"SomeFutureRH": Foreign()})
    with pytest.raises(acquire.AcquisitionError, match="cannot verify"):
        acquire._guard_http_transports(director, lambda url: None)


def test_non_http_handler_is_ignored():
    """Handlers that never carry HTTP are left alone."""
    from textflowkit.sources import acquire

    class Websockets:
        _SUPPORTED_URL_SCHEMES = ("ws", "wss")

    director = types.SimpleNamespace(handlers={"Websockets": Websockets()})
    acquire._guard_http_transports(director, lambda url: None)


def test_guard_wraps_lazily_built_transport():
    """Handlers build their transport on first use, so the factory must be wrapped."""
    from textflowkit.sources import acquire

    seen: list[str] = []
    built: list[object] = []

    class Opener:
        def open(self, fullurl, *args, **kwargs):
            seen.append(fullurl)
            return "response"

    class Handler:
        _SUPPORTED_URL_SCHEMES = ("http", "https")

        def _create_instance(self, **kwargs):
            instance = Opener()
            built.append(instance)
            return instance

    director = types.SimpleNamespace(handlers={"Urllib": Handler()})
    acquire._guard_http_transports(director, lambda url: seen.append(f"validate:{url}"))

    transport = director.handlers["Urllib"]._create_instance()
    assert transport is built[-1]
    transport.open("https://example.com/a")
    assert seen == ["validate:https://example.com/a", "https://example.com/a"]


def test_production_url_fetch_needs_ssrf_filtering_proxy(monkeypatch, tmp_path):
    from textflowkit.sources import acquire

    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.delenv("TEXTFLOWKIT_EGRESS_PROXY", raising=False)
    with pytest.raises(AcquisitionError, match="TEXTFLOWKIT_EGRESS_PROXY"):
        acquire._fetch_with_module(
            "https://example.com/video", work_dir=tmp_path, cookies_from_browser=None,
        )


@pytest.mark.parametrize("size_source", ["content_length", "reported"])
def test_production_rejects_known_download_size_before_transfer(
    monkeypatch, tmp_path, size_source,
):
    import sys
    import types

    from textflowkit.sources import acquire

    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_EGRESS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_MEDIA_BYTES", "100")
    monkeypatch.setattr(acquire, "_validate_fetch_url", lambda url: None)
    transferred = []

    class Response:
        url = "https://example.com/media"

        def __init__(self):
            self.headers = {"Content-Length": "200"}

        def close(self):
            pass

    class FakeYDL:
        _request_director = _director_double()

        def __init__(self, opts):
            self.urlopen = lambda req: Response()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if size_source == "content_length":
                self.urlopen(url)
            return {"id": "v", "url": "https://example.com/media",
                    "filesize": 200 if size_source == "reported" else None}

        def process_info(self, info):
            transferred.append(True)

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    with pytest.raises(acquire.AcquisitionError, match="size limit|configured limit"):
        acquire._fetch_with_module("https://example.com/media", work_dir=tmp_path,
                                   cookies_from_browser=None)
    assert not transferred


# --- cancellation during download -----------------------------------------

def test_module_fetch_invokes_check_cancel_from_progress_hook(monkeypatch, tmp_path):
    """The progress hook must call check_cancel, which is what lets a long
    download be interrupted instead of running to completion."""
    from textflowkit.sources import acquire

    calls: list[int] = []

    def check_cancel():
        calls.append(1)

    class FakeYDL:
        _request_director = _director_double()

        def __init__(self, opts):
            self.opts = opts
            self.urlopen = lambda req: None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "v", "url": "https://example.com/media"}

        def process_info(self, info):
            # simulate yt-dlp reporting progress
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "filename": None})
            out = tmp_path / "v.webm"
            out.write_bytes(b"x")
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "finished", "filename": str(out)})
            info["requested_downloads"] = [{"filepath": str(out)}]

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    acquire._fetch_with_module(
        "https://example.com/v",
        work_dir=tmp_path,
        cookies_from_browser=None,
        check_cancel=check_cancel,
    )
    assert calls, "check_cancel was never invoked during download"


def test_download_cancel_aborts_the_fetch(monkeypatch, tmp_path):
    """A raising check_cancel must propagate out of the download."""
    from textflowkit.core.executor import JobCancelled
    from textflowkit.sources import acquire

    def check_cancel():
        raise JobCancelled()

    class FakeYDL:
        _request_director = _director_double()

        def __init__(self, opts):
            self.opts = opts
            self.urlopen = lambda req: None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "v", "url": "https://example.com/media"}

        def process_info(self, info):
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading"})   # raises here

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    with pytest.raises(JobCancelled):
        acquire._fetch_with_module(
            "https://example.com/v",
            work_dir=tmp_path,
            cookies_from_browser=None,
            check_cancel=check_cancel,
        )


def test_fetch_media_prefers_module_path_when_cancellable(monkeypatch, tmp_path):
    """The CLI cannot be interrupted, so a cancellable fetch must use the API."""
    from textflowkit.sources import acquire
    from textflowkit.sources.detect import SourceRef

    used = {}

    def fake_module(url, *, work_dir, cookies_from_browser, check_cancel=None):
        used["module"] = True
        return tmp_path / "v.webm"

    monkeypatch.setattr(acquire, "_fetch_with_module", fake_module)
    # pretend the CLI binary exists - we should still pick the module path
    monkeypatch.setattr(acquire.shutil, "which", lambda name: "C:/fake/yt-dlp.exe")

    acquire.fetch_media(
        SourceRef(kind="url", location="https://example.com/v", platform="direct"),
        work_dir=tmp_path,
        check_cancel=lambda: None,
    )
    assert used.get("module") is True
