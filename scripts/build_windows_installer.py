"""Validate an external staged Windows payload and compile it with Inno Setup.

No downloads, dependency installation, source sweeping, or end-user setup.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath

SOURCE = Path(__file__).resolve().parents[1]
ISS = SOURCE / "packaging/windows/textflowkit.iss"
BOOTSTRAP = SOURCE / "packaging/windows/launch.py"
REQUIRED = (
    "runtime/python.exe", "runtime/pythonw.exe", "runtime/python313.dll",
    "runtime/python313.zip", "runtime/python313._pth", "bin/ffmpeg.exe",
    "bin/ffprobe.exe", "bin/node.exe", "models/whistle.cact",
    "models/windows-x86_64/needle.exe",
    "models/streaming/libneedle3-2ae11323dc00/libneedle3.dll", "launch.py", "manifest.json",
    "THIRD-PARTY-NOTICES.txt",
    "runtime/Lib/site-packages/textflowkit/ui/launcher.py",
    "runtime/Lib/site-packages/textflowkit/cli.py",
    "runtime/Lib/site-packages/textflowkit/ui/static/index.html",
)
DEPENDENCIES = {
    "textflowkit", "yt-dlp", "fastapi", "uvicorn", "pydantic", "websockets",
    "python-docx", "reportlab", "textflowkit-fonts",
}


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_build_paths(payload: Path, out: Path) -> None:
    payload, out = payload.resolve(), out.resolve()
    for path in (payload, out):
        if path == SOURCE or SOURCE in path.parents or path in SOURCE.parents:
            raise ValueError("Payload and output directories must be outside the source checkout")
    if payload == out or payload in out.parents or out in payload.parents:
        raise ValueError("Payload and output directories must not overlap")


def validate_payload(payload: Path) -> dict:
    payload = payload.resolve()
    for name in REQUIRED:
        if not (payload / name).is_file() or not (payload / name).stat().st_size:
            raise ValueError(f"Missing or empty payload component: {name}")
    if not any(p.is_file() for p in (payload / "licenses").rglob("*")):
        raise ValueError("Payload licenses are missing")
    allowed = {"runtime", "bin", "models", "licenses", "launch.py", "manifest.json",
               "THIRD-PARTY-NOTICES.txt"}
    if any(p.name not in allowed for p in payload.iterdir()):
        raise ValueError("Payload contains unexpected entry outside the installer allowlist")
    value = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Manifest must be an object")  # noqa: TRY004 - invalid JSON input
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)", str(value.get("version", ""))):
        raise ValueError("Invalid manifest application version")
    if value.get("python_version") != "3.13.13" or value.get("platform") != "windows-x86_64":
        raise ValueError("Payload must use CPython 3.13.13 embedded Windows x64")
    if not isinstance(value.get("assets"), dict) or not value["assets"]:
        raise ValueError("Manifest asset provenance is missing")
    declared = value.get("dependencies")
    if not isinstance(declared, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in declared.items()
    ):
        raise ValueError("Manifest dependencies must map names to exact versions")
    declared = {normalized(k): v for k, v in declared.items()}
    if not DEPENDENCIES <= declared.keys() or declared["textflowkit"] != value["version"]:
        raise ValueError("Manifest dependencies are incomplete or application version mismatches")
    site = payload / "runtime/Lib/site-packages"
    installed = {normalized(d.metadata["Name"]): d.version
                 for d in metadata.distributions(path=[str(site)])}
    if installed != declared:
        raise ValueError("Installed dependency versions do not match the manifest")
    if "torch" in installed or (site / "torch").exists():
        raise ValueError("The desktop payload must not include Torch")
    pth = (payload / "runtime/python313._pth").read_text(encoding="utf-8-sig")
    entries = [line.strip() for line in pth.splitlines() if line.strip()]
    if entries != ["python313.zip", ".", "Lib/site-packages"]:
        raise ValueError("Embedded runtime path isolation is invalid")
    if (payload / "launch.py").read_bytes() != BOOTSTRAP.read_bytes():
        raise ValueError("Payload bootstrap differs from the installer bootstrap")
    records = value.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("Manifest file inventory is missing")
    listed = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("Invalid manifest file entry")  # noqa: TRY004 - invalid JSON input
        name = record["path"]
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
                or relative.as_posix() != name or name == "manifest.json"
                or name.casefold() in listed):
            raise ValueError(f"Unsafe or duplicate manifest path: {name}")
        listed.add(name.casefold())
        path = payload / name
        if not path.is_file() or path.is_symlink() or payload not in path.resolve().parents:
            raise ValueError(f"Missing or unsafe manifest file: {name}")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if path.stat().st_size != record.get("size") or digest != record.get("sha256"):
            raise ValueError(f"Payload integrity check failed: {name}")
    actual = set()
    for path in payload.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Payload cannot include symbolic links or junctions")
        if path.is_file() and path != payload / "manifest.json":
            actual.add(path.relative_to(payload).as_posix().casefold())
    if actual != listed:
        raise ValueError("Payload contains unlisted files")
    return value


def find_iscc() -> Path | None:
    found = shutil.which("ISCC.exe")
    candidates = [Path(found)] if found else []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.extend([Path(base) / "Inno Setup 6/ISCC.exe",
                               Path(base) / "Programs/Inno Setup 6/ISCC.exe"])
    return next((p for p in candidates if p.is_file()), None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--iscc", type=Path, help="Inno Setup compiler (otherwise search PATH/install locations)")
    args = parser.parse_args(argv)
    try:
        payload, out = args.payload_dir.resolve(), args.out_dir.resolve()
        validate_build_paths(payload, out)
        value = validate_payload(payload)
        compiler = args.iscc.resolve() if args.iscc else find_iscc()
        if compiler is None or not compiler.is_file():
            raise ValueError("Inno Setup 6 ISCC.exe not found; supply --iscc")
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(compiler), f"/DPayloadDir={payload}",
                        f"/DAppVersion={value['version']}", f"/O{out}", str(ISS)], check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installer build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
