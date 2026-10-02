"""G5A RED — in-flight CLI stage feedback (audit UX-001).

The defect this file pins, from the audit's UX-001 (Major):

    A normal local transcription can be expensive and slow. Without a stage
    message, operators cannot distinguish useful work from a stalled process.

The audit's evidence is a *held-stage* probe: the real runner was stopped at
``transcribing`` and both the CLI's stdout and stderr were exactly empty; only
after the run released did the ordinary completion summary appear. The stage the
store already records (``core/runner.py`` writes ``progress`` through
``_progress`` at each stage boundary) never reaches the person at the terminal.

These tests demand the shape the audit's fix path names:

- **non-quiet transcribe** writes a *useful stage line to stderr* **while the
  stage is still running** — proven by holding the engine on an event and
  releasing it only after the assertion, so no sleep and no timing assumption is
  involved;
- **stdout stays clean** — the transcript or the written paths remain the only
  thing on stdout, so a ``--stdout`` consumer is unaffected;
- **quiet** suppresses progress chatter (stage lines included) while keeping the
  final result;
- **a resumed run that reuses a finished stage does not announce that stage** — a
  message about work that never ran would be a lie.

This suite is EDIT-ONLY and RED-first: the product is deliberately untouched, so
each behavioural test below is expected to FAIL against the current ``cli.py``
(which emits no stage line at all). What the fix must add is exactly the plumbing
the audit asks for — an optional ephemeral stage observer threaded CLI ->
submission -> runner, defaulting to no observer for API/MCP — with **no new
service, no background poll thread, and no change to the stored ``progress``
meaning.**

Only the model boundary is faked. The real ``cli.main`` argument handling, the
real ``submit_request``, the real ``run_job``, the real stage transitions and the
real store all run; only the engine is held. Local sources name real (empty)
files because the real ``SubmissionRequest`` refuses a missing one.

Mid-run stream reads use ``capfd`` (the real file descriptors), not ``capsys``:
the CLI's writes happen on a worker thread the test spawns, and ``capsys`` swaps
``sys.stdout``/``sys.stderr`` in-process objects that another thread would not
necessarily be observed writing through. ``capfd`` captures the descriptors
themselves, so a read mid-run sees exactly what a terminal would.

``capfd.readouterr()`` is *consuming*: each call returns only the bytes written
since the last call. So the mid-run read has to be accumulated by hand, and any
assertion made *after* the run must account for what the mid-run read already
took. There is no ``capfd.out``/``capfd.err`` attribute to read mid-run - pytest
exposes captured output only through ``readouterr()`` - so the helper below
reads it and keeps the consumed text, and the tests join it with the final read.
"""

from __future__ import annotations

import threading

import pytest

from textflowkit import cli as cli_mod
from textflowkit.core import pipeline
from textflowkit.core.checkpoint import CheckpointRecord
from textflowkit.core.jobs import JobState, MemoryJobStore, reset_default_store
from textflowkit.core.model import Segment, Transcript
from textflowkit.render import render

SOURCE = "https://example.com/g5a"


class _Ref:
    platform = "youtube"
    kind = "url"
    location = SOURCE


@pytest.fixture(autouse=True)
def _clean_store():
    """A fresh process-wide store per test, and no cached one left behind.

    ``cli.main`` reaches the process-wide ``get_default_store`` directly, so a
    test that lets the real store build must tear it down for the next file.
    """
    reset_default_store()
    yield
    reset_default_store()


