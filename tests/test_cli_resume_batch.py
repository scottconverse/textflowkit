"""CLI parsing and dispatch for resume and batch."""
from __future__ import annotations

from textflowkit import cli as cli_mod


def test_transcribe_resume_flag_parses():
    parser = cli_mod._build_parser()

    args = parser.parse_args(["transcribe", "src.wav", "--resume"])

    assert args.command == "transcribe"
    assert args.resume is True


def test_batch_is_listed_in_help():
    assert "batch" in cli_mod._build_parser().format_help()


def test_batch_parser_accepts_multiple_sources_and_resume():
    parser = cli_mod._build_parser()

    args = parser.parse_args(["batch", "one", "two", "three", "--resume"])

    assert args.command == "batch"
    assert args.sources == ["one", "two", "three"]
    assert args.resume is True


def test_cmd_batch_dispatches_report_and_returns_failure_on_failed_item(
    monkeypatch, capsys
):
    from textflowkit.core.batch import BatchItem, BatchReport
    from textflowkit.core.jobs import MemoryJobStore

    store = MemoryJobStore()
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)
    captured = {}

    def fake_run_batch(sources, *, store, **kwargs):
        captured["sources"] = sources
        captured["store"] = store
        captured["kwargs"] = kwargs
        return BatchReport(items=[
            BatchItem(source="one", status="succeeded", outputs=["one.json"]),
            BatchItem(source="two", status="failed", error="bad source"),
        ])

    monkeypatch.setattr(cli_mod, "run_batch", fake_run_batch)

    rc = cli_mod.main(["batch", "one", "two", "--formats", "json,srt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert captured["sources"] == ["one", "two"]
    assert captured["store"] is store
    assert captured["kwargs"]["formats"] == ["json", "srt"]
    assert captured["kwargs"]["resume"] is False
    assert "succeeded one" in out
    assert "failed    two - bad source" in out
    assert "batch: 2 total, 1 succeeded, 1 failed, 0 skipped" in out


def test_cmd_batch_returns_success_when_no_failures(monkeypatch, capsys):
    from textflowkit.core.batch import BatchItem, BatchReport
    from textflowkit.core.jobs import MemoryJobStore

    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())

    def fake_run_batch(sources, *, store, **kwargs):
        return BatchReport(items=[
            BatchItem(source="one", status="succeeded", outputs=["one.json"]),
            BatchItem(source="two", status="skipped", error="cancelled"),
        ])

    monkeypatch.setattr(cli_mod, "run_batch", fake_run_batch)

    rc = cli_mod.main(["batch", "one", "two", "--quiet"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "batch: 2 total, 1 succeeded, 0 failed, 1 skipped" in out
    assert "succeeded" not in out.split("batch:", 1)[0]

