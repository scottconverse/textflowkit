"""CLI: an unprintable --stdout-format fails before any work.

``--stdout`` writes straight to the terminal, so it can only carry text. The
renderer enforces that, but it runs after the job has been submitted and the
whole transcription paid for: the user waits out an acquisition and a model run
and then gets a traceback. The format is knowable from the arguments alone, so
the failure belongs ahead of submission, costing nothing.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from textflowkit import cli as cli_mod
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript
from textflowkit.render import TEXT_FORMATS, render


def _transcript(source: str = "media.wav") -> Transcript:
    return Transcript(
        source=source,
        language="en",
        platform="local",
        segments=[Segment(0.0, 1.5, "hello world")],
    )


def _stub_submission(monkeypatch):
    """Record whether submission was reached, and return a finished job.

    The point of the preflight is that these calls do not happen at all, so a
    transcript is supplied: in the broken version the run reaches the renderer
    and dies there, which is exactly the late failure being removed.
    """
    calls: list[str] = []
    store = MemoryJobStore()

    def fake_store():
        calls.append("store")
        return store

    def fake_submit(store_arg, request, **kwargs):
        calls.append("submit")
        job = store_arg.create(request.source, request=request.to_dict())
        return store_arg.update(
            job.id, state=JobState.DONE, transcript=_transcript(request.source).to_dict()
        )

    monkeypatch.setattr(cli_mod, "get_default_store", fake_store)
    monkeypatch.setattr(cli_mod, "submit_request", fake_submit)
    return calls


def _error_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.strip()]


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_binary_stdout_format_is_refused_before_any_work(monkeypatch, capsys, fmt):
    calls = _stub_submission(monkeypatch)

    rc = cli_mod.main(["transcribe", "media.wav", "--stdout", "--stdout-format", fmt])

    captured = capsys.readouterr()
    assert rc == 1
    assert calls == [], f"submission ran before the format was rejected: {calls}"
    assert captured.out == ""
    lines = _error_lines(captured.err)
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("error:")
    assert fmt in lines[0]


def test_unknown_stdout_format_is_refused_before_any_work(monkeypatch, capsys):
    calls = _stub_submission(monkeypatch)

    rc = cli_mod.main(["transcribe", "media.wav", "--stdout", "--stdout-format", "yaml"])

    captured = capsys.readouterr()
    assert rc == 1
    assert calls == [], f"submission ran before the format was rejected: {calls}"
    assert captured.out == ""
    lines = _error_lines(captured.err)
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("error:")
    assert "yaml" in lines[0]


def test_refusal_names_the_formats_that_would_work(monkeypatch, capsys):
    """One clear message: what went wrong and what to type instead."""
    _stub_submission(monkeypatch)

    assert cli_mod.main(["transcribe", "media.wav", "--stdout", "--stdout-format", "pdf"]) == 1

    err = capsys.readouterr().err
    assert "txt" in err
    for fmt in TEXT_FORMATS:
        assert fmt in err


@pytest.mark.parametrize("fmt", ["txt", "srt", "vtt", "md", "json"])
def test_valid_text_stdout_format_still_prints_the_transcript(monkeypatch, capsys, fmt):
    _stub_submission(monkeypatch)

    rc = cli_mod.main(["transcribe", "media.wav", "--stdout", "--stdout-format", fmt, "--quiet"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.out == render(_transcript(), fmt)


def test_default_stdout_format_is_still_txt(monkeypatch, capsys):
    _stub_submission(monkeypatch)

    rc = cli_mod.main(["transcribe", "media.wav", "--stdout", "--quiet"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.out == render(_transcript(), "txt")


def test_stdout_format_is_not_policed_when_stdout_is_not_requested(monkeypatch, capsys):
    """Without --stdout the option is inert, so it must not start failing runs.

    Writing files goes through render_bytes and legitimately accepts pdf/docx,
    so an existing invocation that passes --stdout-format pdf alongside file
    formats keeps working.
    """
    calls = _stub_submission(monkeypatch)

    rc = cli_mod.main(["transcribe", "media.wav", "--stdout-format", "pdf", "--quiet"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert calls == ["store", "submit"]


def test_invalid_stdout_format_fails_without_a_traceback():
    """The user-visible contract: exit 1 and one line, not a Python traceback.

    A missing source proves ordering on its own: the format is rejected before
    the source is ever looked at, so the diagnostic is about the format.
    """
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "textflowkit.cli",
            "transcribe",
            "no-such-file-anywhere.wav",
            "--stdout",
            "--stdout-format",
            "pdf",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert r.returncode == 1, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    lines = _error_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert lines[0].startswith("error:")
    assert "pdf" in lines[0]