def _wire_held_engine(monkeypatch, tmp_path, *, entered, release=None):
    """Stub only the pipeline's external boundaries; hold the engine on an event.

    ``entered`` is set the instant the engine is about to run and the engine
    blocks on ``release`` (when given), so a test can make a positive assertion
    *while a stage is genuinely in flight* — a handshake, not a sleep. Every
    stage boundary, the checkpointing and the store writes are the real code
    path.
    """
    media = tmp_path / "media.bin"
    audio = tmp_path / "audio.wav"
    media.write_bytes(b"media")
    audio.write_bytes(b"audio")
    transcript = Transcript(
        source=SOURCE, language="en", platform="youtube",
        segments=[Segment(0.0, 1.0, "hello from the probe")],
    )

    class Engine:
        def transcribe(self, audio_path, language=None):
            entered.set()
            if release is not None:
                release.wait(timeout=10)
            return transcript

    def fetch(*_a, **_k):
        return media

    def extract(*_a, **_k):
        return audio

    monkeypatch.setattr(pipeline, "resolve_source", lambda value: _Ref())
    monkeypatch.setattr(pipeline, "require_tool", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_media", fetch)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: Engine())
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))
    return transcript


def _stub_cli(monkeypatch, store):
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)


def _run_in_thread(argv, result: dict) -> threading.Thread:
    """Run ``cli.main`` on a worker thread and record its outcome.

    The real submission/runner path is synchronous, so the engine hold gives the
    main test thread a window to read the streams while the CLI is mid-run.
    """

    def target():
        try:
            result["rc"] = cli_mod.main(argv)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            result["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


class _Captured:
    """Accumulate ``capfd`` output across several ``readouterr()`` calls.

    ``capfd.readouterr()`` is consuming: it returns the output since the last
    call and clears the buffer. A test that reads mid-run must keep that text,
    because the run's later output is all a second read returns - the earlier
    bytes are gone. This holds both halves so a final assertion sees the whole
    stream, not just what arrived after the mid-run read.
    """

    def __init__(self, capfd) -> None:
        self._capfd = capfd
        self.out = ""
        self.err = ""

    def read(self) -> "tuple[str, str]":
        """Consume the not-yet-read output and return ``(out, err)`` so far.

        The cumulative totals are returned, so a mid-run assertion and the final
        one measure the same whole stream.
        """
        captured = self._capfd.readouterr()
        self.out += captured.out
        self.err += captured.err
        return self.out, self.err


# --- UX-001: a held stage must be visible before the run completes ------------


def test_nonquiet_transcribe_announces_a_useful_stage_before_completion(
    monkeypatch, capfd, tmp_path
):
    """`Transcribing ...` reaches stderr while the engine is still running.

    The engine is held on an event; the stderr read happens *before* release, so
    the message proven is one a stalled operator would have seen mid-run — not a
    completion summary replayed at the end.
    """
    store = MemoryJobStore()
    _stub_cli(monkeypatch, store)
    (tmp_path / "clip.wav").write_bytes(b"")
    entered, release = threading.Event(), threading.Event()
    _wire_held_engine(monkeypatch, tmp_path, entered=entered, release=release)

    holder: dict = {}
    captured = _Captured(capfd)
    thread = _run_in_thread(
        [
            "transcribe", str(tmp_path / "clip.wav"),
            "--formats", "json", "--output-dir", str(tmp_path),
        ],
        holder,
    )
    try:
        assert entered.wait(timeout=10), "the engine never started"

        # What a watcher sees *now*, mid-stage: capfd captures the real
        # descriptors, so this read observes the worker thread's writes without
        # ending the run. It consumes, so the totals are kept for the final read.
        out_so_far, err_so_far = captured.read()

        assert err_so_far.strip(), (
            "no stage feedback reached stderr while transcription was running: "
            "an operator cannot tell real work from a stall (UX-001)"
        )
        assert "transcrib" in err_so_far.lower(), (
            f"the in-flight line does not name the stage: {err_so_far!r}"
        )
        # stdout is reserved for the result contract; it must still be empty.
        assert out_so_far == "", (
            f"stdout was not clean while the stage ran: {out_so_far!r}"
        )
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive(), "the CLI run did not finish after release"
    assert holder.get("exc") is None, holder.get("exc")
    # The run finished cleanly: nothing after release may corrupt stdout. This
    # uses the accumulated totals, so the mid-run bytes are still accounted for.
    final_out, _final_err = captured.read()
    assert "transcrib" not in final_out.lower(), (
        f"transcript text leaked the stage line onto stdout: {final_out!r}"
    )


def test_nonquiet_stdout_transcript_stays_the_only_thing_on_stdout(
    monkeypatch, capsys, tmp_path
):
    """`--stdout` output is byte-clean: the transcript and nothing else.

    Stage lines must go to stderr, never to the machine-readable stdout a
    pipeline consumes.
    """
    store = MemoryJobStore()
    _stub_cli(monkeypatch, store)
    (tmp_path / "clip.wav").write_bytes(b"")
    entered = threading.Event()
    _wire_held_engine(monkeypatch, tmp_path, entered=entered, release=None)

    rc = cli_mod.main(
        ["transcribe", str(tmp_path / "clip.wav"), "--stdout", "--stdout-format", "txt"]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    expected = render(
        Transcript(
            source=SOURCE, language="en", platform="youtube",
            segments=[Segment(0.0, 1.0, "hello from the probe")],
        ),
        "txt",
    )
    assert captured.out == expected, (
        "stdout carried more than the transcript; stage feedback must be stderr-only"
    )
    assert "hello from the probe" in captured.out


def test_quiet_transcribe_suppresses_stage_feedback(monkeypatch, capsys, tmp_path):
    """`--quiet` suppresses progress chatter, not the result.

    The help text already distinguishes quiet as suppressing progress; a stage
    line that ignored ``--quiet`` would break that contract.
    """
    store = MemoryJobStore()
    _stub_cli(monkeypatch, store)
    (tmp_path / "clip.wav").write_bytes(b"")
    entered = threading.Event()
    _wire_held_engine(monkeypatch, tmp_path, entered=entered, release=None)

    rc = cli_mod.main([
        "transcribe", str(tmp_path / "clip.wav"), "--quiet",
        "--output-dir", str(tmp_path),
    ])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert captured.out.strip(), "quiet must still print the written paths"
    assert "transcrib" not in captured.err.lower(), (
        f"quiet still emitted stage feedback: {captured.err!r}"
    )


def test_resumed_run_does_not_announce_a_reused_finished_stage(
    monkeypatch, capsys, tmp_path
):
    """A resume that reuses a finished stage must not announce that stage.

    The checkpoint already holds the transcript, so the engine never runs. A
    ``Transcribing ...`` line here would name work that did not happen — the same
    class of lie the audit's state-guidance policy forbids at the adapter layer.
    Only ``rendering`` legitimately runs.

    The job is an interrupted (ERROR) one whose checkpoint records a finished
    transcription; ``--resume`` reuses it. The source is a URL so no local
    fingerprint is involved and the match is on the request options alone.
    """
    store = MemoryJobStore()
    _stub_cli(monkeypatch, store)
    transcript = Transcript(
        source=SOURCE, language="en", platform="youtube",
        segments=[Segment(0.0, 1.0, "already done")],
    )
    request_options = {
        "formats": ["json"], "diarize": False,
        "diarizer_backend": "pyannote", "translate_to": None,
        "translator_backend": "ollama",
    }
    checkpoint = CheckpointRecord(
        source=SOURCE,
        model="small",
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=transcript.to_dict(),
        options=request_options,
    )
    job = store.create(SOURCE)
    store.update(
        job.id, state=JobState.ERROR, error="interrupted",
        checkpoint=checkpoint.to_dict(),
    )

    def _explode(*_a, **_k):  # the engine must not be consulted on a reuse
        raise AssertionError("the engine ran for a reused transcript")

    monkeypatch.setattr(pipeline, "get_engine", _explode)
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path))

    rc = cli_mod.main([
        "transcribe", SOURCE, "--resume", "--formats", "json",
        "--output-dir", str(tmp_path),
    ])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "transcrib" not in captured.err.lower(), (
        f"a resumed run announced a stage whose work was reused: {captured.err!r}"
    )
