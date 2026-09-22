"""CLI surface: the doctor diagnostic and argument handling.

Platform support depends on yt-dlp working against sites it does not own, and
that breaks from outside. `doctor` exists so the question "which yt-dlp and which
JavaScript runtime are in play" has an answer that does not require guessing.
"""

from __future__ import annotations

import subprocess
import sys


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


def test_selftest_reports_the_torch_build():
    """Naming the torch build is the point: it is how a ROCm install is told
    apart from a stock CPU wheel."""
    r = _run("selftest", "--skip-transcribe")
    assert "torch" in r.stdout


def test_selftest_is_listed_in_help():
    r = _run("--help")
    assert "selftest" in r.stdout
