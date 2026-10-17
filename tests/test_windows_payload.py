from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_windows_payload as payload


def _zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_payload_builder_module_import_does_not_require_tomllib() -> None:
    code = """
import builtins
original_import = builtins.__import__
def reject_tomllib(name, *args, **kwargs):
    if name == "tomllib":
        raise ModuleNotFoundError("tomllib is unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = reject_tomllib
import scripts.prepare_windows_payload
"""
    subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )


def test_corrupt_locked_asset_is_rejected(tmp_path: Path) -> None:
    asset = tmp_path / "runtime.zip"
    asset.write_bytes(b"not the locked content")
    spec = {
        "filename": asset.name,
        "sha256": hashlib.sha256(b"locked content").hexdigest(),
        "size": len(b"locked content"),
        "url": "https://example.invalid/runtime.zip",
    }

    with pytest.raises(payload.PayloadError, match="(SHA-256|size|hash)"):
        payload.verify_asset_file(asset, spec)


def test_asset_lock_accepts_optional_streaming_and_future_keys(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"").hexdigest()
    assets = {
        name: {
            "filename": f"{name}.zip",
            "url": f"https://example.invalid/{name}.zip",
            "sha256": digest,
            "size": 0,
        }
        for name in payload.REQUIRED_ASSETS
    }
    assets["streaming"] = {
        "filename": "streaming.whl",
        "url": "https://example.invalid/streaming.whl",
        "sha256": digest,
        "size": 0,
    }
    assets["future_source_bundle"] = {"coordinator_owned": True}
    lock_path = tmp_path / "assets.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "python_version": "3.13.13",
                "node_version": "22.23.3",
                "platform": "windows-x86_64",
                "assets": assets,
            }
        ),
        encoding="utf-8",
    )

    lock = payload.load_asset_lock(lock_path)

    assert lock["assets"]["streaming"]["filename"] == "streaming.whl"
    assert "future_source_bundle" not in lock["assets"]


@pytest.mark.parametrize(
    "member_name",
    ["../../escape.txt", "C:/escape.txt", r"..\..\escape.txt"],
)
def test_zip_traversal_is_rejected_before_writing_outside(
    tmp_path: Path, member_name: str
) -> None:
    archive = _zip(tmp_path / "hostile.zip", {member_name: b"escape"})
    destination = tmp_path / "extract"

    with pytest.raises(payload.PayloadError, match="(unsafe|traversal|path)"):
        payload.safe_extract_zip(archive, destination)

    assert not (tmp_path.parent / "escape.txt").exists()
    assert not destination.exists() or not list(destination.rglob("*"))


def test_output_directory_must_be_external_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(payload.PayloadError, match="outside.*repo|repo.*outside"):
        payload.validate_output_dir(repo / "payload", repo)

    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(payload.PayloadError, match="empty"):
        payload.validate_output_dir(external, repo)

    link = tmp_path / "payload-link"
    link.mkdir()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )
    with pytest.raises(payload.PayloadError, match="symlink"):
        payload.validate_output_dir(link, repo)


def test_embedded_python_pth_config_keeps_site_packages_without_site_import(
    tmp_path: Path,
) -> None:
    archive = _zip(
        tmp_path / "python.zip",
        {
            "python.exe": b"python",
            "python313.zip": b"stdlib",
            "python313._pth": b"python313.zip\n# original\n",
            "LICENSE.txt": b"CPython license",
        },
    )

    runtime = tmp_path / "runtime"
    payload.prepare_python_runtime(archive, runtime)

    assert (runtime / "python.exe").read_bytes() == b"python"
    assert (runtime / "LICENSE.txt").read_text(encoding="utf-8") == "CPython license"
    pth = (runtime / "python313._pth").read_text(encoding="ascii")
    assert pth == "python313.zip\n.\nLib/site-packages\n"
    assert "import site" not in pth.splitlines()
    assert "Lib/site-packages" in pth.splitlines()


