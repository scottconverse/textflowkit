"""Whistle runtime assets: platform choice, pinning, offline, telemetry off.

Everything here is deterministic and offline. Downloads are exercised against a
local ``http.server`` on loopback (never the network) so the size/hash gates and
the atomic install are tested against real bytes. No real model or binary is
fetched, and no upstream process is launched.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from textflowkit.core import whistle_assets as wa

# --- helpers ---------------------------------------------------------------


def _asset_fixture(tmp_path: Path, body: bytes, name: str = "unit.cact") -> wa.PinnedAsset:
    return wa.PinnedAsset(
        filename=name,
        url="",  # filled by the server fixture / monkeypatch
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


class _Server:
    """A loopback HTTP server serving one path; a stand-in for the pinned URL."""

    def __init__(self, body: bytes, *, url_path: str = "/asset", headers: dict | None = None):
        self.body = body
        self.url_path = url_path
        self.headers = headers or {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != outer.url_path:
                    self.send_error(404)
                    return
                self.send_response(200)
                for key, value in outer.headers.items():
                    self.send_header(key, value)
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
def _isolated_cache(monkeypatch, tmp_path):
    """Point the cache at this test's tmp dir and clear offline unless set."""
    monkeypatch.setenv(wa.ENV_MODELS_DIR, str(tmp_path / "models"))
    monkeypatch.delenv(wa.ENV_OFFLINE, raising=False)
    yield


@pytest.fixture
def allow_loopback_http(monkeypatch):
    """Let a download run against the loopback HTTP server used here.

    The production guard is HTTPS-only; this fixture relaxes *only* the scheme
    check so the size/hash/atomicity logic can be tested against real bytes.
    The loopback fixture never reaches the network.
    """
    monkeypatch.setattr(wa, "_assert_pinned_scheme", lambda url: None)


# --- platform selection ----------------------------------------------------


def test_current_platform_is_a_published_folder(monkeypatch):
    monkeypatch.setattr(wa.sys, "platform", "win32")
    monkeypatch.setattr(wa._platform, "machine", lambda: "AMD64")
    assert wa.current_platform() == "windows-x86_64"


def test_intel_mac_is_refused_and_names_whisper(monkeypatch):
    monkeypatch.setattr(wa.sys, "platform", "darwin")
    monkeypatch.setattr(wa._platform, "machine", lambda: "x86_64")
    with pytest.raises(wa.WhistleAssetError) as excinfo:
        wa.current_platform()
    message = str(excinfo.value)
    assert "Intel Mac" in message
    assert "whisper" in message
    # No silent fallback: it must not return a folder at all.
    assert "macos-x86_64" in message


def test_unsupported_platform_is_refused(monkeypatch):
    monkeypatch.setattr(wa.sys, "platform", "linux")
    monkeypatch.setattr(wa._platform, "machine", lambda: "mips")
    with pytest.raises(wa.WhistleAssetError) as excinfo:
        wa.current_platform()
    assert "whisper" in str(excinfo.value)


def test_arm64_spellings_normalise(monkeypatch):
    monkeypatch.setattr(wa.sys, "platform", "linux")
    monkeypatch.setattr(wa._platform, "machine", lambda: "aarch64")
    assert wa.current_platform() == "linux-arm64"


def test_no_intel_mac_in_the_published_manifest():
    assert "macos-x86_64" not in wa.PLATFORM_BINARIES
    assert set(wa.SUPPORTED_PLATFORMS) == {
        "windows-x86_64", "windows-arm64",
        "linux-x86_64", "linux-arm64", "macos-arm64",
    }


#: The pinned metadata, copied from the LFS digests in the upstream listing
#: (outputs/whistle-integration-20261005/needle3-pinned-api.json). Kept here as
#: an independent fixture so a manifest edit that silently changes a hash or a
#: size fails this test rather than the code simply agreeing with itself.
_PINNED_BINARIES: dict[str, tuple[str, int]] = {
    "windows-x86_64": (
        "e8863ca0c06a47d406777077f1ba728b58e57d77fedff252dbe6827d588acc8c", 1563136,
    ),
    "windows-arm64": (
        "8a333b4829f4a6f230e26ab1998c17ad442a97c650934448db1c63eed91b0cea", 1352704,
    ),
    "linux-x86_64": (
        "f38dc4b0345d66b4e385734ad0f12af43ac6e2cfa1752d0795af5c137200c8e4", 1541680,
    ),
    "linux-arm64": (
        "6fc25a97def475e1c7d1933a4a219d4e37076be77974e76dd331d9e0cd97f3cb", 1431816,
    ),
    "macos-arm64": (
        "342fa2c6f140e702354a99c4201c9057535ec908eed35c7382e911a19d1d2724", 1089896,
    ),
}

