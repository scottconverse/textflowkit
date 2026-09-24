"""Smoke all three console scripts from a fresh, non-editable wheel install.

This deliberately runs the programs outside the checkout with PYTHONPATH unset.
Heavy Whisper/torch dependencies are not needed for these entry-point and wire
checks; the full suite and the live transcription gate exercise that pipeline.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path


def executable(root: Path, name: str) -> Path:
    if os.name == "nt":
        return root / "Scripts" / (name + ".exe")
    return root / "bin" / name


def python(root: Path) -> Path:
    return executable(root, "python")


def run(*args: str | Path, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env,
        text=True, capture_output=True, timeout=120, check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"{args[0]} returned {result.returncode}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return result.stdout


def mcp_smoke(command: Path, cwd: Path, env: dict[str, str]) -> None:
    proc = subprocess.Popen(
        [str(command)], cwd=cwd, env=env, text=True, encoding="utf-8",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=1,
    )
    responses: queue.Queue[str] = queue.Queue()
    assert proc.stdout and proc.stdin
    threading.Thread(
        target=lambda: [responses.put(line) for line in proc.stdout], daemon=True
    ).start()

    def call(method: str, params: dict | None, id_: int) -> dict:
        frame: dict = {"jsonrpc": "2.0", "method": method, "id": id_}
        if params is not None:
            frame["params"] = params
        proc.stdin.write(json.dumps(frame) + "\n")
        proc.stdin.flush()
        while True:
            try:
                payload = json.loads(responses.get(timeout=30))
            except queue.Empty as exc:
                raise AssertionError(f"MCP {method} timed out; exit={proc.poll()}") from exc
            if payload.get("id") == id_:
                return payload

    try:
        init = call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "wheel-smoke", "version": "1"},
        }, 1)
        assert init.get("result", {}).get("serverInfo"), init
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }) + "\n")
        proc.stdin.flush()
        tools = call("tools/list", None, 2)
        names = {item["name"] for item in tools["result"]["tools"]}
        assert {"list_sources", "submit_batch_media", "resume_job"} <= names, names
        sources = call("tools/call", {"name": "list_sources", "arguments": {}}, 3)
        assert sources["result"]["content"], sources
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)


def http_smoke(command: Path, cwd: Path, env: dict[str, str], *, production: bool = False) -> None:
    env = dict(env)
    headers: dict[str, str] = {}
    if production:
        input_root = cwd / "input"
        input_root.mkdir()
        env.update({
            "TEXTFLOWKIT_PROFILE": "production",
            "TEXTFLOWKIT_API_TOKEN": "wheel-smoke-secret-12345",
            "TEXTFLOWKIT_INPUT_ROOT": str(input_root),
            "TEXTFLOWKIT_WORK_ROOT": str(cwd / "work"),
            "TEXTFLOWKIT_DB": str(cwd / "jobs.db"),
        })
        headers["Authorization"] = "Bearer wheel-smoke-secret-12345"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [str(command), "--port", str(port)], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                request = urllib.request.Request(base + "/health", headers=headers)
                with urllib.request.urlopen(request, timeout=2) as response:
                    health = json.load(response)
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline or proc.poll() is not None:
                    raise AssertionError(f"HTTP entry point failed; exit={proc.poll()}")
                time.sleep(0.2)
        assert health["status"] == "ok", health
        if production:
            try:
                urllib.request.urlopen(base + "/health", timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401, exc.code
            else:
                raise AssertionError("production HTTP accepted an unauthenticated request")
        request = urllib.request.Request(base + "/sources", headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            sources = json.load(response)
        assert "youtube" in sources["platforms"], sources
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python scripts/smoke_installed_wheel.py MAIN_WHEEL FONTS_WHEEL")
    wheel = Path(sys.argv[1]).resolve(strict=True)
    font_wheel = Path(sys.argv[2]).resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.endswith(".ttf") for name in archive.namelist())
        assert "textflowkit/assets/selftest-speech.wav" in archive.namelist()
    with zipfile.ZipFile(font_wheel) as archive:
        assert all(
            f"textflowkit_fonts/fonts/{name}.ttf" in archive.namelist()
            for name in ("NotoSans", "NotoSansArabic", "NotoSansSC")
        )
        assert any("OFL-NotoSans.txt" in name for name in archive.namelist())
    with tempfile.TemporaryDirectory(prefix="tfk-wheel-smoke-") as directory:
        root = Path(directory)
        env_root = root / "venv"
        venv.EnvBuilder(with_pip=True).create(env_root)
        py = python(env_root)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["TEXTFLOWKIT_PROFILE"] = "developer"
        env.pop("TEXTFLOWKIT_DB", None)
        env["TEXTFLOWKIT_OUTPUT_ROOT"] = str(root / "outputs")
        run(py, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps",
            wheel, font_wheel, cwd=root, env=env)
        run(py, "-m", "pip", "install", "--disable-pip-version-check",
            "mcp>=2.0", "fastapi>=0.115", "uvicorn>=0.30", "pydantic>=2.7",
            "reportlab>=4.0", "pypdf>=5",
            cwd=root, env=env)
        installed_path = run(py, "-c", "import textflowkit; print(textflowkit.__file__)",
                             cwd=root, env=env).strip()
        assert str(root).casefold() in installed_path.casefold(), installed_path
        speech_fixture = run(
            py, "-c",
            "from importlib.resources import files; "
            "p=files('textflowkit').joinpath('assets/selftest-speech.wav'); "
            "assert p.read_bytes().startswith(b'RIFF'); print('speech fixture present')",
            cwd=root, env=env,
        )
        assert "speech fixture present" in speech_fixture
        pdf_smoke = run(
            py, "-c",
            "from textflowkit import Segment, Transcript; "
            "from textflowkit.render import render_bytes; "
            "from pypdf import PdfReader; import io; "
            "blob=render_bytes(Transcript(source='wheel',segments=[Segment(0,1,'Hello 你好')]),'pdf'); "
            "assert '你好' in PdfReader(io.BytesIO(blob)).pages[0].extract_text(); "
            "print('PDF export passed')",
            cwd=root, env=env,
        )
        assert "PDF export passed" in pdf_smoke
        cli = executable(env_root, "textflowkit")
        mcp = executable(env_root, "textflowkit-mcp")
        http = executable(env_root, "textflowkit-http")
        for command in (cli, mcp, http):
            assert "textflowkit" in run(command, "--version", cwd=root, env=env)
        assert "youtube" in run(cli, "sources", cwd=root, env=env).splitlines()
        mcp_smoke(mcp, root, env)
        http_smoke(http, root, env)
        http_smoke(http, root, env, production=True)
        print(f"installed wheel smoke passed: {wheel.name}; all three entry points")


if __name__ == "__main__":
    main()