def test_ffmpeg_keeps_runtime_bins_and_all_non_bin_legal_material(
    tmp_path: Path,
) -> None:
    prefix = "ffmpeg-build"
    archive = _zip(
        tmp_path / "ffmpeg.zip",
        {
            f"{prefix}/bin/ffmpeg.exe": b"ffmpeg",
            f"{prefix}/bin/ffprobe.exe": b"ffprobe",
            f"{prefix}/bin/ffplay.exe": b"optional player",
            f"{prefix}/bin/avcodec-63.dll": b"codec",
            f"{prefix}/LICENSE.txt": b"LGPL license",
            f"{prefix}/doc/licenses/libfoo.txt": b"third-party license",
            f"{prefix}/doc/README.html": b"documentation",
            f"{prefix}/include/libavcodec/avcodec.h": b"header",
        },
    )
    root = tmp_path / "payload"

    payload.prepare_ffmpeg_archive(archive, root)

    assert (root / "bin/ffmpeg.exe").read_bytes() == b"ffmpeg"
    assert (root / "bin/ffprobe.exe").read_bytes() == b"ffprobe"
    assert (root / "bin/avcodec-63.dll").read_bytes() == b"codec"
    assert not (root / "bin/ffplay.exe").exists()
    assert (root / "licenses/ffmpeg/LICENSE.txt").read_bytes() == b"LGPL license"
    assert (
        root / "licenses/ffmpeg/doc/licenses/libfoo.txt"
    ).read_bytes() == b"third-party license"
    assert (root / "licenses/ffmpeg/doc/README.html").exists()
    assert (root / "licenses/ffmpeg/include/libavcodec/avcodec.h").exists()


def test_node_selects_only_executable_and_license(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "node.zip",
        {
            "node-v22-win-x64/node.exe": b"node",
            "node-v22-win-x64/LICENSE": b"Node license",
            "node-v22-win-x64/README.md": b"unneeded",
        },
    )
    root = tmp_path / "payload"

    payload.prepare_node_archive(archive, root)

    assert (root / "bin/node.exe").read_bytes() == b"node"
    assert (root / "licenses/node/LICENSE").read_bytes() == b"Node license"
    assert not (root / "README.md").exists()


def test_streaming_wheel_contributes_only_the_pinned_dll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dll = b"dll"
    monkeypatch.setattr(payload, "STREAMING_DLL_SIZE", len(dll))
    monkeypatch.setattr(payload, "STREAMING_DLL_SHA256", hashlib.sha256(dll).hexdigest())
    wheel = _zip(
        tmp_path / "cactus_needle.whl",
        {
            "cactus_needle/lib/libneedle3.dll": dll,
            "cactus_needle/telemetry_sdk.py": b"must not be installed",
            "cactus_needle-3.2.0.dist-info/METADATA": b"metadata",
        },
    )
    root = tmp_path / "payload"

    payload.prepare_streaming_dll(wheel, root)

    assert (root / payload.STREAMING_DLL_RELATIVE_PATH).read_bytes() == dll
    assert not (root / "telemetry_sdk.py").exists()
    assert not list(root.rglob("*.py"))


def test_whistle_files_are_placed_at_the_runtime_contract_paths(
    tmp_path: Path,
) -> None:
    files = {}
    for name, content in (
        ("needle.exe", b"engine"),
        ("whistle.cact", b"model"),
        ("needle-LICENSE", b"engine license"),
        ("whistle-LICENSE", b"model license"),
    ):
        path = tmp_path / name
        path.write_bytes(content)
        files[name] = path
    root = tmp_path / "payload"

    payload.place_whistle_assets(
        engine=files["needle.exe"],
        model=files["whistle.cact"],
        engine_license=files["needle-LICENSE"],
        model_license=files["whistle-LICENSE"],
        payload_root=root,
    )

    assert (root / "models/whistle.cact").read_bytes() == b"model"
    assert (root / "models/windows-x86_64/needle.exe").read_bytes() == b"engine"
    assert (root / "licenses/needle-LICENSE").read_bytes() == b"engine license"
    assert (root / "licenses/whistle-LICENSE").read_bytes() == b"model license"


def test_builder_requires_native_python_313_x64() -> None:
    payload.validate_builder_python(
        version=(3, 13), machine="AMD64", pointer_bits=64, system="Windows"
    )
    with pytest.raises(payload.PayloadError, match="3.13"):
        payload.validate_builder_python(
            version=(3, 12), machine="AMD64", pointer_bits=64, system="Windows"
        )
    with pytest.raises(payload.PayloadError, match="Windows x86_64"):
        payload.validate_builder_python(
            version=(3, 13), machine="ARM64", pointer_bits=64, system="Windows"
        )