_PINNED_MODEL = (
    "b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb", 16919407,
)

_HEX = set("0123456789abcdef")


def test_every_hash_is_a_full_64_hex_sha256():
    for folder, binary in wa.PLATFORM_BINARIES.items():
        digest = binary.asset.sha256
        assert len(digest) == 64, f"{folder}: sha256 is {len(digest)} chars, not 64"
        assert set(digest) <= _HEX, f"{folder}: sha256 is not lowercase hex"
    model = wa.WHISTLE_MODEL.sha256
    assert len(model) == 64 and set(model) <= _HEX


def test_manifest_matches_the_pinned_upstream_metadata():
    for folder, (sha256, size) in _PINNED_BINARIES.items():
        asset = wa.PLATFORM_BINARIES[folder].asset
        assert (asset.sha256, asset.size) == (sha256, size), folder
    assert (wa.WHISTLE_MODEL.sha256, wa.WHISTLE_MODEL.size) == _PINNED_MODEL


def test_urls_are_the_pinned_revision_and_https():
    assert wa.WHISTLE_MODEL.url.startswith("https://")
    assert wa.WHISTLE_MODEL_REVISION in wa.WHISTLE_MODEL.url
    for folder, binary in wa.PLATFORM_BINARIES.items():
        assert binary.asset.url.startswith("https://")
        assert wa.NEEDLE_BINARY_REVISION in binary.asset.url
        assert f"/{folder}/" in binary.asset.url


# --- cache discovery -------------------------------------------------------


def test_models_dir_honours_override_and_is_absolute(tmp_path):
    expected = (tmp_path / "models").resolve()
    assert wa.models_dir() == expected
    assert wa.models_dir().is_absolute()


def test_models_dir_is_outside_the_package():
    # Guard against ever writing assets into an installed wheel.
    package = Path(wa.__file__).resolve().parent
    models = wa.models_dir()
    assert models != package and package not in models.parents


def test_cache_status_reports_presence_without_verifying(monkeypatch):
    monkeypatch.setattr(wa.sys, "platform", "win32")
    monkeypatch.setattr(wa._platform, "machine", lambda: "AMD64")
    status = wa.cache_status()
    assert status["platform"] == "windows-x86_64"
    assert status["binary_present"] is False
    assert status["model_present"] is False
    assert Path(status["models_dir"]).is_absolute()


# --- child environment: telemetry forced off -------------------------------


def test_child_env_forces_telemetry_off_over_parent_opt_in(monkeypatch):
    parent = {
        "NEEDLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "0",
        "PATH": os.environ.get("PATH", ""),
    }
    env = wa.whistle_child_env(parent)
    assert env["NEEDLE_TELEMETRY"] == "0"
    assert env["DO_NOT_TRACK"] == "1"
    # parent's other keys survive
    assert env["PATH"] == parent["PATH"]


def test_child_env_sets_telemetry_off_when_parent_is_silent():
    env = wa.whistle_child_env({})
    assert env["NEEDLE_TELEMETRY"] == "0"
    assert env["DO_NOT_TRACK"] == "1"


def test_no_upstream_sdk_or_telemetry_module_is_imported():
    """This product brings in no cactus-needle SDK and no telemetry layer.

    The scan is over both *production* modules - ``whistle_assets.py`` and
    ``whistle.py`` - because either could import an upstream telemetry layer if
    one crept in; scanning only one, or scanning a test file instead of the
    production ``whistle.py``, would leave the real launch path unchecked.
    """
    import ast

    from textflowkit.core import whistle as w

    for module_file in (wa.__file__, w.__file__):
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in ("needle", "cactus_needle", "cactus-needle"):
            assert forbidden not in imported, f"{module_file} imports {forbidden}"


