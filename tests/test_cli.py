"""CLI surface: the doctor diagnostic and argument handling.

Platform support depends on yt-dlp working against sites it does not own, and
that breaks from outside. `doctor` exists so the question "which yt-dlp and which
JavaScript runtime are in play" has an answer that does not require guessing.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "textflowkit.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_sources_lists_platforms():
    r = _run("sources")
    assert r.returncode == 0
    for name in ("youtube", "tiktok", "facebook", "instagram", "local", "direct"):
        assert name in r.stdout


def test_doctor_reports_the_essentials():
    r = _run("doctor")
    assert r.returncode == 0
    out = r.stdout
    for label in (
        "textflowkit",
        "python",
        "ffmpeg",
        "yt-dlp",
        "js runtime",
        "input root",
        "output root",
        "jobs store",
        "device",
        "whisper device",
        "diarize device",
        "translation model",
    ):
        assert label in out, label


def test_doctor_reports_missing_optional_extras_without_failing():
    """A missing optional extra is information, not an error."""
    r = _run("doctor")
    assert r.returncode == 0
    assert "not installed" in r.stdout or "installed" in r.stdout


def test_doctor_does_not_claim_a_durable_store_by_default(monkeypatch):
    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    r = _run("doctor")
    assert "in-memory" in r.stdout


def test_doctor_reports_durable_store_when_configured(monkeypatch, tmp_path):
    env = {"TEXTFLOWKIT_DB": str(tmp_path / "jobs.db")}
    r = subprocess.run(
        [sys.executable, "-m", "textflowkit.cli", "doctor"],
        capture_output=True,
        text=True,
        check=False,
        env={**__import__("os").environ, **env},
    )
    assert "jobs.db" in r.stdout


def test_unknown_command_exits_nonzero():
    r = _run("nonsense")
    assert r.returncode != 0


def test_version_flag():
    r = _run("--version")
    assert r.returncode == 0
    assert "textflowkit" in r.stdout


def test_selftest_compute_only_passes():
    """The compute check must work anywhere - it is the closest CI can get to
    the GPU path, and it is what a user runs to verify their own machine."""
    r = _run("selftest", "--skip-transcribe")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout
    assert "SELFTEST PASSED" in r.stdout


def test_selftest_reports_the_torch_build_for_the_whisper_engine():
    """Naming the torch build is the point: it is how a ROCm install is told
    apart from a stock CPU wheel. That path is the Whisper engine's, so it is
    checked by selecting Whisper; the default (Whistle) needs no torch."""
    r = _run("selftest", "--engine", "whisper", "--skip-transcribe")
    assert "torch" in r.stdout


def test_selftest_defaults_to_whistle_and_needs_no_torch():
    """The default selftest checks the default engine, not a torch stack it
    would not use; it must pass on a machine with no torch at all."""
    r = _run("selftest", "--skip-transcribe")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "torch" not in r.stdout


def test_selftest_is_listed_in_help():
    r = _run("--help")
    assert "selftest" in r.stdout


# --- selftest: the sample-rate normalization contract -----------------------
#
# The bundled speech fixture is 22.05 kHz, the rate the Whisper-family engines
# read directly. Whistle's native CLI requires 16 kHz mono 16-bit PCM and refuses
# anything else, so handing it the raw fixture made `selftest` fail on the
# default engine even though the product always decodes to 16 kHz first. The
# self-test must exercise that same normalized input.


def _whistle_engine():
    from textflowkit.core.whistle import WhistleEngine

    return WhistleEngine()


def test_selftest_normalizes_the_bundled_fixture_for_whistle(tmp_path):
    """Whistle gets a 16 kHz mono 16-bit copy, not the 22.05 kHz fixture."""
    from importlib.resources import as_file, files

    from textflowkit.cli import _selftest_transcribe_input
    from textflowkit.core.whistle import read_wav_info

    fixture = files("textflowkit").joinpath("assets/selftest-speech.wav")
    with as_file(fixture) as wav:
        raw = read_wav_info(wav)
        assert raw.sample_rate == 22050, "fixture rate changed; test assumes 22.05 kHz"
        normalized = _selftest_transcribe_input(
            _whistle_engine(), wav, work_dir=tmp_path
        )
        info = read_wav_info(normalized)
        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.sample_width == 2


def test_selftest_leaves_a_non_whistle_engine_input_untouched(tmp_path):
    """A Whisper-family engine reads the fixture directly; nothing is decoded."""
    from importlib.resources import as_file, files

    from textflowkit.cli import _selftest_transcribe_input

    class _NotWhistle:
        pass

    fixture = files("textflowkit").joinpath("assets/selftest-speech.wav")
    with as_file(fixture) as wav:
        returned = _selftest_transcribe_input(_NotWhistle(), wav, work_dir=tmp_path)
        assert returned == wav  # unchanged


# --- platform and optional-extra refusals need no acquisition ---------------


def test_whistle_on_an_unsupported_platform_is_refused_before_acquisition(monkeypatch):
    """`require_engine('whistle')` refuses an unsupported host with ValueError -
    never a Whisper fallback, and never after media was fetched."""
    from textflowkit.core import engine as engine_mod
    from textflowkit.core import whistle_assets

    def _no_platform():
        raise whistle_assets.WhistleAssetError(
            "Whistle has no native runtime for platform 'fake'. "
            "Select the Whisper engine explicitly instead (engine='whisper')."
        )

    monkeypatch.setattr(whistle_assets, "current_platform", _no_platform)
    with pytest.raises(ValueError, match="no native runtime"):
        engine_mod.require_engine("whistle")


def test_require_engine_never_network_accesses_for_whistle(monkeypatch):
    """The platform check reads a table; it must not fetch an asset."""
    from textflowkit.core import engine as engine_mod
    from textflowkit.core import whistle_assets

    def _boom(*a, **k):
        raise AssertionError("require_engine must not acquire assets")

    monkeypatch.setattr(whistle_assets, "ensure_assets", _boom, raising=False)
    # On this platform Whistle is assumed supported (the test host); the point is
    # only that no acquisition happens either way.
    try:
        engine_mod.require_engine("whistle")
    except ValueError:
        pass  # refused on platform grounds is fine; it still did not acquire


def test_a_missing_whisper_extra_is_refused_as_a_value_error(monkeypatch):
    """An absent optional Whisper must be a request refusal (ValueError), not an
    unhandled RuntimeError escaping request construction as a 500."""
    from textflowkit.core import engine as engine_mod

    def _missing(_name):
        raise RuntimeError("openai-whisper is not installed.")

    monkeypatch.setattr(engine_mod, "engine_model_names", _missing)
    with pytest.raises(ValueError, match="not installed"):
        engine_mod.validate_model("small", "whisper")


def test_an_invalid_model_is_refused_by_the_pipeline_before_acquisition(tmp_path):
    """A direct Python caller gets the same cheap model refusal the adapters do,
    before any media is fetched or decoded."""
    from textflowkit.core import pipeline

    source = tmp_path / "clip.wav"
    source.write_bytes(b"not really audio")
    with pytest.raises(pipeline.PipelineError, match="unknown model"):
        pipeline.transcribe(str(source), engine="whistle", model="small",
                            input_root=tmp_path)
