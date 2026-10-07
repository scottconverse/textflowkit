"""Streaming native library: pinning, extraction safety, and platform refusals.

Everything here is offline: a wheel is built in memory and served by a loopback
HTTP fixture, and the pins are monkeypatched to that wheel's real bytes. No test
reaches the network, and no test loads a native library.

What is being protected:

- The **whole wheel** is verified by size and SHA-256 *before* its member is
  read, so a tampered wheel never reaches extraction.
- The **extracted member** is pinned by size and SHA-256 too, so a wheel that
  hashes correctly but carries a swapped member is refused - the member is what
  actually loads, and the wheel hash alone does not prove it.
- Extraction reads a *fixed member name* and never calls ``extractall``, so an
  archive cannot steer the read to a traversal path.
- A platform whose member hash is unverified here is refused rather than run.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import zipfile
from pathlib import Path

import pytest

from textflowkit.core import streaming_assets as sa

#: The real platform selector, captured before any fixture can stub it.
_REAL_CURRENT_FOLDER = sa.current_streaming_folder


# --- helpers ---------------------------------------------------------------


def _build_wheel(members: dict[str, bytes]) -> bytes:
    """Build an in-memory wheel (zip) with the given member -> bytes entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


#: A stand-in for the pinned library member, and the wheel that carries it.
_MEMBER_NAME = "needle/libneedle3.dll"
_MEMBER_BYTES = b"\x4d\x5a" + bytes(range(256)) * 4  # a small "binary"
_WHEEL_BYTES = _build_wheel({
    _MEMBER_NAME: _MEMBER_BYTES,
    "needle/__init__.py": b"# unused by this product\n",
    "cactus_needle-3.2.0.dist-info/METADATA": b"Name: cactus-needle\n",
})


def _library_for(wheel: bytes, member: bytes, *, member_verified: bool = True,
                 member_name: str = _MEMBER_NAME) -> sa.StreamingLibrary:
    return sa.StreamingLibrary(
        folder="windows-x86_64",
        wheel_url="",  # filled by the server fixture
        wheel_sha256=hashlib.sha256(wheel).hexdigest(),
        wheel_size=len(wheel),
        member=member_name,
        member_sha256=hashlib.sha256(member).hexdigest(),
        member_size=len(member),
        member_verified=member_verified,
    )


class _Server:
    """A loopback HTTP server serving one path; a stand-in for the pinned URL."""

    def __init__(self, body: bytes, *, url_path: str = "/wheel"):
        self.body = body
        self.url_path = url_path
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != outer.url_path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args):  # silence
                return

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}{self.url_path}"

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolated_streaming_cache(monkeypatch, tmp_path):
    """Point the streaming cache at this test's tmp dir; default to the 'verified' pin."""
    from textflowkit.core import whistle_assets as wa

    monkeypatch.setenv(wa.ENV_MODELS_DIR, str(tmp_path / "models"))
    monkeypatch.delenv(wa.ENV_OFFLINE, raising=False)
    monkeypatch.setattr(sa, "current_streaming_folder", lambda: "windows-x86_64")
    yield


@pytest.fixture
def allow_loopback_http(monkeypatch):
    """Relax only the HTTPS scheme guard so the size/hash logic runs on real bytes."""
    monkeypatch.setattr(sa, "_assert_pinned_scheme", lambda url: None)


def _install_pin(monkeypatch, library: sa.StreamingLibrary, url: str) -> sa.StreamingLibrary:
    """Publish a single monkeypatched pin whose wheel URL is the loopback server."""
    pinned = sa.StreamingLibrary(
        folder=library.folder, wheel_url=url,
        wheel_sha256=library.wheel_sha256, wheel_size=library.wheel_size,
        member=library.member, member_sha256=library.member_sha256,
        member_size=library.member_size, member_verified=library.member_verified,
    )
    monkeypatch.setitem(sa.STREAMING_LIBRARIES, "windows-x86_64", pinned)
    return pinned


# --- platform selection ----------------------------------------------------