def test_local_wheel_build_uses_exact_supported_pip_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.11"\n', encoding="utf-8"
    )
    wheel_dir = tmp_path / "wheelhouse"
    wheel = wheel_dir / "textflowkit-0.1.11-py3-none-any.whl"
    captured = []

    def run(command, **kwargs):
        captured.append((command, kwargs))
        _zip(wheel, {
            "textflowkit-0.1.11.dist-info/METADATA":
                b"Name: textflowkit\nVersion: 0.1.11\n",
        })

    monkeypatch.setattr(payload.subprocess, "run", run)
    # This unit checks pip invocation and wheel validation, not TOML parsing.
    # The real installer builder requires Python 3.13; the general CI matrix
    # also runs this isolated unit on 3.10, which has no stdlib tomllib.
    def project_version(root):
        assert root == repo
        return "0.1.11"

    monkeypatch.setattr(payload, "_project_version", project_version)

    assert payload._build_local_wheel(repo, wheel_dir) == wheel.resolve()
    assert captured == [([
        sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
        "--wheel-dir", str(wheel_dir), str(repo),
    ], {"check": True, "cwd": repo})]


def test_dependency_wheel_cache_is_offline_and_local_project_wheel_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "textflowkit-0.1.11-py3-none-any.whl"
    cache = tmp_path / "dependency-wheels"
    cache.mkdir()
    captured: list[list[str]] = []

    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(
        payload.subprocess,
        "run",
        lambda command, **kwargs: captured.append(command),
    )
    monkeypatch.setattr(
        payload.importlib.metadata,
        "distributions",
        lambda path=None: [
            Distribution("textflowkit", "0.1.11"),
            Distribution("websockets", "15.0.1"),
        ],
    )

    dependencies = payload._install_runtime_dependencies(
        wheel, tmp_path / "site-packages", cache
    )

    command = captured[0]
    assert "--only-binary=:all:" in command
    assert "--no-compile" in command
    assert "--no-index" in command
    assert command[command.index("--find-links") + 1] == str(cache)
    assert command[-1] == (
        "textflowkit[http,export,streaming] @ " + wheel.resolve().as_uri()
    )
    assert dependencies["textflowkit"] == "0.1.11"


def test_release_notices_gpl_text_and_source_metadata_are_retained(
    tmp_path: Path,
) -> None:
    windows = tmp_path / "packaging" / "windows"
    windows.mkdir(parents=True)
    (windows / "THIRD-PARTY-NOTICES.txt").write_text("notices", encoding="utf-8")
    (windows / "source-materials.json").write_text('{"materials": []}', encoding="utf-8")
    cache = tmp_path / "asset-cache"
    cache.mkdir()
    gpl = cache / "GPL-3.0.txt"
    gpl.write_text("GNU GPL version 3", encoding="utf-8")
    root = tmp_path / "payload"

    copied = payload.copy_release_license_material(
        payload_root=root,
        packaging_windows=windows,
        asset_cache=cache,
    )

    assert (root / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8") == "notices"
    assert (root / "licenses/GPL-3.0.txt").read_text(encoding="utf-8") == "GNU GPL version 3"
    assert (root / "licenses/source-materials.json").read_text(encoding="utf-8") == (
        '{"materials": []}'
    )
    assert copied["gpl_license"]["filename"] == "GPL-3.0.txt"
    assert copied["gpl_license"]["sha256"] == hashlib.sha256(
        b"GNU GPL version 3"
    ).hexdigest()


def test_manifest_records_payload_files_but_not_itself(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    (root / "bin").mkdir(parents=True)
    (root / "bin/ffprobe.exe").write_bytes(b"probe")
    document = payload.build_manifest(
        payload_root=root,
        version="1.2.3",
        python_version="3.13.13",
        assets={"python": {"filename": "python.zip", "sha256": "a" * 64}},
        dependencies={"textflowkit": "1.2.3"},
        created_utc="2026-10-14T00:00:00Z",
    )

    assert document["version"] == "1.2.3"
    assert document["platform"] == "windows-x86_64"
    assert document["python_version"] == "3.13.13"
    assert document["assets"]["python"]["filename"] == "python.zip"
    assert document["dependencies"] == {"textflowkit": "1.2.3"}
    assert document["files"] == [
        {
            "path": "bin/ffprobe.exe",
            "sha256": hashlib.sha256(b"probe").hexdigest(),
            "size": 5,
        }
    ]
    assert "manifest.json" not in {item["path"] for item in document["files"]}
    json.dumps(document)
