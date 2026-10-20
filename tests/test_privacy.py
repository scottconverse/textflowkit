"""Dependency telemetry is disabled for every TextFlowKit process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPT_IN_ENV = {
    "PYANNOTE_METRICS_ENABLED": "true",
    "HF_HUB_DISABLE_TELEMETRY": "0",
    "DISABLE_TELEMETRY": "0",
    "DO_NOT_TRACK": "0",
}
FORCED_ENV = {
    "PYANNOTE_METRICS_ENABLED": "false",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(OPT_IN_ENV)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests"), env.get("PYTHONPATH", "")]
    )
    return env


def _run_fresh(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_package_import_overrides_inherited_opt_ins_before_pipeline_import():
    code = """
import builtins
import json
import os
import sys

seen = []
real_import = builtins.__import__


def checking_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "textflowkit.core.pipeline":
        seen.append({key: os.environ.get(key) for key in (
            "PYANNOTE_METRICS_ENABLED",
            "HF_HUB_DISABLE_TELEMETRY",
            "DISABLE_TELEMETRY",
            "DO_NOT_TRACK",
        )})
    return real_import(name, globals, locals, fromlist, level)


builtins.__import__ = checking_import
import textflowkit

print(json.dumps({
    "seen": seen,
    "env": {key: os.environ.get(key) for key in (
        "PYANNOTE_METRICS_ENABLED",
        "HF_HUB_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    )},
    "optional": sorted(
        name for name in ("torch", "pyannote", "huggingface_hub")
        if name in sys.modules
    ),
}))
"""
    result = _run_fresh(code)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["seen"], "the pipeline import was not observed"
    assert all(seen == FORCED_ENV for seen in payload["seen"])
    assert payload["env"] == FORCED_ENV
    assert payload["optional"] == []


def test_preimported_huggingface_hub_cached_constant_is_disarmed():
    code = """
import sys
import types

constants = types.ModuleType("huggingface_hub.constants")
constants.HF_HUB_DISABLE_TELEMETRY = False
hub = types.ModuleType("huggingface_hub")
hub.constants = constants
sys.modules["huggingface_hub"] = hub
sys.modules["huggingface_hub.constants"] = constants

import textflowkit

assert constants.HF_HUB_DISABLE_TELEMETRY is True
assert sys.modules["huggingface_hub"] is hub
print("ok")
"""
    result = _run_fresh(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_diarizer_forces_opt_out_before_importing_and_loading_pyannote(monkeypatch):
    from textflowkit.core.diarize import PyannoteDiarizer

    fake_package = types.ModuleType("pyannote")
    fake_audio = types.ModuleType("pyannote.audio")

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, model, **kwargs):
            return object()

    fake_audio.Pipeline = FakePipeline
    fake_package.audio = fake_audio
    monkeypatch.setitem(sys.modules, "pyannote", fake_package)
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_audio)

    diarizer = PyannoteDiarizer(token="fake-token", device="cpu")
    for key, value in OPT_IN_ENV.items():
        monkeypatch.setenv(key, value)

    diarizer._load()

    assert {key: os.environ[key] for key in FORCED_ENV} == FORCED_ENV


def test_diarizer_forces_opt_out_before_each_pipeline_use(monkeypatch):
    from textflowkit.core.diarize import PyannoteDiarizer

    observed = []

    class Turn:
        start = 0.0
        end = 1.0

    class Annotation:
        def itertracks(self, *, yield_label):
            assert yield_label is True
            yield Turn(), None, "SPEAKER_00"

    class FakePipeline:
        def __call__(self, waveform):
            observed.append({key: os.environ[key] for key in FORCED_ENV})
            return Annotation()

    diarizer = PyannoteDiarizer(token="fake-token", device="cpu")
    diarizer._pipeline = FakePipeline()
    monkeypatch.setattr(diarizer, "_load_waveform", lambda path: {"fake": path})

    for key, value in OPT_IN_ENV.items():
        monkeypatch.setenv(key, value)

    turns = diarizer.diarize("stub.wav")

    assert [turn.speaker for turn in turns] == ["SPEAKER_00"]
    assert observed == [FORCED_ENV]
    assert {key: os.environ[key] for key in FORCED_ENV} == FORCED_ENV
