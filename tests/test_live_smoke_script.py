"""The optional live gate checks transcript content, not just process exit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_live_youtube


@pytest.mark.parametrize("mode,expected", [
    ("valid", 0), ("missing", 1), ("invalid", 1),
])
def test_live_smoke_requires_a_parseable_timestamped_transcript(
    monkeypatch, tmp_path, mode, expected, capsys,
):
    def fake_run(command, **kwargs):
        if command[0] == "git":
            return SimpleNamespace(
                returncode=0,
                stdout="a" * 40 if "rev-parse" in command else "",
                stderr="",
            )
        assert kwargs["env"]["PYTHONPATH"].split(smoke_live_youtube.os.pathsep)[0] == str(
            smoke_live_youtube.REPO_ROOT / "src"
        )
        output = Path(command[command.index("--output-dir") + 1])
        if mode != "missing":
            payload = {"platform": "youtube", "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello"},
            ]}
            if mode == "invalid":
                payload["segments"][0]["end"] = 0.0
            (output / "transcript.json").write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", fake_run)
    receipt = tmp_path / "receipt.json"
    rc = smoke_live_youtube.main(["--receipt", str(receipt)])
    assert rc == expected
    if mode == "valid":
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        assert saved["segment_count"] == 1
        assert saved["git_commit"] == "a" * 40
        assert saved["checked_at_utc"].endswith("+00:00")
        assert saved["runner_os"]
        assert "transcript_sha256" in capsys.readouterr().out
    else:
        assert not receipt.exists()
        assert "live smoke failed" in capsys.readouterr().err


def test_live_smoke_refuses_dirty_candidate(monkeypatch, tmp_path, capsys):
    def fake_run(command, **kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="a" * 40, stderr="")
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout=" M src/textflowkit/cli.py\n", stderr="")
        raise AssertionError("transcription must not run for a dirty candidate")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", fake_run)
    receipt = tmp_path / "receipt.json"
    assert smoke_live_youtube.main(["--receipt", str(receipt)]) == 1
    assert not receipt.exists()
    assert "commit the candidate first" in capsys.readouterr().err


def test_live_smoke_refuses_missing_git_checkout(monkeypatch, tmp_path, capsys):
    def fake_run(command, **kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(returncode=128, stdout="", stderr="not a repository")
        if "status" in command:
            return SimpleNamespace(returncode=128, stdout="", stderr="not a repository")
        raise AssertionError("transcription must not run without a Git checkout")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", fake_run)
    receipt = tmp_path / "receipt.json"
    assert smoke_live_youtube.main(["--receipt", str(receipt)]) == 1
    assert not receipt.exists()
    assert "valid HEAD" in capsys.readouterr().err


def test_live_smoke_never_reuses_an_existing_receipt(monkeypatch, tmp_path, capsys):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("existing receipt must be rejected before running")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", forbidden_run)
    receipt = tmp_path / "old.json"
    receipt.write_text('{"old": true}', encoding="utf-8")
    assert smoke_live_youtube.main(["--receipt", str(receipt)]) == 1
    assert receipt.read_text(encoding="utf-8") == '{"old": true}'
    assert "already exists" in capsys.readouterr().err


def test_live_smoke_refuses_receipt_inside_checkout(monkeypatch, capsys):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("in-repository receipt must be rejected before running")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", forbidden_run)
    receipt = smoke_live_youtube.REPO_ROOT / "do-not-write-receipt.json"
    assert smoke_live_youtube.main(["--receipt", str(receipt)]) == 1
    assert not receipt.exists()
    assert "outside the repository" in capsys.readouterr().err
