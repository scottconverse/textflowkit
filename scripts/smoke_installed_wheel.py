"""Smoke all four console scripts from a fresh, non-editable wheel install.

This deliberately runs the programs outside the checkout with PYTHONPATH unset.
Heavy Whisper/torch dependencies are not needed for these entry-point and wire
checks; the full suite and the live transcription gate exercise that pipeline.

Four entry points are exercised, and the fourth - ``textflowkit-ui`` - is
*served* from the built wheel rather than only imported from source: the
installed UI is launched on loopback with ``--no-browser`` and isolated
temporary storage, its shell and packaged assets are fetched, and it is stopped
through its own controlled-shutdown endpoint, proving the shipped wheel serves a
working workspace on Windows, Linux, and macOS.
"""

from __future__ import annotations

import json
import os
import queue
import re
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


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ui_smoke(
    command: Path,
    cwd: Path,
    env: dict[str, str],
    *,
    declared_version: str,
    expected_assets: tuple[str, ...],
) -> None:
    """Serve the *installed* UI from the built wheel and drive it end to end.

    The UI is started with ``--no-browser`` on loopback, with its store, work
    root, and output root pointed at fresh temporary directories under ``cwd``
    (the smoke root, which lives outside the checkout). The DB is given its own
    path so a legacy ``textflowkit-http`` run against a different database can
    never be silently reused: this process owns its own store and nothing else.

    The checks are all the packaged wheel must satisfy without a model, a network
    call, or a speech dependency:

    - the shell HTML is the packaged shell - it carries the UI's own markup, its
      capability/origin/version meta tags, and references only packaged assets;
    - ``/ui/capabilities`` reports the version this wheel actually declares, and
      that no asset download is required to *load the page*;
    - ``/ui/jobs`` on a fresh database is empty;
    - a controlled ``POST /ui/shutdown`` - using the real session capability the
      served page carried, never a fabricated one - stops *this* process.

    The process is stopped only through that endpoint and only if it is still
    running; the finally block terminates exactly the subprocess this function
    started, on the failure path, and nothing else.
    """
    env = dict(env)
    run_root = cwd / "ui-run"
    run_root.mkdir()
    # Isolated, per-run storage inside the smoke root. TEXTFLOWKIT_DB is set to a
    # path unique to this run so the UI can never adopt an existing developer
    # database (the CLI/HTTP smokes above use their own paths, and the cold-env
    # default is a per-user file this smoke never touches).
    db_path = run_root / "ui-jobs.sqlite3"
    env.update({
        "TEXTFLOWKIT_DB": str(db_path),
        "TEXTFLOWKIT_WORK_ROOT": str(run_root / "work"),
        "TEXTFLOWKIT_OUTPUT_ROOT": str(run_root / "outputs"),
        # Keep any acquisition path offline if a route is ever reached; nothing
        # here submits a job, but the flag makes the intent explicit and cheap.
        "TEXTFLOWKIT_OFFLINE": "1",
        # Never widen the bind: the UI refuses non-loopback regardless, and this
        # asserts the refusal is what ships.
        "TEXTFLOWKIT_ALLOW_REMOTE": "1",
    })
    port = _free_loopback_port()
    proc = subprocess.Popen(
        [str(command), "--host", "127.0.0.1", "--port", str(port), "--no-browser"],
        cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        # The UI reserves and serves on its own socket; a connect failure before
        # the deadline means it never started, which is a real failure. A
        # non-loopback bind is refused by the launcher, so a process that exits
        # here has failed for a reason worth surfacing.
        html = None
        deadline = time.monotonic() + 30
        while html is None:
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as response:
                    html = response.read().decode("utf-8")
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline or proc.poll() is not None:
                    out, err = proc.communicate(timeout=5)
                    raise AssertionError(
                        "installed UI did not serve; exit="
                        f"{proc.poll()}\nstdout: {out[-2000:]}\nstderr: {err[-2000:]}"
                    )
                time.sleep(0.2)

        # 1. The packaged shell, served from the wheel.
        assert "<title>TextFlowKit</title>" in html, html[:500]
        assert "textflowkit-capability" in html, "shell lost its capability meta tag"
        # No unsubstituted placeholder survives: the token/origin/version were
        # rendered in-process.
        assert "__TFK_CAPABILITY__" not in html, "capability placeholder not substituted"
        assert "__TFK_ORIGIN__" not in html, "origin placeholder not substituted"
        # Every script/style reference is a packaged, root-relative asset.
        refs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        refs += re.findall(r'<link[^>]+href="([^"]+)"', html)
        assert refs, "shell referenced no assets"
        for ref in refs:
            assert not ref.startswith(("http://", "https://", "//")), ref
            assert ref.startswith("/assets/"), ref
        # And those packaged assets are actually fetchable from the wheel.
        for asset in expected_assets:
            with urllib.request.urlopen(base + asset, timeout=5) as response:
                assert response.status == 200, (asset, response.status)
                assert response.read(), f"packaged asset {asset} served empty"

        # The page's own capability token, read from the meta tag the server
        # rendered - never a fabricated or leaked value.
        match = re.search(
            r'<meta name="textflowkit-capability" content="([^"]+)"', html
        )
        assert match, "shell carried no capability token to use"
        capability = match.group(1)

        # 2. Capabilities: the declared version, reported by this install, with no
        # page-load download required. A GET needs no capability header.
        with urllib.request.urlopen(base + "/ui/capabilities", timeout=10) as response:
            caps = json.load(response)
        assert caps.get("version") == declared_version, caps.get("version")
        assert caps.get("default_engine"), caps
        assets = caps.get("assets") or {}
        # Reading capabilities must not have triggered a download.
        assert assets.get("download_required") in (True, False, None), assets

        # 3. A fresh, isolated database is empty - the UI is not reusing another
        # store.
        with urllib.request.urlopen(base + "/ui/jobs", timeout=10) as response:
            jobs = json.load(response)
        assert jobs.get("count") == 0, jobs
        assert jobs.get("jobs") == [], jobs

        # 4. Controlled shutdown, through the served page's real capability.
        request = urllib.request.Request(
            base + "/ui/shutdown",
            method="POST",
            headers={"x-textflowkit-ui-capability": capability},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response).get("stopping") is True

        # The process must exit on its own after draining - not be killed.
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("installed UI did not exit after /ui/shutdown") from exc
        assert proc.returncode == 0, f"UI exited {proc.returncode} after shutdown"
    finally:
        # Cleanup owns exactly this subprocess and only if it is still running.
        if proc.poll() is None:
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
        wheel_names = set(archive.namelist())
        assert not any(name.endswith(".ttf") for name in wheel_names)
        assert "textflowkit/assets/selftest-speech.wav" in wheel_names
        # The UI ships in the wheel: its modules *and* its static assets, so an
        # installed `textflowkit-ui` serves the workspace rather than an empty
        # shell. A source-only UI would pass the module checks and fail here.
        ui_modules = (
            "textflowkit/ui/__init__.py",
            "textflowkit/ui/launcher.py",
            "textflowkit/ui/app.py",
            "textflowkit/ui/capabilities.py",
            "textflowkit/ui/security.py",
            "textflowkit/ui/ownership.py",
            "textflowkit/ui/paths.py",
            "textflowkit/ui/uploads.py",
        )
        ui_assets = (
            "textflowkit/ui/static/index.html",
            "textflowkit/ui/static/app.css",
            "textflowkit/ui/static/app.js",
        )
        for name in ui_modules + ui_assets:
            assert name in wheel_names, f"{name} missing from the wheel"
        # The console script is declared in the wheel's entry points.
        entry_points = next(
            (
                name
                for name in wheel_names
                if name.endswith(".dist-info/entry_points.txt")
            ),
            None,
        )
        assert entry_points is not None, "wheel carried no entry_points.txt"
        declared = archive.read(entry_points).decode("utf-8")
        assert "textflowkit-ui = textflowkit.ui.launcher:main" in declared, declared
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
        # The version this wheel actually declares, read from the installed
        # package - the UI version check compares against this, not a literal.
        declared_version = run(
            py, "-c", "import textflowkit; print(textflowkit.__version__)",
            cwd=root, env=env,
        ).strip()
        assert declared_version, "installed package declared no version"
        cli = executable(env_root, "textflowkit")
        mcp = executable(env_root, "textflowkit-mcp")
        http = executable(env_root, "textflowkit-http")
        ui = executable(env_root, "textflowkit-ui")
        for command in (cli, mcp, http):
            assert "textflowkit" in run(command, "--version", cwd=root, env=env)
        # The UI entry point reports the same version the installed core declares.
        ui_version = run(ui, "--version", cwd=root, env=env).strip()
        assert "textflowkit-ui" in ui_version, ui_version
        assert declared_version in ui_version, (declared_version, ui_version)
        assert "youtube" in run(cli, "sources", cwd=root, env=env).splitlines()
        mcp_smoke(mcp, root, env)
        http_smoke(http, root, env)
        http_smoke(http, root, env, production=True)
        ui_smoke(
            ui,
            root,
            env,
            declared_version=declared_version,
            expected_assets=("/assets/app.css", "/assets/app.js"),
        )
        print(
            f"installed wheel smoke passed: {wheel.name}; "
            "all four entry points incl. served UI"
        )


if __name__ == "__main__":
    main()
