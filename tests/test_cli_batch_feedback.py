"""G5A RED — CLI batch per-item feedback arrives as each item finishes (UX-001).

The defect this file pins, from the audit's UX-001:

    ... CLI batches conceal completed earlier items while later inputs run.

The audit's source note is precise: ``cli.py`` waits for ``run_batch`` to return
(local ``_cmd_batch``, the ``report = run_batch(...)`` call), and only *then*
replays every per-item line; ``core/batch.py`` runs its items synchronously and
returns a completed report. So an operator watching a two-item batch sees nothing
until the last (slow) item has finished, even though the store already holds the
first item's DONE row.

What these tests demand, matching the audit's fix path ("Batch emits an item
outcome as each item finishes, rather than replaying the entire report only at the
end"):

1. **Non-quiet** — a completed item's line is emitted **before** a later item's
   work even begins. Proven by holding the second item's engine on an event:
   after the first item has finished and the second has *started* (the engine's
   ``entered`` handshake), the first item's outcome must already be on the
   stream, and the second item's must not be.
2. **Final summary retained** — the ``batch: N total, ...`` line is still printed
   exactly once, after all items, so existing consumers keep their contract.
3. **Quiet** — per-item lines stay suppressed (the UX-003 diagnostics question is
   explicitly out of scope here; only the success/progress chatter is pinned as
   suppressed).

This suite is EDIT-ONLY and RED-first: the product is untouched, so the timing
assertion is expected to FAIL against the current ``cli.py`` (which prints the
first item's line only after the whole batch returns).

The batch path is driven through the **real** ``cli.main`` -> ``run_batch`` ->
``submit_request`` loop. Only the model boundary is faked: each item's engine is
a per-source stand-in so the test can hold item two while item one completes. No
inference, no network, no download. Local sources name real (empty) files.
"""

from __future__ import annotations

import threading

import pytest

from textflowkit import cli as cli_mod
from textflowkit.core import pipeline
from textflowkit.core.jobs import MemoryJobStore, reset_default_store
from textflowkit.core.model import Segment, Transcript


class _Ref:
    kind = "url"
    platform = "youtube"

    def __init__(self, location: str) -> None:
        self.location = location


@pytest.fixture(autouse=True)
def _clean_store():
    reset_default_store()
    yield
    reset_default_store()


def _wire_two_item_batch(monkeypatch, tmp_path, *, entered_second, release_second):
    """Real batch loop; only the engine and the acquire/decode boundaries faked.

    Item one completes at once. Item two's engine sets ``entered_second`` and
    blocks on ``release_second``, giving the test a provable window in which item
    one is finished and item two is mid-run.

    Batch items run strictly one after another, so the engine built for the
    second item is the second engine the pipeline asks for. ``get_engine`` is
    called once per item, immediately before that item's ``transcribing`` stage,
    so counting calls is a reliable hold key with no global flag.
    """
    media = tmp_path / "media.bin"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")

    calls = {"n": 0}

    class Engine:
        def __init__(self, ordinal: int) -> None:
            self._ordinal = ordinal

        def transcribe(self, audio_path, language=None):
            if self._ordinal == 2:
                entered_second.set()
                release_second.wait(timeout=10)
            return Transcript(
                source=f"item-{self._ordinal}", language="en", platform="youtube",
                segments=[Segment(0.0, 1.0, f"text for item {self._ordinal}")],
            )

    def make_engine(name="whisper", **kwargs):
        calls["n"] += 1
        return Engine(calls["n"])

    monkeypatch.setattr(pipeline, "get_engine", make_engine)

    def fetch(ref, **_k):
        return media

    def extract(*_a, **_k):
        return audio

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: _Ref(value))
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)


# --- the timing assertion: item one's outcome precedes item two's work --------


def test_completed_first_item_is_reported_before_the_second_item_runs(
    monkeypatch, capfd, tmp_path
):
    """The batch must not hold item one's line until the whole run returns.

    Item two is held mid-transcription; the assertion reads the stream in that
    window. Item one's line must already be there (its work finished and its row
    is DONE), and item two's successful line must not be — item two has not
    finished.
    """
    store = MemoryJobStore()
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)
    (tmp_path / "one.wav").write_bytes(b"")
    (tmp_path / "two.wav").write_bytes(b"")
    entered_second, release_second = threading.Event(), threading.Event()
    _wire_two_item_batch(
        monkeypatch, tmp_path,
        entered_second=entered_second, release_second=release_second,
    )

    holder: dict = {}

    def target():
        try:
            holder["rc"] = cli_mod.main([
                "batch", str(tmp_path / "one.wav"), str(tmp_path / "two.wav"),
                "--formats", "json", "--output-dir", str(tmp_path),
            ])
        except BaseException as exc:  # noqa: BLE001
            holder["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        assert entered_second.wait(timeout=10), "the second item never started"
        # `capfd.readouterr()` is consuming, and this is the only read before the
        # run ends: `seen` holds everything item one has printed so far. The
        # product flushes its in-flight item line (prompt feedback is the point),
        # so the line has reached the descriptor by the time item two is held.
        seen = capfd.readouterr().out

        assert "one.wav" in seen, (
            "the completed first item was not reported while the second item was "
            f"still running; a batch conceals finished work (UX-001). Saw: {seen!r}"
        )
        assert "two.wav" not in seen, (
            f"the second item was reported before it finished: {seen!r}"
        )
    finally:
        release_second.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert holder.get("exc") is None, holder.get("exc")


def _fake_batch_run(monkeypatch, items):
    """Stand in for the real ``run_batch`` *including its callback contract*.

    ``run_batch`` signals each finished item through the ``on_item_complete``
    keyword the CLI passes it, then returns the report. A stub that returned the
    report and never invoked the callback would be testing a ``run_batch`` that
    does not exist, so this fires the callback once per item before returning.
    Quiet runs pass ``None``; those items must not produce a per-item line.
    """
    from textflowkit.core.batch import BatchReport

    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())

    def fake_run_batch(sources, *, store, on_item_complete=None, **kwargs):
        report = BatchReport()
        for item in items:
            report.items.append(item)
            if on_item_complete is not None:
                on_item_complete(item)
        return report

    monkeypatch.setattr(cli_mod, "run_batch", fake_run_batch)


def test_batch_still_prints_the_final_summary_once(monkeypatch, capsys, tmp_path):
    """The ``batch: N total, ...`` summary line survives incremental reporting."""
    from textflowkit.core.batch import BatchItem

    _fake_batch_run(monkeypatch, [
        BatchItem(source="one", status="succeeded", outputs=["one.json"]),
        BatchItem(source="two", status="succeeded", outputs=["two.json"]),
    ])

    rc = cli_mod.main(["batch", "one", "two", "--output-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("batch: 2 total, 2 succeeded, 0 failed, 0 skipped") == 1, out
    assert "succeeded one" in out
    assert "succeeded two" in out


def test_quiet_batch_suppresses_per_item_lines(monkeypatch, capsys, tmp_path):
    """Quiet keeps the summary but drops the per-item chatter (UX-001 scope).

    UX-003 (quiet mode losing failure *reasons*) is explicitly deferred and is
    NOT asserted here; this test pins only the non-quiet success chatter as
    suppressed under ``--quiet``.
    """
    from textflowkit.core.batch import BatchItem

    _fake_batch_run(monkeypatch, [
        BatchItem(source="one", status="succeeded", outputs=["one.json"]),
    ])

    rc = cli_mod.main(["batch", "one", "--quiet", "--output-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "batch: 1 total, 1 succeeded, 0 failed, 0 skipped" in out
    assert "succeeded one" not in out