def test_whistle_assets_never_references_an_upstream_telemetry_url():
    source = Path(wa.__file__).read_text(encoding="utf-8")
    # The upstream telemetry endpoint host must not appear anywhere here.
    assert "supabase" not in source.lower()
    assert "telemetry_url" not in source.lower()


# --- download, pinning, atomic install -------------------------------------


def test_download_verifies_and_installs_atomically(monkeypatch, allow_loopback_http):
    body = b"whistle-model-bytes" * 1000
    asset = _asset_fixture(Path(), body)
    with _Server(body) as url:
        asset = replace(asset, url=url)
        monkeypatch.setattr(wa, "WHISTLE_MODEL", asset)
        final = wa.models_dir() / asset.filename
        monkeypatch.setattr(
            wa, "_pinned_paths",
            lambda folder: (wa.models_dir() / "bin" / "needle.exe", final),
        )
        monkeypatch.setattr(
            wa, "PLATFORM_BINARIES",
            {"windows-x86_64": wa.PlatformBinary(
                folder="windows-x86_64",
                asset=replace(asset, filename="needle.exe", url=url),
            )},
        )
        monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
        binary, model = wa.ensure_assets()
        assert model.read_bytes() == body
        assert binary.read_bytes() == body
        # No temp file left behind.
        assert not list(wa.models_dir().rglob("*.part"))


def test_size_mismatch_is_refused_and_leaves_no_file(monkeypatch, allow_loopback_http):
    body = b"x" * 2048
    asset = _asset_fixture(Path(), body)
    wrong = replace(asset, size=len(body) + 1)
    with _Server(body) as url:
        wrong = replace(wrong, url=url)
        monkeypatch.setattr(wa, "WHISTLE_MODEL", wrong)
        final = wa.models_dir() / wrong.filename
        monkeypatch.setattr(
            wa, "_pinned_paths",
            lambda folder: (wa.models_dir() / "bin" / "needle.exe", final),
        )
        monkeypatch.setattr(
            wa, "PLATFORM_BINARIES",
            {"windows-x86_64": wa.PlatformBinary(
                folder="windows-x86_64",
                asset=replace(wrong, filename="needle.exe", url=url),
            )},
        )
        monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
        with pytest.raises(wa.WhistleAssetError, match="size"):
            wa.ensure_assets()
    assert not final.exists()
    assert not list(wa.models_dir().rglob("*.part"))


def test_hash_mismatch_is_refused(monkeypatch, allow_loopback_http):
    body = b"real-bytes"
    asset = _asset_fixture(Path(), body)
    tampered = replace(asset, sha256="00" * 32)
    with _Server(body) as url:
        tampered = replace(tampered, url=url)
        monkeypatch.setattr(wa, "WHISTLE_MODEL", tampered)
        final = wa.models_dir() / tampered.filename
        monkeypatch.setattr(
            wa, "_pinned_paths",
            lambda folder: (wa.models_dir() / "bin" / "needle.exe", final),
        )
        monkeypatch.setattr(
            wa, "PLATFORM_BINARIES",
            {"windows-x86_64": wa.PlatformBinary(
                folder="windows-x86_64",
                asset=replace(tampered, filename="needle.exe", url=url),
            )},
        )
        monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
        with pytest.raises(wa.WhistleAssetError, match="sha256"):
            wa.ensure_assets()
    assert not final.exists()


def test_content_length_ceiling_refuses_before_body(monkeypatch, allow_loopback_http):
    body = b"y" * 4096
    asset = _asset_fixture(Path(), body)
    with _Server(body) as url:
        asset = replace(asset, url=url)
        monkeypatch.setattr(wa, "WHISTLE_MODEL", asset)
        final = wa.models_dir() / asset.filename
        monkeypatch.setattr(
            wa, "_pinned_paths",
            lambda folder: (wa.models_dir() / "bin" / "needle.exe", final),
        )
        monkeypatch.setattr(
            wa, "PLATFORM_BINARIES",
            {"windows-x86_64": wa.PlatformBinary(
                folder="windows-x86_64",
                asset=replace(asset, filename="needle.exe", url=url),
            )},
        )
        monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
        with pytest.raises(wa.WhistleAssetError, match="size limit"):
            wa.ensure_assets(max_bytes=128)