def test_windows_x86_64_is_the_verified_target():
    library = sa.STREAMING_LIBRARIES["windows-x86_64"]
    assert library.member_verified is True
    assert library.member_size is not None and library.member_sha256 is not None
    assert Path(library.member).name == "libneedle3.dll"


def test_unverified_platforms_publish_a_wheel_but_no_member_hash():
    # The non-Windows wheels are pinned by wheel hash; their *member* hashes are
    # not verified here, and that is recorded so the loader can refuse them.
    for folder, library in sa.STREAMING_LIBRARIES.items():
        if folder == "windows-x86_64":
            continue
        assert library.member_verified is False
        assert library.member_sha256 is None and library.member_size is None


def test_unsupported_platform_is_refused_with_a_clear_message(monkeypatch):
    # Use the real selector captured at import (the autouse fixture stubs the
    # module attribute), then feed it an unpublished platform.
    monkeypatch.setattr(sa.sys, "platform", "linux")
    monkeypatch.setattr(sa._platform, "machine", lambda: "mips")
    with pytest.raises(sa.StreamingAssetError, match="no native library"):
        _REAL_CURRENT_FOLDER()


def test_streaming_cache_dir_is_versioned_and_absolute(monkeypatch, tmp_path):
    monkeypatch.delenv("TEXTFLOWKIT_STREAMING_DIR", raising=False)
    directory = sa.streaming_dir()
    assert directory.is_absolute()
    assert "streaming" in directory.parts
    assert directory.name == f"libneedle3-{sa.STREAMING_LIBRARY_REVISION[:12]}"


def test_streaming_dir_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom-streaming"
    monkeypatch.setenv("TEXTFLOWKIT_STREAMING_DIR", str(target))
    assert sa.streaming_dir() == target.resolve()


# --- download, verify, extract, install ------------------------------------


def test_download_verifies_wheel_then_installs_only_the_member(monkeypatch, allow_loopback_http):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    with _Server(_WHEEL_BYTES) as url:
        _install_pin(monkeypatch, library, url)
        final = sa.ensure_streaming_library()
    assert final.exists()
    assert final.name == "libneedle3.dll"
    assert final.read_bytes() == _MEMBER_BYTES
    # The wheel itself is not left in the cache; only the member is installed.
    leftovers = [p.name for p in sa.streaming_dir().iterdir() if p.suffix == ".whl"]
    assert leftovers == []


def test_a_cached_member_is_reverified_before_it_is_trusted(monkeypatch, allow_loopback_http):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    with _Server(_WHEEL_BYTES) as url:
        _install_pin(monkeypatch, library, url)
        final = sa.ensure_streaming_library()
    # Corrupt the installed member; the next call must refuse, not reuse it.
    final.write_bytes(b"tampered")
    with _Server(_WHEEL_BYTES) as url:  # server present but must not be needed
        _install_pin(monkeypatch, library, url)
        with pytest.raises(sa.StreamingAssetError):
            sa.ensure_streaming_library()


def test_wheel_size_mismatch_is_refused_and_leaves_no_member(monkeypatch, allow_loopback_http):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    wrong = sa.StreamingLibrary(
        folder=library.folder, wheel_url=library.wheel_url,
        wheel_sha256=library.wheel_sha256, wheel_size=library.wheel_size + 1,
        member=library.member, member_sha256=library.member_sha256,
        member_size=library.member_size, member_verified=True,
    )
    with _Server(_WHEEL_BYTES) as url:
        _install_pin(monkeypatch, wrong, url)
        with pytest.raises(sa.StreamingAssetError):
            sa.ensure_streaming_library()
    assert not (sa.streaming_dir() / "libneedle3.dll").exists()


def test_wheel_hash_mismatch_is_refused(monkeypatch, allow_loopback_http):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    tampered = sa.StreamingLibrary(
        folder=library.folder, wheel_url=library.wheel_url,
        wheel_sha256="0" * 64, wheel_size=library.wheel_size,
        member=library.member, member_sha256=library.member_sha256,
        member_size=library.member_size, member_verified=True,
    )
    with _Server(_WHEEL_BYTES) as url:
        _install_pin(monkeypatch, tampered, url)
        with pytest.raises(sa.StreamingAssetError):
            sa.ensure_streaming_library()


