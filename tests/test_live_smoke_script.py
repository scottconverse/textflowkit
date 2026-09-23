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
        assert json.loads(receipt.read_text(encoding="utf-8"))["segment_count"] == 1
        assert "transcript_sha256" in capsys.readouterr().out
    else:
        assert not receipt.exists()
        assert "live smoke failed" in capsys.readouterr().err
