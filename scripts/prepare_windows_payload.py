"""Build a verified, isolated Windows payload for the TextFlowKit installer.

The output directory is the payload root. Build-time downloads are permitted
only for HTTPS URLs pinned in ``assets.lock.json``; the installed runtime gets
no setup-time Python or global PATH changes from this script.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "packaging" / "windows" / "assets.lock.json"
REQUIRED_ASSETS = (
    "python",
    "node",
    "ffmpeg",
    "engine",
    "model",
    "engine_license",
    "model_license",
)
OPTIONAL_ASSET_KEYS = {"streaming"}
PLATFORM = "windows-x86_64"
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
STREAMING_DLL_SHA256 = "d20c72dccd7557b10493fae3d6af97e4456d420cb454804d5716fa2cb15eb774"
STREAMING_DLL_SIZE = 1_524_736
STREAMING_DLL_RELATIVE_PATH = Path(
    "models/streaming/libneedle3-2ae11323dc00/libneedle3.dll"
)
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class PayloadError(RuntimeError):
    """A locked input or payload layout is invalid."""


@dataclass(frozen=True)
class ZipMember:
    info: zipfile.ZipInfo
    relative_path: PurePosixPath


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, *, filename: str | None = None) -> dict[str, Any]:
    _require_regular_file(path)
    return {
        "filename": filename or path.name,
        "sha256": _hash_file(path),
        "size": path.stat().st_size,
    }


def _require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise PayloadError(f"Required file is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or _is_junction(path):
        raise PayloadError(f"Refusing symlink or junction input: {path}")
    if not stat.S_ISREG(mode):
        raise PayloadError(f"Expected a regular file: {path}")


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return False


def _validate_asset_spec(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise PayloadError(f"Invalid asset key: {name!r}")
    if not isinstance(value, dict):
        raise PayloadError(f"Asset {name!r} must be an object")
    filename = value.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or filename.rstrip(" .") != filename
    ):
        raise PayloadError(f"Asset {name!r} has an unsafe filename")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise PayloadError(f"Asset {name!r} has an invalid SHA-256")
    size = value.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise PayloadError(f"Asset {name!r} has an invalid size")
    url = value.get("url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise PayloadError(f"Asset {name!r} must use an HTTPS URL")
    return {
        "filename": filename,
        "url": url,
        "sha256": digest.lower(),
        "size": size,
    }


def load_asset_lock(lock_path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadError(f"Cannot read asset lock {lock_path}: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("schema") != 1:
        raise PayloadError("Asset lock must be a schema 1 object")
    if lock.get("platform") != PLATFORM:
        raise PayloadError(f"Asset lock platform must be {PLATFORM}")
    python_version = lock.get("python_version")
    if not isinstance(python_version, str) or not re.fullmatch(r"3\.13\.\d+", python_version):
        raise PayloadError("Asset lock must pin a Python 3.13 patch version")
    if not isinstance(lock.get("node_version"), str):
        raise PayloadError("Asset lock must provide node_version")
    raw_assets = lock.get("assets")
    if not isinstance(raw_assets, dict):
        raise PayloadError("Asset lock assets must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    seen_filenames: dict[str, str] = {}
    for key in REQUIRED_ASSETS:
        if key not in raw_assets:
            raise PayloadError(f"Asset lock is missing required asset {key!r}")
    accepted_keys = set(REQUIRED_ASSETS) | OPTIONAL_ASSET_KEYS
    for key, value in raw_assets.items():
        if key in accepted_keys:
            normalized[key] = _validate_asset_spec(key, value)
        elif (
            isinstance(key, str)
            and isinstance(value, dict)
            and (
                "gpl" in key.casefold()
                or str(value.get("filename", "")).casefold() == "gpl-3.0.txt"
            )
        ):
            # The coordinator can add the separately pinned GPL text without
            # requiring older builders or source-only optional assets to know
            # every future lock key.
            normalized[key] = _validate_asset_spec(key, value)
    for key in normalized:
        folded = normalized[key]["filename"].casefold()
        previous = seen_filenames.get(folded)
        if previous is not None:
            raise PayloadError(
                f"Asset filenames must be unique: {previous!r} and {key!r}"
            )
        seen_filenames[folded] = key
    lock["assets"] = normalized
    return lock


def verify_asset_file(path: Path, spec: dict[str, Any]) -> dict[str, Any]:
    _require_regular_file(path)
    actual_size = path.stat().st_size
    if actual_size != spec["size"]:
        raise PayloadError(
            f"Asset size mismatch for {path.name}: expected {spec['size']}, got {actual_size}"
        )
    actual_hash = _hash_file(path)
    if actual_hash != spec["sha256"].lower():
        raise PayloadError(
            f"Asset SHA-256 mismatch for {path.name}: expected {spec['sha256']}, got {actual_hash}"
        )
    return {
        "filename": spec["filename"],
        "sha256": actual_hash,
        "size": actual_size,
    }


def _download_locked_asset(cache_dir: Path, spec: dict[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir.is_symlink() or _is_junction(cache_dir):
        raise PayloadError(f"Refusing symlink or junction asset cache: {cache_dir}")
    target = cache_dir / spec["filename"]
    if os.path.lexists(target):
        verify_asset_file(target, spec)
        return target

    temp_path: Path | None = None
    request = urllib.request.Request(
        spec["url"], headers={"User-Agent": "TextFlowKit-Windows-Payload/1"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme.lower() != "https":
                raise PayloadError("Asset download redirected away from HTTPS")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{spec['filename']}.",
                suffix=".part",
                dir=cache_dir,
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > spec["size"]:
                        raise PayloadError(
                            f"Download exceeded locked size for {spec['filename']}"
                        )
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT_SECONDS:
                        raise PayloadError(
                            f"Download exceeded {DOWNLOAD_TIMEOUT_SECONDS}s for {spec['filename']}"
                        )
                    output.write(chunk)
                    digest.update(chunk)
        if size != spec["size"] or digest.hexdigest() != spec["sha256"].lower():
            raise PayloadError(f"Downloaded asset failed size or SHA-256 verification: {spec['filename']}")
        os.replace(temp_path, target)
        temp_path = None
        return target
    except PayloadError:
        raise
    except Exception as exc:
        raise PayloadError(f"HTTPS download failed for {spec['filename']}: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def obtain_locked_assets(
    lock: dict[str, Any], cache_dir: Path
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    if os.path.lexists(cache_dir) and (cache_dir.is_symlink() or _is_junction(cache_dir)):
        raise PayloadError(f"Refusing symlink or junction asset cache: {cache_dir}")
    if os.path.lexists(cache_dir) and not cache_dir.is_dir():
        raise PayloadError(f"Asset cache must be a directory: {cache_dir}")
    paths: dict[str, Path] = {}
    records: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_ASSETS:
        spec = lock["assets"][name]
        path = cache_dir / spec["filename"]
        if not path.exists():
            path = _download_locked_asset(cache_dir, spec)
        records[name] = verify_asset_file(path, spec)
        paths[name] = path

    # The streaming runtime is optional in older locks, and a missing optional
    # entry never blocks assembly. When the lock includes it, include only its
    # one pinned native DLL; never install the upstream SDK wheel.
    streaming_spec = lock["assets"].get("streaming")
    if streaming_spec is not None:
        path = cache_dir / streaming_spec["filename"]
        if not path.exists():
            path = _download_locked_asset(cache_dir, streaming_spec)
        records["streaming"] = verify_asset_file(path, streaming_spec)
        paths["streaming"] = path
    return paths, records


def _safe_zip_members(archive_path: Path) -> list[ZipMember]:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PayloadError(f"Invalid ZIP archive {archive_path}: {exc}") from exc
    members: list[ZipMember] = []
    seen: dict[str, bool] = {}
    files: set[str] = set()
    directories: set[str] = set()
    with archive:
        for info in archive.infolist():
            raw = info.filename.replace("\\", "/")
            if not raw or "\x00" in raw or raw.startswith("/"):
                raise PayloadError(f"Unsafe ZIP member path: {info.filename!r}")
            drive, _ = os.path.splitdrive(raw)
            if drive or re.match(r"^[A-Za-z]:", raw):
                raise PayloadError(f"Unsafe ZIP drive path: {info.filename!r}")
            is_dir = info.is_dir() or raw.endswith("/")
            normalized = raw[:-1] if is_dir and raw.endswith("/") else raw
            parts = normalized.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise PayloadError(f"Unsafe ZIP traversal path: {info.filename!r}")
            for part in parts:
                if (
                    ":" in part
                    or part.rstrip(" .") != part
                    or any(ord(character) < 32 for character in part)
                    or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
                ):
                    raise PayloadError(f"Unsafe Windows ZIP path component: {info.filename!r}")
            relative = PurePosixPath(*parts)
            path_key = relative.as_posix().casefold()
            if path_key in seen:
                raise PayloadError(f"Duplicate ZIP member path: {info.filename!r}")
            seen[path_key] = is_dir

            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise PayloadError(f"Refusing ZIP symlink member: {info.filename!r}")
            allowed_types = {0, stat.S_IFREG, stat.S_IFDIR}
            if file_type not in allowed_types or (is_dir and file_type == stat.S_IFREG):
                raise PayloadError(f"Refusing special ZIP member: {info.filename!r}")
            if is_dir:
                directories.add(path_key)
            else:
                files.add(path_key)
            members.append(ZipMember(info=info, relative_path=relative))

        for member in members:
            path_key = member.relative_path.as_posix().casefold()
            parts = member.relative_path.parts
            for length in range(1, len(parts)):
                parent_key = PurePosixPath(*parts[:length]).as_posix().casefold()
                if parent_key in files:
                    raise PayloadError(f"ZIP file is also used as a directory: {member.info.filename!r}")
            if not member.info.is_dir() and path_key in directories:
                raise PayloadError(f"ZIP path is both a file and directory: {member.info.filename!r}")
    return members


def _copy_zip_member(archive: zipfile.ZipFile, member: ZipMember, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive.open(member.info, "r") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=DOWNLOAD_CHUNK_SIZE)
    except FileExistsError as exc:
        raise PayloadError(f"ZIP extraction would overwrite {destination}") from exc
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise PayloadError(f"Could not extract {member.info.filename!r}: {exc}") from exc


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    members = _safe_zip_members(archive_path)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise PayloadError(f"ZIP extraction destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in members:
            if member.info.is_dir():
                (destination.joinpath(*member.relative_path.parts)).mkdir(
                    parents=True, exist_ok=True
                )
            else:
                _copy_zip_member(
                    archive,
                    member,
                    destination.joinpath(*member.relative_path.parts),
                )


def prepare_python_runtime(archive_path: Path, runtime_dir: Path) -> None:
    safe_extract_zip(archive_path, runtime_dir)
    required = ("python.exe", "python313.zip", "python313._pth", "LICENSE.txt")
    missing = [name for name in required if not (runtime_dir / name).is_file()]
    if missing:
        raise PayloadError(f"Embeddable Python archive is missing: {', '.join(missing)}")
    pth = runtime_dir / "python313._pth"
    with pth.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("python313.zip\n.\nLib/site-packages\n")
    (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def _copy_selected(archive_path: Path, selected: Iterable[tuple[ZipMember, Path]], root: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member, relative_output in selected:
            _copy_zip_member(archive, member, root / relative_output)


def prepare_ffmpeg_archive(archive_path: Path, payload_root: Path) -> None:
    members = _safe_zip_members(archive_path)
    ffmpeg_candidates = [
        member
        for member in members
        if len(member.relative_path.parts) == 3
        and member.relative_path.parts[1].casefold() == "bin"
        and member.relative_path.parts[2].casefold() == "ffmpeg.exe"
    ]
    if len(ffmpeg_candidates) != 1:
        raise PayloadError("FFmpeg archive must contain one package-prefix/bin/ffmpeg.exe")
    prefix = ffmpeg_candidates[0].relative_path.parts[0].casefold()
    package_members = [
        member
        for member in members
        if member.relative_path.parts[0].casefold() == prefix
    ]
    executables = {
        member.relative_path.parts[2].casefold()
        for member in package_members
        if len(member.relative_path.parts) == 3
        and member.relative_path.parts[1].casefold() == "bin"
        and not member.info.is_dir()
        and (
            member.relative_path.parts[2].casefold().endswith(".dll")
            or member.relative_path.parts[2].casefold() in {"ffmpeg.exe", "ffprobe.exe"}
        )
    }
    if not {"ffmpeg.exe", "ffprobe.exe"}.issubset(executables):
        raise PayloadError("FFmpeg package must contain ffmpeg.exe and ffprobe.exe")
    legal_material = [
        member
        for member in package_members
        if not (
            len(member.relative_path.parts) >= 2
            and member.relative_path.parts[1].casefold() == "bin"
        )
        and not member.info.is_dir()
    ]
    if not any(member.relative_path.name.casefold() == "license.txt" for member in legal_material):
        raise PayloadError("FFmpeg archive does not contain its LICENSE.txt")

    selected: list[tuple[ZipMember, Path]] = []
    for member in package_members:
        parts = member.relative_path.parts
        if member.info.is_dir():
            continue
        if len(parts) == 3 and parts[1].casefold() == "bin":
            filename = parts[2]
            folded = filename.casefold()
            if folded == "ffplay.exe":
                continue
            if folded in {"ffmpeg.exe", "ffprobe.exe"} or folded.endswith(".dll"):
                selected.append((member, Path("bin") / filename))
        elif len(parts) >= 2:
            selected.append((member, Path("licenses/ffmpeg").joinpath(*parts[1:])))
    _copy_selected(archive_path, selected, payload_root)


def prepare_node_archive(archive_path: Path, payload_root: Path) -> None:
    members = _safe_zip_members(archive_path)
    node_candidates = [
        member
        for member in members
        if len(member.relative_path.parts) == 2
        and member.relative_path.name.casefold() == "node.exe"
        and not member.info.is_dir()
    ]
    if len(node_candidates) != 1:
        raise PayloadError("Node archive must contain one package-prefix/node.exe")
    prefix = node_candidates[0].relative_path.parts[0].casefold()
    licenses = [
        member
        for member in members
        if len(member.relative_path.parts) == 2
        and member.relative_path.parts[0].casefold() == prefix
        and member.relative_path.name.casefold() in {"license", "license.txt"}
        and not member.info.is_dir()
    ]
    if len(licenses) != 1:
        raise PayloadError("Node archive must contain one package-root LICENSE file")
    _copy_selected(
        archive_path,
        [
            (node_candidates[0], Path("bin/node.exe")),
            (licenses[0], Path("licenses/node") / licenses[0].relative_path.name),
        ],
        payload_root,
    )


def prepare_streaming_dll(archive_path: Path, payload_root: Path) -> None:
    members = _safe_zip_members(archive_path)
    candidates = [
        member
        for member in members
        if member.relative_path.name.casefold() == "libneedle3.dll"
        and not member.info.is_dir()
    ]
    if len(candidates) != 1:
        raise PayloadError("Pinned streaming wheel must contain exactly one libneedle3.dll")
    member = candidates[0]
    if member.info.file_size != STREAMING_DLL_SIZE:
        raise PayloadError(
            f"Streaming DLL size mismatch: expected {STREAMING_DLL_SIZE}, got {member.info.file_size}"
        )
    target = payload_root / STREAMING_DLL_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            zipfile.ZipFile(archive_path) as archive,
            archive.open(member.info) as source,
            target.open("xb") as output,
        ):
            while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        target.unlink(missing_ok=True)
        raise PayloadError(f"Could not extract the pinned streaming DLL: {exc}") from exc
    if size != STREAMING_DLL_SIZE or digest.hexdigest() != STREAMING_DLL_SHA256:
        target.unlink(missing_ok=True)
        raise PayloadError("Pinned streaming DLL failed member size or SHA-256 verification")


def _copy_file(source: Path, destination: Path) -> None:
    _require_regular_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        if destination.is_file() and not destination.is_symlink() and _hash_file(source) == _hash_file(destination):
            return
        raise PayloadError(f"Two inputs would write different content to {destination}")
    shutil.copyfile(source, destination)


def place_whistle_assets(
    *,
    engine: Path,
    model: Path,
    engine_license: Path,
    model_license: Path,
    payload_root: Path,
) -> None:
    _copy_file(engine, payload_root / "models/windows-x86_64/needle.exe")
    _copy_file(model, payload_root / "models/whistle.cact")
    _copy_file(engine_license, payload_root / "licenses/needle-LICENSE")
    _copy_file(model_license, payload_root / "licenses/whistle-LICENSE")


def copy_release_license_material(
    *,
    payload_root: Path,
    packaging_windows: Path,
    asset_cache: Path,
    optional_asset_specs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    copied_assets: dict[str, dict[str, Any]] = {}
    notices = packaging_windows / "THIRD-PARTY-NOTICES.txt"
    if notices.exists():
        _copy_file(notices, payload_root / "THIRD-PARTY-NOTICES.txt")

    source_materials = packaging_windows / "source-materials.json"
    if not source_materials.is_file():
        raise PayloadError(f"Missing source provenance metadata: {source_materials}")
    _copy_file(source_materials, payload_root / "licenses/source-materials.json")
    try:
        source_metadata = json.loads(source_materials.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadError(f"Invalid source-materials.json: {exc}") from exc
    if not isinstance(source_metadata, dict):
        raise PayloadError("source-materials.json must contain an object")

    gpl_candidates = [
        item
        for item in asset_cache.iterdir()
        if item.name.casefold() == "gpl-3.0.txt"
    ] if asset_cache.is_dir() else []
    if len(gpl_candidates) > 1:
        raise PayloadError("Asset cache contains multiple GPL-3.0.txt candidates")
    if gpl_candidates:
        source = gpl_candidates[0]
        spec = None
        if optional_asset_specs:
            spec = next(
                (
                    value
                    for value in optional_asset_specs.values()
                    if value["filename"].casefold() == source.name.casefold()
                ),
                None,
            )
        if spec is None:
            source_spec = source_metadata.get("ffmpeg_gpl_license")
            if isinstance(source_spec, dict):
                spec = _validate_asset_spec("ffmpeg_gpl_license", source_spec)
        record = verify_asset_file(source, spec) if spec else _file_record(source)
        _copy_file(source, payload_root / "licenses/GPL-3.0.txt")
        copied_assets["gpl_license"] = record
    return copied_assets


def validate_output_dir(out_dir: Path, repo_root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(out_dir)))
    _reject_symlink_components(candidate)
    # The output path has already been checked component-by-component for
    # symlinks. Lexical absolute paths avoid requiring traversal permissions
    # on a not-yet-created output or a synthetic test repository.
    resolved_repo = Path(os.path.abspath(os.fspath(repo_root)))
    resolved_candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        common = os.path.commonpath((os.path.normcase(resolved_repo), os.path.normcase(resolved_candidate)))
    except ValueError:
        common = ""
    if common == os.path.normcase(resolved_repo):
        raise PayloadError(f"Output directory must be outside the repository: {candidate}")
    if os.path.lexists(candidate):
        if not candidate.is_dir():
            raise PayloadError(f"Output path must be a directory: {candidate}")
        if any(candidate.iterdir()):
            raise PayloadError(f"Output directory must be empty: {candidate}")
    return candidate


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if os.path.lexists(cursor) and (cursor.is_symlink() or _is_junction(cursor)):
            raise PayloadError(f"Refusing symlink or junction path component: {cursor}")


def validate_builder_python(
    *,
    version: tuple[int, int] | None = None,
    machine: str | None = None,
    pointer_bits: int | None = None,
    system: str | None = None,
) -> None:
    version = version or sys.version_info[:2]
    machine = (machine or platform.machine()).lower()
    pointer_bits = pointer_bits or (8 * __import__("struct").calcsize("P"))
    system = system or platform.system()
    if version != (3, 13):
        raise PayloadError(
            f"Builder Python must be 3.13.x for cp313 wheels; found {version[0]}.{version[1]}"
        )
    if system != "Windows" or machine not in {"amd64", "x86_64", "x64"} or pointer_bits != 64:
        raise PayloadError(
            f"Builder Python must run on Windows x86_64 (64-bit); found {system}/{machine}/{pointer_bits}-bit"
        )


def _project_version(repo_root: Path) -> str:
    import tomllib

    try:
        with (repo_root / "pyproject.toml").open("rb") as stream:
            metadata = tomllib.load(stream)
        version = metadata["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise PayloadError(f"Cannot read project version from pyproject.toml: {exc}") from exc
    if not isinstance(version, str):
        raise PayloadError("Project version in pyproject.toml must be a string")
    return version


def _wheel_project_metadata(wheel_path: Path) -> tuple[str, str]:
    _require_regular_file(wheel_path)
    members = _safe_zip_members(wheel_path)
    metadata_members = [
        member
        for member in members
        if member.relative_path.name == "METADATA"
        and member.relative_path.parent.name.endswith(".dist-info")
    ]
    if len(metadata_members) != 1:
        raise PayloadError(f"Wheel must contain one dist-info/METADATA: {wheel_path.name}")
    with zipfile.ZipFile(wheel_path) as archive:
        message = email.parser.Parser().parsestr(
            archive.read(metadata_members[0].info).decode("utf-8", errors="replace")
        )
    name, version = message.get("Name"), message.get("Version")
    if not name or not version:
        raise PayloadError(f"Wheel metadata lacks Name or Version: {wheel_path.name}")
    return name, version


def _find_local_wheel(wheel_dir: Path, expected_version: str) -> Path:
    if not wheel_dir.is_dir():
        raise PayloadError(f"Wheel directory does not exist: {wheel_dir}")
    matches: list[Path] = []
    for path in wheel_dir.iterdir():
        if path.suffix.lower() != ".whl":
            continue
        name, version = _wheel_project_metadata(path)
        if name.casefold().replace("_", "-") == "textflowkit":
            if version != expected_version:
                raise PayloadError(
                    f"Local TextFlowKit wheel version {version} does not match project {expected_version}"
                )
            matches.append(path)
    if len(matches) != 1:
        raise PayloadError(
            f"Expected exactly one TextFlowKit wheel in {wheel_dir}, found {len(matches)}"
        )
    return matches[0].resolve(strict=True)


def _build_local_wheel(repo_root: Path, wheel_dir: Path) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--wheel-dir",
        str(wheel_dir),
        str(repo_root),
    ]
    subprocess.run(command, check=True, cwd=repo_root)
    return _find_local_wheel(wheel_dir, _project_version(repo_root))


def _install_runtime_dependencies(
    wheel: Path,
    site_packages: Path,
    dependency_wheels: Path | None = None,
) -> dict[str, str]:
    site_packages.mkdir(parents=True, exist_ok=True)
    requirement = f"textflowkit[http,export,streaming] @ {wheel.as_uri()}"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--only-binary=:all:",
        "--no-compile",
        "--disable-pip-version-check",
    ]
    if dependency_wheels is not None:
        if not dependency_wheels.is_dir():
            raise PayloadError(f"Dependency wheel directory does not exist: {dependency_wheels}")
        command.extend(["--no-index", "--find-links", str(dependency_wheels)])
    command.extend(["--no-input", requirement])
    subprocess.run(command, check=True)

    dependencies: dict[str, str] = {}
    forbidden: list[str] = []
    for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            continue
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        dependencies[canonical] = version
        if (
            canonical.startswith(("torch", "pyannote"))
            or canonical in {"whisper", "openai-whisper"}
        ):
            forbidden.append(canonical)
    if forbidden:
        raise PayloadError(
            "Forbidden ML packages were installed in the payload: "
            + ", ".join(sorted(forbidden))
        )
    required_runtime = {"textflowkit", "websockets"}
    missing = sorted(required_runtime - dependencies.keys())
    if missing:
        raise PayloadError("Runtime dependency installation is missing: " + ", ".join(missing))
    return dict(sorted(dependencies.items()))


def _copy_optional_streaming_dll(
    paths: dict[str, Path],
    payload_root: Path,
) -> None:
    wheel = paths.get("streaming")
    if wheel is not None:
        prepare_streaming_dll(wheel, payload_root)


def _payload_file_records(payload_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(payload_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink() or _is_junction(path):
            raise PayloadError(f"Payload must not contain symlinks or junctions: {path}")
        if path.is_dir():
            continue
        _require_regular_file(path)
        relative = path.relative_to(payload_root).as_posix()
        if relative.casefold() == "manifest.json":
            continue
        records.append(
            {
                "path": relative,
                "sha256": _hash_file(path),
                "size": path.stat().st_size,
            }
        )
    return records


def build_manifest(
    *,
    payload_root: Path,
    version: str,
    python_version: str,
    assets: dict[str, dict[str, Any]],
    dependencies: dict[str, str],
    created_utc: str | None = None,
) -> dict[str, Any]:
    return {
        "version": version,
        "platform": PLATFORM,
        "python_version": python_version,
        "assets": {key: assets[key] for key in sorted(assets)},
        "dependencies": {key: dependencies[key] for key in sorted(dependencies)},
        "files": _payload_file_records(payload_root),
        "created_utc": created_utc
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _assemble_payload(
    *,
    payload_root: Path,
    lock: dict[str, Any],
    asset_paths: dict[str, Path],
    asset_records: dict[str, dict[str, Any]],
    wheel: Path,
    dependency_wheels: Path | None,
    repo_root: Path,
) -> None:
    payload_root.mkdir(parents=True, exist_ok=True)
    prepare_python_runtime(asset_paths["python"], payload_root / "runtime")
    (payload_root / "bin").mkdir(parents=True, exist_ok=True)
    prepare_node_archive(asset_paths["node"], payload_root)
    prepare_ffmpeg_archive(asset_paths["ffmpeg"], payload_root)
    place_whistle_assets(
        engine=asset_paths["engine"],
        model=asset_paths["model"],
        engine_license=asset_paths["engine_license"],
        model_license=asset_paths["model_license"],
        payload_root=payload_root,
    )
    _copy_optional_streaming_dll(asset_paths, payload_root)

    _copy_file(repo_root / "LICENSE", payload_root / "licenses/TextFlowKit-LICENSE")
    release_assets = copy_release_license_material(
        payload_root=payload_root,
        packaging_windows=repo_root / "packaging/windows",
        asset_cache=next(iter(asset_paths.values())).parent,
        optional_asset_specs={
            key: value
            for key, value in lock["assets"].items()
            if key not in REQUIRED_ASSETS and key not in OPTIONAL_ASSET_KEYS
        },
    )
    # Lock keys beyond the core set are forward-compatible. The known streaming
    # wheel is consumed above; an optional GPL text file is recorded if present.
    asset_records.update(release_assets)
    launch = repo_root / "packaging/windows/launch.py"
    _require_regular_file(launch)
    _copy_file(launch, payload_root / "launch.py")

    dependencies = _install_runtime_dependencies(
        wheel,
        payload_root / "runtime/Lib/site-packages",
        dependency_wheels,
    )
    manifest = build_manifest(
        payload_root=payload_root,
        version=_project_version(repo_root),
        python_version=lock["python_version"],
        assets=asset_records,
        dependencies=dependencies,
    )
    (payload_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def prepare_payload(
    *,
    asset_cache: Path,
    out_dir: Path,
    wheel_dir: Path | None = None,
    dependency_wheels: Path | None = None,
    lock_path: Path = DEFAULT_LOCK,
    repo_root: Path = REPO_ROOT,
) -> Path:
    validate_builder_python()
    lock = load_asset_lock(lock_path)
    output = validate_output_dir(out_dir, repo_root)
    assets_directory = asset_cache.absolute()
    asset_paths, asset_records = obtain_locked_assets(lock, assets_directory)
    version = _project_version(repo_root)

    if wheel_dir is not None:
        wheel = _find_local_wheel(wheel_dir, version)
        scratch_context = None
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        scratch_context = tempfile.TemporaryDirectory(prefix="textflowkit-wheel-")
        scratch = Path(scratch_context.name)
        try:
            _reject_symlink_components(scratch)
            wheel = _build_local_wheel(repo_root, scratch / "wheelhouse")
        except Exception:
            scratch_context.cleanup()
            raise

    output.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(output)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.payload-", dir=output.parent))
    try:
        _assemble_payload(
            payload_root=staging,
            lock=lock,
            asset_paths=asset_paths,
            asset_records=asset_records,
            wheel=wheel,
            dependency_wheels=dependency_wheels,
            repo_root=repo_root,
        )
        if output.exists():
            if not output.is_dir() or any(output.iterdir()):
                raise PayloadError(f"Output directory changed while building: {output}")
            output.rmdir()
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if scratch_context is not None:
            scratch_context.cleanup()
    return output


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-cache", type=Path, required=True, help="Directory containing locked downloads")
    parser.add_argument("--out-dir", type=Path, required=True, help="New or empty external payload root")
    parser.add_argument("--wheel-dir", type=Path, help="Directory containing a built local TextFlowKit wheel")
    parser.add_argument(
        "--dependency-wheels",
        type=Path,
        help="Use only wheels in this directory to install runtime dependencies",
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="Asset lock JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        output = prepare_payload(
            asset_cache=args.asset_cache,
            out_dir=args.out_dir,
            wheel_dir=args.wheel_dir,
            dependency_wheels=args.dependency_wheels,
            lock_path=args.lock,
        )
    except (PayloadError, subprocess.CalledProcessError) as exc:
        print(f"Windows payload preparation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Windows payload prepared: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
