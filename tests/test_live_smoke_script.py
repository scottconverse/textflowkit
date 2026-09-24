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
                {"start": 0.0, "end": 1.0, "text": "Alright so here we are one of the elephants."},
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


OTHER_URL = "https://www.youtube.com/watch?v=OVERRIDE0000"

# The default word is an observation, not a guess. This is the coordinator's
# baseline transcript of the default clip (2026-09-24, `--model tiny --device
# cpu`), copied in so a later edit cannot quietly swap the default expectation
# for a word the clip was never heard to say. The live re-run stays a
# maintainer step; this test only pins the premise the gate rests on.
OBSERVED_DEFAULT_CLIP_SEGMENTS = [
    "Alright so here we are one of the elephants.",
    "Cool thing for these guys is that they have really really long prompts and that's cool.",
    "And that's pretty much all it is to say.",
]


def test_the_default_word_is_one_the_default_clip_is_observed_to_say() -> None:
    spoken = smoke_live_youtube._normalize_words(" ".join(OBSERVED_DEFAULT_CLIP_SEGMENTS))
    assert smoke_live_youtube.DEFAULT_EXPECT_TEXT == "elephants"
    assert smoke_live_youtube._contains_whole_phrase(
        spoken, smoke_live_youtube._normalize_words(smoke_live_youtube.DEFAULT_EXPECT_TEXT)
    )


def _transcript_run(texts: list[str]):
    """Fake subprocess.run: a clean candidate plus one JSON transcript."""

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return SimpleNamespace(
                returncode=0,
                stdout="a" * 40 if "rev-parse" in command else "",
                stderr="",
            )
        output = Path(command[command.index("--output-dir") + 1])
        payload = {
            "platform": "youtube",
            "segments": [
                {"start": float(index), "end": float(index) + 0.75, "text": text}
                for index, text in enumerate(texts)
            ],
        }
        (output / "transcript.json").write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    return fake_run


# QA-02: a nonempty timed segment is not evidence of recognized speech. The
# first row is the defect as filed: plausible unrelated audio passed.
CONTENT_CASES = [
    (["Hello and welcome to an unrelated video."], [], False),
    (["Alright so here we are one of the elephants."], [], True),
    (["Alright so here we are one of the", "elephants."], [], True),
    (["Alright so here we are one of the ELEPHANTS!"], [], True),
    (["Alright so here we are one of the", "elephants, and that's it."], [], True),
    (["Alright so here we are one of the elephant."], [], False),
    (["Elephantiasis is not this clip."], [], False),
    (["Said with a pause: elephants are here."], [], True),
    (["Alright so here we are one of the elephants."], ["--expect-text", "elephant"], False),
    (["A short clip about bicycles and bells."],
     ["--url", OTHER_URL, "--expect-text", "bicycles"], True),
    (["A short clip about bicycle bells."],
     ["--url", OTHER_URL, "--expect-text", "bicycles"], False),
    (["A short clip about bicycles and bells."],
     ["--url", OTHER_URL, "--expect-text", "clip about"], True),
    (["A short clip about bicycles and bells."],
     ["--url", OTHER_URL, "--expect-text", "clip bells"], False),
    (["A short clip about bicycles and bells."],
     ["--url", OTHER_URL, "--expect-text", "Bicycles!"], True),
]


@pytest.mark.parametrize("texts,extra,passes", CONTENT_CASES)
def test_live_smoke_asserts_expected_speech(
    monkeypatch, tmp_path, capsys, texts, extra, passes,
):
    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", _transcript_run(texts))
    receipt = tmp_path / "receipt.json"
    rc = smoke_live_youtube.main([*extra, "--receipt", str(receipt)])
    if passes:
        assert rc == 0, capsys.readouterr().err
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        assert saved["content_assertion"]["matched"] is True
    else:
        assert rc == 1
        assert not receipt.exists()
        assert "not found" in capsys.readouterr().err


def test_live_smoke_requires_expect_text_for_a_nondefault_url(monkeypatch, tmp_path, capsys):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("no git, download, or model work before the expectation is named")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", forbidden_run)
    receipt = tmp_path / "receipt.json"
    with pytest.raises(SystemExit) as excinfo:
        smoke_live_youtube.main(["--url", OTHER_URL, "--receipt", str(receipt)])
    assert excinfo.value.code == 2
    assert not receipt.exists()
    assert "--expect-text" in capsys.readouterr().err


@pytest.mark.parametrize("blank", ["", "   ", "!!! ..."])
def test_live_smoke_rejects_a_blank_expect_text(monkeypatch, tmp_path, capsys, blank):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("a blank expectation must be rejected before any work")

    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", forbidden_run)
    receipt = tmp_path / "receipt.json"
    with pytest.raises(SystemExit) as excinfo:
        smoke_live_youtube.main(["--expect-text", blank, "--receipt", str(receipt)])
    assert excinfo.value.code == 2
    assert not receipt.exists()
    assert "at least one" in capsys.readouterr().err


def test_receipt_records_the_content_assertion_but_not_the_transcript(
    monkeypatch, tmp_path,
):
    spoken = "Alright so here we are one of the elephants."
    monkeypatch.setattr(smoke_live_youtube.subprocess, "run", _transcript_run([spoken]))
    receipt = tmp_path / "receipt.json"
    assert smoke_live_youtube.main(["--receipt", str(receipt)]) == 0
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assertion = saved["content_assertion"]
    assert assertion["expected_text"] == "elephants"
    assert assertion["expected_text_source"] == "default-clip"
    assert assertion["matched"] is True
    assert "not a general transcript-accuracy certification" in assertion["scope"]
    assert saved["url"] == smoke_live_youtube.DEFAULT_URL
    assert spoken not in receipt.read_text(encoding="utf-8")


def test_receipt_records_a_caller_supplied_expectation_as_such(monkeypatch, tmp_path):
    monkeypatch.setattr(
        smoke_live_youtube.subprocess, "run",
        _transcript_run(["This clip still says elephants, spelled the same."]),
    )
    receipt = tmp_path / "receipt.json"
    assert smoke_live_youtube.main(["--expect-text", "Elephants", "--receipt", str(receipt)]) == 0
    assertion = json.loads(receipt.read_text(encoding="utf-8"))["content_assertion"]
    assert assertion["expected_text"] == "Elephants"
    assert assertion["expected_text_source"] == "caller-supplied"