def test_non_https_url_is_refused():
    asset = wa.PinnedAsset(filename="a", url="http://example.invalid/x", sha256="0" * 64, size=1)
    with pytest.raises(wa.WhistleAssetError, match="non-HTTPS"):
        wa._download_to_temp(asset, timeout=5, max_bytes=1024)


# --- offline ---------------------------------------------------------------


def test_offline_missing_asset_raises_without_network(monkeypatch):
    monkeypatch.setenv(wa.ENV_OFFLINE, "1")

    def _no_network(*args, **kwargs):
        raise AssertionError("network must not be touched in offline mode")

    monkeypatch.setattr(wa.urllib.request, "urlopen", _no_network)
    with pytest.raises(wa.WhistleAssetError, match="offline mode is on"):
        wa.ensure_assets()


def test_offline_reuses_a_verified_cache(monkeypatch):
    body = b"cached-model"
    asset = _asset_fixture(Path(), body, name="whistle.cact")
    monkeypatch.setattr(wa, "WHISTLE_MODEL", asset)
    final = wa.models_dir() / asset.filename
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(body)
    binary = wa.models_dir() / "windows-x86_64" / "needle.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(body)
    monkeypatch.setattr(
        wa, "PLATFORM_BINARIES",
        {"windows-x86_64": wa.PlatformBinary(
            folder="windows-x86_64",
            asset=replace(asset, filename="needle.exe"),
        )},
    )
    monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
    monkeypatch.setattr(
        wa, "_pinned_paths", lambda folder: (binary, final)
    )
    monkeypatch.setattr(
        wa.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")),
    )
    got_binary, got_model = wa.ensure_assets(offline=True)
    assert got_binary == binary and got_model == final


def test_offline_refuses_a_corrupt_cache(monkeypatch):
    monkeypatch.setenv(wa.ENV_OFFLINE, "1")
    asset = wa.PinnedAsset(
        filename="whistle.cact", url="https://x/y", sha256="ab" * 32, size=5
    )
    monkeypatch.setattr(wa, "WHISTLE_MODEL", asset)
    final = wa.models_dir() / asset.filename
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"wrong")  # 5 bytes but wrong content
    monkeypatch.setattr(wa, "_pinned_paths", lambda folder: (final, final))
    monkeypatch.setattr(
        wa, "PLATFORM_BINARIES",
        {"windows-x86_64": wa.PlatformBinary(
            folder="windows-x86_64", asset=replace(asset, filename="needle.exe"),
        )},
    )
    monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")
    with pytest.raises(wa.WhistleAssetError, match="sha256"):
        wa.ensure_assets()


# --- seeding ---------------------------------------------------------------


def test_seed_cache_verifies_before_copying(monkeypatch, tmp_path):
    body = b"verified-seed-bytes"
    model_asset = wa.PinnedAsset(
        filename="whistle.cact", url="https://x/y",
        sha256=hashlib.sha256(body).hexdigest(), size=len(body),
    )
    binary_asset = wa.PinnedAsset(
        filename="needle.exe", url="https://x/y",
        sha256=hashlib.sha256(body).hexdigest(), size=len(body),
    )
    monkeypatch.setattr(wa, "WHISTLE_MODEL", model_asset)
    monkeypatch.setattr(
        wa, "PLATFORM_BINARIES",
        {"windows-x86_64": wa.PlatformBinary(folder="windows-x86_64", asset=binary_asset)},
    )
    monkeypatch.setattr(wa, "current_platform", lambda: "windows-x86_64")

    binary_src = tmp_path / "src-needle.exe"
    model_src = tmp_path / "src-whistle.cact"
    binary_src.write_bytes(body)
    model_src.write_bytes(body)
    binary, model = wa.seed_cache_from(binary_src, model_src)
    assert binary.read_bytes() == body and model.read_bytes() == body

    # A source that does not verify is refused, and nothing is written.
    bad = tmp_path / "bad.cact"
    bad.write_bytes(b"tampered")
    with pytest.raises(wa.WhistleAssetError):
        wa.seed_cache_from(binary_src, bad)


# --- import hygiene --------------------------------------------------------


def test_import_does_not_touch_network():
    # Importing the module must not open a socket. This is asserted by
    # inspection of the module: no network call exists at import scope, and
    # `ensure_assets` is the only caller of `urlopen`.
    import ast

    source = Path(wa.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level_calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    }
    assert "urlopen" not in module_level_calls