def test_member_hash_mismatch_is_refused_even_when_the_wheel_verifies(
    monkeypatch, allow_loopback_http
):
    # The wheel is exactly the pinned one, but the *member* pin names different
    # bytes of the SAME LENGTH - so the size check passes and the hash check is
    # what refuses it. The member is what loads, so this must be refused.
    same_length_wrong = bytes((b + 1) & 0xFF for b in _MEMBER_BYTES)
    assert len(same_length_wrong) == len(_MEMBER_BYTES)
    library = _library_for(_WHEEL_BYTES, same_length_wrong)
    with _Server(_WHEEL_BYTES) as url:
        _install_pin(monkeypatch, library, url)
        with pytest.raises(sa.StreamingAssetError, match="sha256"):
            sa.ensure_streaming_library()
    assert not (sa.streaming_dir() / "libneedle3.dll").exists()


def test_a_wheel_missing_the_member_is_refused(monkeypatch, allow_loopback_http):
    other_wheel = _build_wheel({"needle/other.txt": b"nope"})
    library = _library_for(other_wheel, _MEMBER_BYTES)
    with _Server(other_wheel) as url:
        _install_pin(monkeypatch, library, url)
        with pytest.raises(sa.StreamingAssetError):
            sa.ensure_streaming_library()


def test_extraction_never_calls_extractall(monkeypatch, tmp_path):
    # Extraction reads the file by a fixed member name, so a traversal path in
    # the archive cannot be followed even if the pin named one.
    called = {"extractall": False}
    real_extractall = zipfile.ZipFile.extractall

    def _spy(self, *args, **kwargs):
        called["extractall"] = True
        return real_extractall(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "extractall", _spy)
    wheel = _build_wheel({_MEMBER_NAME: _MEMBER_BYTES})
    library = _library_for(wheel, _MEMBER_BYTES)
    wheel_path = tmp_path / "wheel.whl"
    wheel_path.write_bytes(wheel)
    data = sa._extract_member(wheel_path, library)
    assert data == _MEMBER_BYTES
    assert called["extractall"] is False


# --- offline and seeding ---------------------------------------------------


def test_offline_refuses_a_missing_member_without_downloading(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_OFFLINE", "1")
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    monkeypatch.setitem(sa.STREAMING_LIBRARIES, "windows-x86_64", library)
    with pytest.raises(sa.StreamingAssetError, match="offline"):
        sa.ensure_streaming_library()


def test_unverified_platform_is_refused_even_offline(monkeypatch):
    unverified = _library_for(_WHEEL_BYTES, _MEMBER_BYTES, member_verified=False)
    monkeypatch.setitem(sa.STREAMING_LIBRARIES, "windows-x86_64", unverified)
    with pytest.raises(sa.StreamingAssetError, match="verified"):
        sa.ensure_streaming_library()


def test_seed_verifies_before_copying(monkeypatch, tmp_path):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    monkeypatch.setitem(sa.STREAMING_LIBRARIES, "windows-x86_64", library)
    good = tmp_path / "libneedle3.dll"
    good.write_bytes(_MEMBER_BYTES)
    installed = sa.seed_streaming_cache_from(good)
    assert installed.read_bytes() == _MEMBER_BYTES

    bad = tmp_path / "bad.dll"
    bad.write_bytes(b"not the pinned member")
    with pytest.raises(sa.StreamingAssetError):
        sa.seed_streaming_cache_from(bad)


def test_cache_status_reports_presence_without_verifying(monkeypatch):
    library = _library_for(_WHEEL_BYTES, _MEMBER_BYTES)
    monkeypatch.setitem(sa.STREAMING_LIBRARIES, "windows-x86_64", library)
    status = sa.streaming_cache_status()
    assert status["platform"] == "windows-x86_64"
    assert status["present"] is False
    assert status["member_verified"] is True
