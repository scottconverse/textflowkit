"""CLI ``batch --resume`` must use the shared checked submission lifecycle.

Outside review A1: `run_batch` had its own resume path that found a matching
checkpoint and then called ``reusable_done_result(store, prior)`` with no
formats, output directory, or stem. Two defects followed:

- A local source whose bytes changed after the checkpoint was reported as a
  stale success, because the fingerprint check (``validate_local_resume``) was
  never reached.
- A ``--resume`` run with a new ``--output-dir`` returned the old paths instead
  of writing into the newly requested directory.

These tests drive the real CLI over a confined temp tree with a fake engine, so
no model download or network access is involved. They mirror the fixtures in
``test_release_boundaries`` for the same boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_release_boundaries import _RecordingEngine, _wav
from textflowkit import cli
from textflowkit.core.batch import run_batch
from textflowkit.core.jobs import JobState
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.sources.acquire import AcquisitionError, require_tool


def _require_ffmpeg() -> None:
    try:
        require_tool("ffmpeg")
    except AcquisitionError:  # pragma: no cover - environment without ffmpeg
        pytest.skip("ffmpeg is required for the local media pipeline")


def _flip_byte(path: Path, offset: int = 100) -> None:
    """Change a file's bytes without changing its size.

    Size and mtime alone cannot detect this, so it pins the content fingerprint.
    """
    with path.open("r+b") as handle:
        handle.seek(offset)
        original = handle.read(1)
        handle.seek(offset)
        handle.write(b"\x01" if original != b"\x01" else b"\x02")


@pytest.fixture
def batch_cli(tmp_path, monkeypatch):
    """A confined CLI boundary: real store, real pipeline, fake engine."""
    from textflowkit.core import pipeline

    root = tmp_path / "input"
    output = tmp_path / "output"
    root.mkdir()
    output.mkdir()
    store = SqliteJobStore(tmp_path / "jobs.db")
    engine = _RecordingEngine()
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(cli, "get_default_store", lambda: store)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)
    yield root, output, store, engine
    store.close()


def _batch_args(sources, output_dir, *, resume: bool = False) -> list[str]:
    args = [
        "batch", *[str(source) for source in sources],
        "--formats", "json",
        "--output-dir", str(output_dir),
        "--model", "tiny",
        "--device", "cpu",
    ]
    if resume:
        args.append("--resume")
    return args


def _item_lines(stdout: str) -> list[str]:
    return [
        line for line in stdout.splitlines()
        if line.startswith(("succeeded", "failed", "skipped"))
    ]


def _reported_outputs(line: str) -> list[Path]:
    """The output paths a per-item report line printed after ' - '."""
    if " - " not in line:
        return []
    return [Path(part) for part in line.split(" - ", 1)[1].split(", ") if part]


def test_batch_resume_rejects_local_source_changed_since_checkpoint(batch_cli, capsys):
    """A changed local file must fail that item, not report the stale transcript."""
    _require_ffmpeg()
    root, output, store, engine = batch_cli
    media = root / "clip.wav"
    _wav(media)
    args = _batch_args([media], output)

    assert cli.main(args) == 0
    assert engine.calls == 1
    capsys.readouterr()

    _flip_byte(media)

    assert cli.main([*args, "--resume"]) == 1
    stdout = capsys.readouterr().out
    lines = _item_lines(stdout)

    assert len(lines) == 1
    assert lines[0].startswith("failed"), lines[0]
    assert "changed" in lines[0].lower(), lines[0]
    assert "batch: 1 total, 0 succeeded, 1 failed, 0 skipped" in stdout
    assert engine.calls == 1, "a rejected resume must not transcribe again"
    assert len(store.list(limit=100)) == 1, "a rejected resume must not create a job"


def test_batch_resume_writes_to_the_newly_requested_output_directory(batch_cli, capsys):
    """--resume with a new --output-dir must report outputs in that directory."""
    _require_ffmpeg()
    root, output, _store, engine = batch_cli
    media = root / "clip.wav"
    _wav(media)
    first_dir = output / "first"
    second_dir = output / "second"

    assert cli.main(_batch_args([media], first_dir)) == 0
    first_stdout = capsys.readouterr().out
    first_paths = _reported_outputs(_item_lines(first_stdout)[0])
    assert [path.parent for path in first_paths] == [first_dir.resolve()]
    assert engine.calls == 1

    assert cli.main(_batch_args([media], second_dir, resume=True)) == 0
    stdout = capsys.readouterr().out
    resumed_paths = _reported_outputs(_item_lines(stdout)[0])

    assert engine.calls == 1, "resume must reuse the stored transcript"
    assert resumed_paths, "a resumed item must report the outputs it produced"
    for path in resumed_paths:
        assert path.parent == second_dir.resolve(), path
        assert path.is_file(), path
    # Same file name as the original run, published under the new directory.
    assert resumed_paths[0].name == first_paths[0].name
    assert first_paths[0].is_file(), "the earlier output must not be clobbered"


def test_batch_resume_keeps_item_failures_isolated(batch_cli, capsys):
    """One rejected item must not stop, or hide, the item after it."""
    _require_ffmpeg()
    root, output, store, engine = batch_cli
    changed = root / "changed.wav"
    stable = root / "stable.wav"
    _wav(changed)
    _wav(stable)
    args = _batch_args([changed, stable], output)

    assert cli.main(args) == 0
    capsys.readouterr()
    assert engine.calls == 2
    before = {job.source: job for job in store.list(limit=100)}
    assert set(before) == {str(changed), str(stable)}

    _flip_byte(changed)

    assert cli.main([*args, "--resume"]) == 1
    stdout = capsys.readouterr().out
    lines = _item_lines(stdout)

    assert len(lines) == 2
    assert lines[0].startswith("failed") and "changed" in lines[0].lower(), lines[0]
    assert lines[1].startswith("succeeded"), lines[1]
    assert "batch: 2 total, 1 succeeded, 1 failed, 0 skipped" in stdout
    assert engine.calls == 2
    assert len(store.list(limit=100)) == 2, "the rejected item must not add a job"

    # The good item resumed its own job and kept the outputs it already had.
    resume_prior = before[str(stable)]
    after = store.get(resume_prior.id)
    assert after.state is JobState.DONE
    assert after.outputs == resume_prior.outputs
    assert after.outputs


def test_batch_report_marks_reused_items_as_resumed(batch_cli):
    """A reuse keeps the prior job id and outputs and is reported as `resumed`."""
    _require_ffmpeg()
    root, output, store, engine = batch_cli
    media = root / "clip.wav"
    _wav(media)

    first = run_batch([str(media)], store=store, formats=["json"],
                      output_dir=str(output), model="tiny", device="cpu")
    assert first.items[0].resumed is False
    assert first.items[0].status == "succeeded"
    assert first.items[0].outputs
    assert engine.calls == 1

    second = run_batch([str(media)], store=store, resume=True, formats=["json"],
                       output_dir=str(output), model="tiny", device="cpu")
    item = second.items[0]

    assert item.status == "succeeded"
    assert item.resumed is True
    assert item.job_id == first.items[0].job_id
    assert item.outputs == first.items[0].outputs
    assert engine.calls == 1
    assert len(store.list(limit=100)) == 1
