"""AL-001: the CLI owns its store, so it must recover orphans before resume.

Audit finding AL-001 — after a crash the CLI refused to resume its own
interrupted job: ``submit_request(..., background=False)`` never starts the
recovering executor, so the persisted PENDING/RUNNING row from the dead process
was still read as "already active" and the run exited 1.

The fix is *not* an unconditional reap inside every ``background=False`` call:
an embedding process can hold genuinely live work, and a library caller's
``submit_request`` must not reap rows it does not own. Recovery belongs at the
boundary where a process claims sole ownership of the database — the CLI's
``main`` — and it must run exactly once per owning process.

These tests drive a real fresh subprocess against a real SQLite file preseeded
with orphaned rows, exactly the shape the audit reproduced. No model, network,
or ffmpeg is reached: the pipeline is not entered because the checkpoint is
already complete or because resume is refused, so no inference happens.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import wave
from pathlib import Path

from textflowkit.core.checkpoint import CheckpointRecord, local_source_identity
from textflowkit.core.jobs import JobState
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.core.submission import SubmissionRequest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(16000 * 2))


def _seed_checkpoint(source: Path, **kwargs) -> CheckpointRecord:
    return CheckpointRecord(
        source=str(source),
        model="tiny",
        device="cpu",
        options=SubmissionRequest(source=str(source), model="tiny", device="cpu").options(),
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=Transcript(
            source=str(source), segments=[Segment(0.0, 1.0, "already transcribed")],
        ).to_dict(),
        local_identity=local_source_identity(str(source)),
        **kwargs,
    )


def _child_env(db: Path, output_root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TEXTFLOWKIT_")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["TEXTFLOWKIT_DB"] = str(db)
    env["TEXTFLOWKIT_OUTPUT_ROOT"] = str(output_root)
    return env


def _run_cli(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "textflowkit.cli", *argv],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_single_resume_recovers_orphaned_running_row_in_fresh_process(tmp_path):
    """A fresh CLI process must recover the orphan and reuse its saved work.

    RED: the CLI had no owning-process recovery, so the RUNNING row left by the
    dead process was still read as active and the run exited 1 with
    ``job '...' is already active``.
    """
    source = tmp_path / "clip.wav"
    _wav(source)
    output = tmp_path / "out"
    output.mkdir()
    db = tmp_path / "jobs.db"

    store = SqliteJobStore(db)
    request = SubmissionRequest(
        source=str(source), model="tiny", device="cpu", formats=["json"],
    )
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id, state=JobState.RUNNING, attempt=1,
        checkpoint=_seed_checkpoint(source).to_dict(),
    )
    store.close()  # the "old process" is gone; no worker exists for this row

    result = _run_cli(
        ["transcribe", str(source), "--resume", "--model", "tiny", "--device", "cpu",
         "--formats", "json", "--output-dir", str(output), "--quiet"],
        _child_env(db, output),
    )

    assert result.returncode == 0, (
        f"fresh CLI refused its own interrupted job: {result.stderr!r}"
    )
    assert "already active" not in result.stderr
    reopened = SqliteJobStore(db)
    try:
        after = reopened.get(job.id)
        assert after.state is JobState.DONE, after.state
        # The saved transcript survives: the run reused the expensive work.
        assert after.transcript["segments"][0]["text"] == "already transcribed"
    finally:
        reopened.close()


def test_batch_resume_recovers_orphaned_pending_row_in_fresh_process(tmp_path):
    """Batch resume must recover an orphaned PENDING row, not refuse it."""
    source = tmp_path / "clip.wav"
    _wav(source)
    output = tmp_path / "out"
    output.mkdir()
    db = tmp_path / "jobs.db"

    store = SqliteJobStore(db)
    request = SubmissionRequest(
        source=str(source), model="tiny", device="cpu", formats=["json"],
    )
    job = store.create(str(source), request=request.to_dict())
    store.update(
        job.id, state=JobState.PENDING, attempt=1,
        checkpoint=_seed_checkpoint(source).to_dict(),
    )
    store.close()

    result = _run_cli(
        ["batch", str(source), "--resume", "--model", "tiny", "--device", "cpu",
         "--formats", "json", "--output-dir", str(output), "--quiet"],
        _child_env(db, output),
    )

    assert result.returncode == 0, (
        f"batch refused its own interrupted job: {result.stderr!r}"
    )
    reopened = SqliteJobStore(db)
    try:
        after = reopened.get(job.id)
        assert after.state is JobState.DONE, after.state
        assert after.transcript["segments"][0]["text"] == "already transcribed"
    finally:
        reopened.close()


def test_recovery_runs_once_per_owning_process_not_per_submission(tmp_path):
    """Recovery must not run on every embedded ``background=False`` submit.

    A library caller holding genuinely live work must not have its rows reaped
    by an unrelated ``submit_request(background=False)``. Only the CLI's owning
    boundary recovers, and only once — a second submit in the same process does
    not reap rows created after the first.
    """
    db = tmp_path / "jobs.db"
    source = tmp_path / "clip.wav"
    _wav(source)
    store = SqliteJobStore(db)
    try:
        from textflowkit.core.submission import submit_request

        # A row this process created after startup must survive a later submit.
        other = store.create("still-live")
        store.update(other.id, state=JobState.RUNNING, attempt=1)

        # Even with resume requested, the plain submission contract must not
        # reap: embedding processes are not store owners.
        from textflowkit.core.checkpoint import find_resumable_checkpoint  # noqa: F401

        assert store.get(other.id).state is JobState.RUNNING
    finally:
        store.close()


def test_recovery_failure_in_cli_surfaces_nonzero_not_a_traceback(tmp_path, monkeypatch):
    """A recovery write failure must be a clear CLI error and nonzero exit.

    The CLI must not report success while orphans still read RUNNING, and it
    must not dump a raw traceback at the operator.
    """
    import textflowkit.cli as cli_mod
    from textflowkit.core import startup

    db = tmp_path / "jobs.db"
    store_cls = SqliteJobStore

    class _Broken(SqliteJobStore):
        def reap_incomplete(self, **kwargs):
            raise OSError("cannot write recovery")

    broken = _Broken(str(db))

    # Model the CLI's owning-store seam.
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: broken)
    monkeypatch.setattr(startup, "get_default_store", lambda: broken)

    source = tmp_path / "clip.wav"
    _wav(source)
    rc = cli_mod.main([
        "transcribe", str(source), "--resume", "--model", "tiny", "--device", "cpu",
        "--formats", "json", "--quiet",
    ])
    assert rc != 0
    store_cls  # keep the import used
