"""Installer contracts; fixtures are staged bytes, never downloads or installs."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build_windows_installer.py"
BOOTSTRAP = ROOT / "packaging/windows/launch.py"
ISS = ROOT / "packaging/windows/textflowkit.iss"
PTH = "python313.zip\n.\nLib/site-packages\n"
DEPENDENCIES = {
    "textflowkit": "0.1.11", "yt-dlp": "2026.10.14", "fastapi": "0.115.0",
    "uvicorn": "0.30.0", "pydantic": "2.7.0", "websockets": "15.0.1",
    "python-docx": "1.1.0", "reportlab": "4.0.0", "textflowkit-fonts": "0.1.5",
}


def load(path):
    assert path.is_file(), f"installer implementation missing: {path.name}"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(payload):
    files = []
    for path in payload.rglob("*"):
        if path.is_file() and path != payload / "manifest.json":
            data = path.read_bytes()
            files.append({"path": path.relative_to(payload).as_posix(),
                          "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    value = {
        "version": "0.1.11", "platform": "windows-x86_64", "python_version": "3.13.13",
        "assets": {"fixture": "fixture"}, "dependencies": DEPENDENCIES, "files": files,
    }
    (payload / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "payload"
    paths = [
        "runtime/python.exe", "runtime/pythonw.exe", "runtime/python313.dll",
        "runtime/python313.zip", "bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/node.exe",
        "models/whistle.cact", "models/windows-x86_64/needle.exe",
        "models/streaming/libneedle3-2ae11323dc00/libneedle3.dll", "licenses/NOTICE.txt",
        "runtime/Lib/site-packages/textflowkit/ui/static/index.html",
        "runtime/Lib/site-packages/textflowkit/ui/launcher.py",
        "runtime/Lib/site-packages/textflowkit/cli.py",
        "THIRD-PARTY-NOTICES.txt",
    ]
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (root / "runtime/python313._pth").write_text(PTH, encoding="utf-8")
    # Tests can reach RED before bootstrap implementation exists.
    (root / "launch.py").write_bytes(BOOTSTRAP.read_bytes() if BOOTSTRAP.exists() else b"")
    for name, version in DEPENDENCIES.items():
        directory = root / "runtime/Lib/site-packages" / f"{name.replace('-', '_')}-{version}.dist-info"
        directory.mkdir()
        (directory / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
        )
    manifest(root)
    return root


def test_valid_payload_and_compile_command(payload, tmp_path, monkeypatch):
    build = load(BUILD)
    compiler = tmp_path / "compiler/ISCC.exe"
    compiler.parent.mkdir()
    compiler.write_bytes(b"compiler")
    calls = []
    monkeypatch.setattr(build.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    out = tmp_path / "installer output"
    assert build.main(["--payload-dir", str(payload), "--iscc", str(compiler),
                       "--out-dir", str(out)]) == 0
    args, kwargs = calls[0]
    assert args[0] == str(compiler)
    assert f"/DPayloadDir={payload}" in args
    assert "/DAppVersion=0.1.11" in args
    assert f"/O{out}" in args
    assert args[-1] == str(ISS)
    assert kwargs.get("check") is True
    assert not kwargs.get("shell", False)


def test_runtime_site_initialization_is_refused(payload):
    build = load(BUILD)
    (payload / "runtime/python313._pth").write_text(PTH + "import site\n", encoding="utf-8")
    manifest(payload)
    with pytest.raises(ValueError, match="isolation"):
        build.validate_payload(payload)


def test_payload_hashing_without_python311_file_digest(payload, monkeypatch):
    build = load(BUILD)
    target = payload / "bin/node.exe"
    target.write_bytes(b"verified bytes" * 100000 + b"last byte")
    expected = manifest(payload)
    monkeypatch.delattr(hashlib, "file_digest", raising=False)
    try:
        actual = build.validate_payload(payload)
    except AttributeError as exc:
        pytest.fail(f"Validator requires a Python 3.11-only API: {exc}")
    assert actual == expected
    with target.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"!")
    with pytest.raises(ValueError, match="integrity"):
        build.validate_payload(payload)


@pytest.mark.parametrize("missing", [
    "runtime/pythonw.exe", "bin/ffprobe.exe", "bin/node.exe", "models/whistle.cact",
    "models/windows-x86_64/needle.exe",
    "models/streaming/libneedle3-2ae11323dc00/libneedle3.dll", "manifest.json",
])
def test_missing_payload_component_refused(payload, missing):
    build = load(BUILD)
    (payload / missing).unlink()
    with pytest.raises(ValueError):
        build.validate_payload(payload)


def test_tamper_and_unlisted_file_refused(payload):
    build = load(BUILD)
    target = payload / "bin/node.exe"
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        build.validate_payload(payload)
    manifest(payload)
    (payload / "private.env").write_text("not shipped", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected|unlisted"):
        build.validate_payload(payload)


@pytest.mark.parametrize("change", ["pth", "torch", "version", "dependency", "traversal"])
def test_invalid_runtime_or_manifest_refused(payload, change):
    build = load(BUILD)
    if change == "pth":
        (payload / "runtime/python313._pth").write_text(PTH + "../external\n")
    if change == "torch":
        (payload / "runtime/Lib/site-packages/torch").mkdir()
    value = manifest(payload)
    if change == "version":
        value["version"] = '0.1.11"\nEvil=yes'
    if change == "dependency":
        value["dependencies"] = {"textflowkit": "0.1.11"}
    if change == "traversal":
        value["files"].append({"path": "../outside", "size": 1, "sha256": "0" * 64})
    (payload / "manifest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        build.validate_payload(payload)


def test_build_paths_must_be_outside_checkout(payload, tmp_path):
    build = load(BUILD)
    with pytest.raises(ValueError, match="outside"):
        build.validate_build_paths(ROOT / "payload", tmp_path / "out")
    with pytest.raises(ValueError, match="outside"):
        build.validate_build_paths(payload, ROOT / "dist")
    with pytest.raises(ValueError, match="overlap"):
        build.validate_build_paths(payload, payload / "out")


def test_bootstrap_environment_is_process_local_and_preserves_overrides(tmp_path):
    bootstrap = load(BOOTSTRAP)
    env = {"PATH": "original", "PYTHONPATH": "user-package", "PYTHONHOME": "user-python",
           "TEXTFLOWKIT_MODELS_DIR": "custom-models", "TEXTFLOWKIT_OFFLINE": "0"}
    bootstrap.configure_environment(tmp_path, env)
    assert env["PATH"] == str(tmp_path / "bin") + os.pathsep + "original"
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["TEXTFLOWKIT_MODELS_DIR"] == "custom-models"
    assert env["TEXTFLOWKIT_OFFLINE"] == "0"
    blank = {}
    bootstrap.configure_environment(tmp_path, blank)
    assert blank["TEXTFLOWKIT_MODELS_DIR"] == str(tmp_path / "models")
    assert blank["TEXTFLOWKIT_OFFLINE"] == "1"


def test_bootstrap_mutex_wraps_launcher_lifetime(monkeypatch):
    bootstrap = load(BOOTSTRAP)
    events = []
    class Mutex:
        def __enter__(self):
            events.append("held")
        def __exit__(self, *args):
            events.append("released")
    monkeypatch.setattr(bootstrap, "app_mutex", lambda: Mutex())
    monkeypatch.setattr(bootstrap, "configure_environment", lambda *a: None)
    def launch(args):
        assert events == ["held"]
        assert args == ["--no-browser"]
        events.append("launcher")
        return 0
    monkeypatch.setitem(sys.modules, "textflowkit.ui.launcher", SimpleNamespace(main=launch))
    assert bootstrap.main(["--no-browser"]) == 0
    assert events == ["held", "launcher", "released"]


def test_bootstrap_disables_bytecode_before_product_import(monkeypatch):
    import builtins
    from contextlib import nullcontext

    bootstrap = load(BOOTSTRAP)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(bootstrap, "configure_environment", lambda *a: None)
    monkeypatch.setattr(bootstrap, "app_mutex", nullcontext)
    original_import = builtins.__import__
    observed = []
    def import_product(name, *args, **kwargs):
        if name == "textflowkit.ui.launcher":
            observed.append(sys.dont_write_bytecode)
            return SimpleNamespace(main=lambda argv: 0)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_product)
    assert bootstrap.main([]) == 0
    assert observed == [True], "Product imports must not generate installed bytecode caches"


def test_streaming_child_disables_bytecode_in_argv(monkeypatch):
    from textflowkit.core import streaming_process
    from textflowkit.core.streaming_events import WorkerProcessError

    observed = []
    def spawn(argv, **kwargs):
        observed.append(argv)
        raise OSError("expected stop before spawn")
    monkeypatch.setattr(streaming_process.subprocess, "Popen", spawn)
    worker = streaming_process.SubprocessWorker(language=None, python="explicit-python.exe")
    with pytest.raises(WorkerProcessError, match="could not launch"):
        worker.start()
    assert observed == [["explicit-python.exe", "-B", "-u", "-m", worker._module]]


def test_selftest_invokes_existing_cli_without_ui(monkeypatch):
    bootstrap = load(BOOTSTRAP)
    events = []
    monkeypatch.setattr(bootstrap, "configure_environment", lambda *a: None)
    from contextlib import nullcontext
    monkeypatch.setattr(bootstrap, "app_mutex", nullcontext)
    monkeypatch.setitem(sys.modules, "textflowkit.cli",
                        SimpleNamespace(main=lambda args: events.append(args) or 0))
    assert bootstrap.main(["--selftest", "--engine", "whistle"]) == 0
    assert events == [["selftest", "--engine", "whistle"]]


def test_native_mutex_shared_handles_and_release():
    bootstrap = load(BOOTSTRAP)
    if os.name != "nt":
        pytest.skip("Windows named mutex integration")
    import ctypes
    with bootstrap.app_mutex(), bootstrap.app_mutex():
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
        api.OpenMutexW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenMutexW(0x100000, False, bootstrap.APP_MUTEX)
        assert handle
        assert api.CloseHandle(handle)


def test_installer_preserves_user_data_and_refuses_running_app():
    assert ISS.is_file(), "Inno Setup implementation missing"
    iss = ISS.read_text(encoding="utf-8")
    assert r"AppMutex=Local\TextFlowKit.Desktop" in iss
    assert "PrivilegesRequired=lowest" in iss
    assert "CloseApplications=no" in iss and "RestartApplications=no" in iss
    assert "[UninstallDelete]" not in iss and "[Registry]" not in iss
    assert '{localappdata}\\TextFlowKit\\ui' not in iss
    assert 'Filename: "{app}\\runtime\\pythonw.exe"' in iss
    assert 'Parameters: """{app}\\launch.py"""' in iss
    assert "CheckForMutexes" in iss  # recheck before copying/removing files


@pytest.mark.parametrize("old_versions", [[], ["0.1.17"], ["0.1.11", "0.1.16", "0.1.17"]])
def test_upgrade_replaces_core_metadata_without_touching_fonts_or_user_data(tmp_path, old_versions):
    """Model the recipe's delete-before-copy phase; never execute an installer.

    Inno's recursive copy overwrites files but leaves absent versioned directories.
    Apply the actual InstallDelete entries to an isolated old runtime, then overlay
    the new payload, and ask importlib.metadata what an installed consumer sees.
    """
    app = tmp_path / "app"
    site = app / "runtime/Lib/site-packages"
    incoming = tmp_path / "incoming"
    for root, versions in ((site, old_versions), (incoming, ["0.1.18"])):
        for version in versions:
            directory = root / f"textflowkit-{version}.dist-info"
            directory.mkdir(parents=True)
            (directory / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: textflowkit\nVersion: {version}\n",
                encoding="utf-8",
            )
        module = root / "textflowkit/__init__.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(f'__version__ = "{versions[-1] if versions else "0.0.0"}"\n')
    preserved = [
        site / "textflowkit_fonts-0.1.6.dist-info/METADATA",
        site / "other-1.0.dist-info/METADATA",
        site / "textflowkit_extra-1.0.dist-info/METADATA",
        app / "models/whistle.cact",
        tmp_path / "user-data/jobs.sqlite3",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve exactly")
    source = ISS.read_text(encoding="utf-8")
    section = source.split("[InstallDelete]", 1)[-1].split("\n[", 1)[0]
    entries = re.findall(r'Type: filesandordirs; Name: "([^"]+)"', section)
    # Constrain deletion to core metadata in this fixture, including future edits.
    prefix = "{app}\\runtime\\Lib\\site-packages\\"
    for entry in entries:
        assert entry == prefix + "textflowkit-*.dist-info"
        for directory in site.glob(entry.removeprefix(prefix)):
            assert site.resolve() in directory.resolve().parents
            shutil.rmtree(directory)
    shutil.copytree(incoming, site, dirs_exist_ok=True)
    versions = [d.version for d in metadata.distributions(path=[str(site)])
                if d.metadata.get("Name") == "textflowkit"]
    assert versions == ["0.1.18"], f"Overlay left stale installed core metadata: {versions}"
    assert '__version__ = "0.1.18"' in (site / "textflowkit/__init__.py").read_text()
    assert all(path.read_bytes() == b"preserve exactly" for path in preserved)


def test_seeded_valid_assets_are_read_only_and_offline(tmp_path, monkeypatch):
    bootstrap = load(BOOTSTRAP)
    from textflowkit.core import whistle_assets as assets
    model_bytes, engine_bytes = b"model", b"engine"
    def pin(name, data):
        return assets.PinnedAsset(name, "https://invalid.example/never-fetch",
                                  hashlib.sha256(data).hexdigest(), len(data))
    monkeypatch.setattr(assets, "WHISTLE_MODEL", pin("whistle.cact", model_bytes))
    monkeypatch.setattr(assets, "PLATFORM_BINARIES", {"windows-x86_64": assets.PlatformBinary(
        "windows-x86_64", pin("needle.exe", engine_bytes))})
    monkeypatch.setattr(assets, "current_platform", lambda: "windows-x86_64")
    env = {}
    bootstrap.configure_environment(tmp_path, env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    model = tmp_path / "models/whistle.cact"
    engine = tmp_path / "models/windows-x86_64/needle.exe"
    engine.parent.mkdir(parents=True)
    model.write_bytes(model_bytes)
    engine.write_bytes(engine_bytes)
    def forbidden(*a, **kw):
        pytest.fail("A valid seeded cache must never download or write")
    monkeypatch.setattr(assets, "_install_pinned", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    assert assets.ensure_assets() == (engine, model)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows message box")
def test_startup_import_failure_logs_and_notifies_natively(tmp_path, monkeypatch):
    bootstrap = load(BOOTSTRAP)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(bootstrap.sys, "stderr", None)
    calls = []
    class MessageBox:
        def __call__(self, *args):
            calls.append(args)
    monkeypatch.setattr(bootstrap.ctypes, "WinDLL",
                        lambda *a, **kw: SimpleNamespace(MessageBoxW=MessageBox()))
    try:
        raise ImportError("SECRET must not appear in the log")
    except ImportError as exc:
        bootstrap.report_failure(exc)
    log = tmp_path / "TextFlowKit/ui/desktop-startup.log"
    assert log.is_file()
    assert "ImportError" in log.read_text()
    assert "SECRET" not in log.read_text()
    assert len(calls) == 1
    assert str(log) in calls[0][1]


def test_automatic_compiler_discovery(tmp_path, monkeypatch):
    build = load(BUILD)
    compiler = tmp_path / "Inno Setup 6/ISCC.exe"
    compiler.parent.mkdir()
    compiler.write_bytes(b"compiler")
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    assert build.find_iscc() == compiler


def test_manifest_cannot_hide_nested_manifest_json(payload):
    build = load(BUILD)
    (payload / "bin/manifest.json").write_text("unlisted private data")
    with pytest.raises(ValueError, match="unlisted"):
        build.validate_payload(payload)


def test_third_party_notices_are_required_and_packaged():
    build = load(BUILD)
    assert "THIRD-PARTY-NOTICES.txt" in build.REQUIRED
    assert 'Source: "{#PayloadDir}\\THIRD-PARTY-NOTICES.txt"' in ISS.read_text()


def test_streaming_uses_console_sibling_but_explicit_interpreter_wins(tmp_path, monkeypatch):
    from textflowkit.core.streaming_process import SubprocessWorker
    console = tmp_path / "python.exe"
    console.write_bytes(b"interpreter")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert SubprocessWorker(language=None)._python == str(console)
    assert SubprocessWorker(language=None, python="explicit.exe")._python == "explicit.exe"
    console.unlink()
    assert SubprocessWorker(language=None)._python == str(tmp_path / "pythonw.exe")


@pytest.mark.parametrize("platform,executable,streams,hidden", [
    ("win32", "pythonw.exe", True, True),
    ("win32", "python.exe", False, True),
    ("win32", "python.exe", True, False),
    ("linux", "pythonw.exe", False, False),
])
def test_hidden_children_only_for_windows_consoleless(platform, executable, streams, hidden,
                                                     monkeypatch):
    helper_path = ROOT / "src/textflowkit/core/windows_process.py"
    helper = load(helper_path)
    monkeypatch.setattr(helper.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(helper, "sys", SimpleNamespace(
        platform=platform, executable=executable,
        stdout=object() if streams else None, stderr=object() if streams else None))
    assert helper.hidden_process_kwargs() == ({"creationflags": 0x08000000} if hidden else {})


def test_native_launch_sites_apply_consoleless_flags(monkeypatch, tmp_path):
    from textflowkit.core import service, whistle
    from textflowkit.core.streaming_process import SubprocessWorker
    from textflowkit.sources import acquire
    sentinel_flags = {"creationflags": 0x08000000}
    # Capture at the actual launch boundary without ever launching executables.
    def popen(*args, **kwargs):
        assert kwargs.get("creationflags") == sentinel_flags["creationflags"]
        raise OSError("expected test stop before spawn")
    monkeypatch.setattr(__import__("subprocess"), "Popen", popen)
    for module in (service, whistle, acquire):
        assert hasattr(module, "hidden_process_kwargs"), f"{module.__name__} lacks consoleless flags"
        monkeypatch.setattr(module, "hidden_process_kwargs", lambda: sentinel_flags)
    from textflowkit.core import streaming_process
    assert hasattr(streaming_process, "hidden_process_kwargs")
    monkeypatch.setattr(streaming_process, "hidden_process_kwargs", lambda: sentinel_flags)
    with pytest.raises(whistle.WhistleError, match="could not start"):
        whistle._run_native(["needle.exe"], timeout=1, check_cancel=None)
    with pytest.raises(Exception, match="could not launch"):
        SubprocessWorker(language=None).start()
    monkeypatch.setattr(acquire, "require_tool", lambda *a, **kw: "ffmpeg.exe")
    media = tmp_path / "input.wav"
    media.write_bytes(b"media")
    with pytest.raises(acquire.AcquisitionError, match="could not start"):
        acquire.extract_audio(media, work_dir=tmp_path)
    with pytest.raises(OSError, match="expected test stop"):
        service._probe_duration(media)


def test_sdist_includes_installer_recipe():
    settings = (ROOT / "pyproject.toml").read_text()
    sdist = settings.split("[tool.hatch.build.targets.sdist]", 1)[1].split("\n[", 1)[0]
    includes = sdist.split("include = [", 1)[1].split("]", 1)[0]
    assert '"/packaging"' in {line.strip().rstrip(",") for line in includes.splitlines()}


@pytest.mark.parametrize("missing_windll", [False, True])
def test_setup_presence_is_checked_after_app_presence_and_handle_released(
        monkeypatch, missing_windll):
    bootstrap = load(BOOTSTRAP)
    # Exercise the Windows branch with isolated module views, even on Linux/macOS.
    # Never remove WinDLL from the process-wide ctypes module on Windows.
    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(name="nt"))
    if missing_windll:
        monkeypatch.setattr(bootstrap, "ctypes", SimpleNamespace(**{
            name: value for name, value in vars(bootstrap.ctypes).items()
            if name != "WinDLL"
        }))
    events = []
    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result
        def __call__(self, *args):
            events.append((self.name, args))
            return self.result
    api = SimpleNamespace(CreateMutexW=Function("create", 101),
                          OpenMutexW=Function("open", 102),
                          CloseHandle=Function("close", True))
    monkeypatch.setattr(bootstrap.ctypes, "WinDLL", lambda *a, **kw: api, raising=False)
    with pytest.raises(OSError, match="install|setup"), bootstrap.app_mutex():
        pytest.fail("Application must not run during setup")
    assert events[0] == ("create", (None, False, r"Local\TextFlowKit.Desktop"))
    assert events[1][0] == "open" and events[1][1][-1] == r"Local\TextFlowKit.Setup"
    assert ("close", (102,)) in events and events[-1] == ("close", (101,))


@pytest.mark.skipif(os.name != "nt", reason="Windows installer mutex integration")
def test_real_setup_presence_refuses_bootstrap_and_releases_app_handle():
    bootstrap = load(BOOTSTRAP)
    import ctypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    api.CreateMutexW.restype = ctypes.c_void_p
    api.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    api.OpenMutexW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.CreateMutexW(None, False, r"Local\TextFlowKit.Setup")
    assert handle
    try:
        with pytest.raises(OSError, match="install|setup"), bootstrap.app_mutex():
            pytest.fail("Application must not start during setup")
        app = api.OpenMutexW(0x100000, False, bootstrap.APP_MUTEX)
        if app:
            api.CloseHandle(app)
        assert not app, "Refused startup leaked its application presence handle"
    finally:
        api.CloseHandle(handle)


def test_uninstall_locks_setup_before_checking_running_app():
    source = ISS.read_text()
    uninstall = source.split("function InitializeUninstall(): Boolean;", 1)[1]
    assert "CreateMutex('Local\\TextFlowKit.Setup')" in uninstall
    assert uninstall.index("CheckForMutexes('Local\\TextFlowKit.Setup')") < uninstall.index("CreateMutex(")
    assert uninstall.index("CreateMutex(") < uninstall.index("CheckForMutexes('Local\\TextFlowKit.Desktop')")


def test_installer_launch_waits_before_app_presence(monkeypatch):
    bootstrap = load(BOOTSTRAP)
    assert hasattr(bootstrap, "wait_for_setup"), "Installer launch needs deferred startup"
    events = []
    class Mutex:
        def __enter__(self):
            events.append("presence")
        def __exit__(self, *args):
            events.append("release")
    monkeypatch.setattr(bootstrap, "configure_environment", lambda *a: None)
    monkeypatch.setattr(bootstrap, "wait_for_setup", lambda: events.append("wait"))
    monkeypatch.setattr(bootstrap, "app_mutex", Mutex)
    def launch(args):
        assert args == []
        events.append("serve")
        return 0
    monkeypatch.setitem(sys.modules, "textflowkit.ui.launcher", SimpleNamespace(main=launch))
    assert bootstrap.main(["--after-install"]) == 0
    assert events == ["wait", "presence", "serve", "release"]
    run_section = ISS.read_text().split("[Run]", 1)[1].split("[Code]", 1)[0]
    assert "--after-install" in run_section and "nowait" in run_section


def test_installer_requires_native_x64_windows():
    source = ISS.read_text()
    assert "ArchitecturesAllowed=x64os" in source
    assert "ArchitecturesInstallIn64BitMode=x64os" in source
