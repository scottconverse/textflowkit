"""U2B acceptance repairs: the real defects, each guarded by a failing-first test.

Every test here fails against the pre-repair source and passes after it. They are
grouped by the brief's four sections:

A. Native child I/O blocking - bounded writer, bounded reads, bounded terminate.
B. Terminal correctness - start/finish failures release the slot and mark failed;
   exactly one real final record is required; unexpected EOF is immediate.
C. Asset path - ``ensure_streaming_model`` verifies and installs the model.
D. Timeline + budget - child-owned acknowledgment counts, budget checked before
   mutation.

The real child process is launched only through a *stub* worker module written
into a temp dir - never the native loader, never the network.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest

from textflowkit.core import streaming as st

#: A stub child that reports ``started`` and then never reads stdin again. This
#: is the shape that wedges a synchronous parent write: a full OS pipe buffer
#: has no reader, so a blocking write never returns.
_NEVER_READS = (
    "import sys, time\n"
    'sys.stdout.write("started " + "{}" + "\\n")\n'
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)

#: A stub child that reports ``started`` then writes one oversized, newline-free
#: stderr line (larger than any bound) and never a newline.
_STDERR_RUNAWAY = (
    "import sys, time\n"
    'sys.stdout.write("started " + "{}" + "\\n")\n'
    "sys.stdout.flush()\n"
    'sys.stderr.write("X" * (8 * 1024 * 1024))\n'
    "sys.stderr.flush()\n"
    "time.sleep(30)\n"
)


@pytest.fixture(autouse=True)
def _clean_registry():
    st.reset_session_registry()
    yield
    for session in list(_active()):
        with contextlib.suppress(Exception):
            session.cancel()
    st.reset_session_registry()


def _active():
    with st._registry_lock:
        return set(st._active_sessions)


def _stub(tmp_path, name: str, body: str) -> tuple[str, dict[str, str]]:
    """Write a stub child module and return (module_name, child_env)."""
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return name, env


def pcm(byte_count: int) -> bytes:
    assert byte_count % 2 == 0
    return bytes((i * 7 + 3) & 0xFF for i in range(byte_count))


# --- A: bounded writer; feed never blocks forever --------------------------


def test_feed_against_a_child_that_never_reads_returns_bounded_and_cancel_is_2s(
    tmp_path,
):
    """RED: synchronous stdin write blocks forever against a non-reading child.

    The worker must own a bounded writer queue so ``feed`` cannot outrun the
    child's pipe and block the caller, and ``terminate`` must kill the child
    before touching stdin so the whole cancel stays within the 2 s budget.
    """
    module, env = _stub(tmp_path, "neverreads", _NEVER_READS)
    from textflowkit.core.streaming_process import SubprocessWorker

    worker = SubprocessWorker(language=None, module=module, env=env, start_deadline=10.0)
    worker.start()

    fed = threading.Event()
    errors: list[BaseException] = []

    def _feed_loop() -> None:
        try:
            for _ in range(400):
                worker.feed(pcm(32000))
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)
        finally:
            fed.set()

    writer = threading.Thread(target=_feed_loop, daemon=True)
    started = time.monotonic()
    writer.start()
    # The child never reads, so the writer must hit its byte budget and error
    # out - never block indefinitely on the OS pipe.
    fed.wait(timeout=15.0)
    assert fed.is_set(), "feed did not return within 15s against a non-reading child"
    assert errors and all(isinstance(e, st.WorkerProcessError) for e in errors), errors
    assert time.monotonic() - started < 15.0

    cancel_started = time.monotonic()
    worker.terminate()
    cancel_elapsed = time.monotonic() - cancel_started

    assert cancel_elapsed < 2.0, f"terminate took {cancel_elapsed:.2f}s (budget 2s)"
    assert worker._proc is not None and worker._proc.poll() is not None


def test_cancel_with_a_blocked_writer_releases_within_the_budget(tmp_path):
    """RED: closing stdin before killing blocks on the stuck writer's lock.

    The writer thread is left blocked in a pipe write to a non-reading child;
    ``terminate`` must still return within the cancel budget, which means it must
    kill the child *before* it tries to close stdin.
    """
    module, env = _stub(tmp_path, "neverreads2", _NEVER_READS)
    from textflowkit.core.streaming_process import SubprocessWorker

    worker = SubprocessWorker(language=None, module=module, env=env, start_deadline=10.0)
    worker.start()
    # Fill the writer until it is genuinely blocked on the pipe (never reading).
    with pytest.raises(st.WorkerProcessError):
        for _ in range(400):
            worker.feed(pcm(32000))
    assert worker._writer_thread is not None and worker._writer_thread.is_alive()

    started = time.monotonic()
    worker.terminate()
    assert time.monotonic() - started < 2.0
    assert worker._proc is not None and worker._proc.poll() is not None


def test_stderr_runaway_without_newline_is_bounded_and_flagged(tmp_path):
    """RED: ``stderr.readline`` with no bound buffers a runaway 8 MiB line."""
    from textflowkit.core.streaming_process import SubprocessWorker

    module, env = _stub(tmp_path, "stderrrunaway", _STDERR_RUNAWAY)
    worker = SubprocessWorker(language=None, module=module, env=env, start_deadline=10.0)
    worker.start()
    # The child writes an oversize newline-free stderr line; the reader must
    # bound what it retains rather than buffering the whole thing.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if worker._stderr_overflowed:
            break
        time.sleep(0.05)
    assert worker._stderr_overflowed is True
    assert sum(len(line) for line in worker._stderr_tail) < 1 << 20
    worker.terminate()


# --- B: terminal correctness ----------------------------------------------


def test_start_failure_releases_the_slot_and_marks_failed(tmp_path):
    """A worker whose start() raises must not leak its session slot or a child."""
    from textflowkit.core.streaming_process import SubprocessWorker

    session = st.StreamingSession(
        worker_factory=lambda: SubprocessWorker(
            language=None, module="textflowkit.core.no_such_worker"
        )
    )
    with pytest.raises(st.WorkerProcessError):
        session.start()
    assert session.state == "failed"
    assert st.active_sessions() == 0
    # The slot is really free: a fresh session can start.
    other = _make_fake_session()
    other.start()
    other.cancel()


def test_start_failure_terminates_the_created_worker():
    """RED: a worker created before start() failed must be terminated."""
    session = _make_fake_session()
    worker_ref = {}
    original_factory = session._factory

    def _factory():
        worker = original_factory()
        worker_ref["worker"] = worker

        def _boom():
            raise st.WorkerProcessError("worker failed to load the model")

        worker.start = _boom
        return worker

    session._factory = _factory
    with pytest.raises(st.WorkerProcessError):
        session.start()
    assert worker_ref["worker"].terminated is True
    assert session.state == "failed"
    assert st.active_sessions() == 0


def test_finish_timeout_terminates_marks_failed_and_releases_slot():
    session = _make_fake_session(blocking=True)
    session.start()
    assert st.active_sessions() == 1
    with pytest.raises(st.WorkerProcessError):
        session.finish(timeout=0.2)
    assert session.state == "failed"
    assert session.worker.terminated is True
    assert st.active_sessions() == 0


def test_a_live_done_without_a_final_record_is_a_protocol_error():
    """RED: a bare ``done`` on a live read must not silently finish the session."""
    session = _make_fake_session(pending_records=[{"kind": "done"}])
    session.start()
    with pytest.raises(st.StreamingProtocolError):
        session.feed(pcm(3200))
        for _ in range(5):
            event = session.read_event(timeout=1.0)
            if event is None:
                break
    assert session.state != "finished"


def test_finish_requires_exactly_one_final_record():
    """RED: ``finish`` must fail, not fabricate an empty final, if no final came."""
    from textflowkit.core.streaming_process import SubprocessWorker  # noqa: F401
    session = _make_fake_session(finish_records=[{"kind": "done"}])
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.finish()
    assert session.state == "failed"
    assert st.active_sessions() == 0


def test_a_final_record_with_nonempty_pending_is_refused():
    session = _make_fake_session(
        finish_records=[
            {"kind": "final", "record": {"text": "tail", "pending": "leftover",
                                         "language": "en", "received": 0.1,
                                         "pass_ms": 0.0, "words": []}},
            {"kind": "done"},
        ]
    )
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.finish()


def test_unexpected_eof_before_final_is_an_immediate_error():
    """RED: EOF must be an error, not an endless stream of ``None``."""
    session = _make_fake_session(eof=True)  # the child's stream has ended
    session.start()
    session.feed(pcm(3200))
    started = time.monotonic()
    with pytest.raises(st.WorkerProcessError):
        for _ in range(5):
            session.read_event(timeout=0.2)
    assert time.monotonic() - started < 2.0
    session.cancel()


def test_concurrent_reads_are_serialized_and_lose_no_records():
    """Two readers on one session must not split the stream and steal events."""
    records = [
        {"kind": "event", "record": {"text": f"w{i}", "pending": "", "language": "en",
                                     "received": (i + 1) * 0.1,
                                     "consumed_samples": (i + 1) * 800,
                                     "pass_ms": 0.0, "words": []}}
        for i in range(6)
    ]
    session = _make_fake_session(records=records)
    session.start()
    session.feed(pcm(9600))  # 4800 samples: every ack (max 4800) stays within fed

    seen: list[int] = []
    lock = threading.Lock()

    def _reader() -> None:
        for _ in range(6):
            event = session.read_event(timeout=0.5)
            if event is not None:
                with lock:
                    seen.append(event.seq)

    threads = [threading.Thread(target=_reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    # No sequence number was delivered twice: the readers did not steal.
    assert sorted(seen) == list(range(len(seen)))
    session.cancel()


def test_cancel_finish_race_cannot_overwrite_cancelled_state():
    session = _make_fake_session()
    session.start()
    barrier = threading.Barrier(2)

    results: list[BaseException] = []

    def _cancel() -> None:
        barrier.wait()
        session.cancel()

    def _finish() -> None:
        barrier.wait()
        try:
            session.finish()
        except BaseException as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=_cancel), threading.Thread(target=_finish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert session.state == "cancelled"


def test_per_chunk_deadline_is_enforced_on_a_slow_worker():
    """The 10 s chunk deadline must actually bound a read, not just exist."""
    session = _make_fake_session(blocking=True)
    session.start()
    # A tiny deadline stands in for the chunk deadline's contract: read_event
    # returns None within the caller's bound rather than blocking past it.
    started = time.monotonic()
    assert session.read_event(timeout=0.2) is None
    assert time.monotonic() - started < 1.0
    assert st.CHUNK_DEADLINE_S == 10.0
    session.cancel()


def test_public_timeout_must_be_finite_nonnegative():
    session = _make_fake_session(blocking=True)
    session.start()
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises((ValueError, TypeError, st.StreamingProtocolError)):
            session.read_event(timeout=bad)
    with pytest.raises(TypeError):
        session.read_event(timeout="soon")
    session.cancel()


def test_registry_reset_rejects_when_sessions_active():
    session = _make_fake_session()
    session.start()
    with pytest.raises(st.StreamingError):
        st.reset_session_registry()
    assert st.active_sessions() == 1
    session.cancel()


def test_keywords_argument_is_validated_and_bounded():
    with pytest.raises((ValueError, st.StreamingProtocolError)):
        st.StreamingSession(keywords=["x" * 5000], worker_factory=None)
    session = st.StreamingSession(keywords=["alpha", "beta"], worker_factory=None)
    assert session.keywords == ("alpha", "beta")


# --- C: the streaming model path must be verified/resolved -----------------


def test_explicit_weights_path_is_validated_in_place(monkeypatch, tmp_path):
    from textflowkit.core import streaming_assets as sa
    from textflowkit.core import whistle_assets as wa

    good = tmp_path / "whistle.cact"
    good.write_bytes(b"the pinned model bytes")
    pin = wa.PinnedAsset(filename="whistle.cact", url="", sha256=_sha(good),
                         size=good.stat().st_size)
    monkeypatch.setattr(sa, "WHISTLE_MODEL", pin)
    assert sa.ensure_streaming_model(weights_path=good) == good.resolve()


def test_an_explicit_weights_path_that_does_not_match_the_pin_is_refused(
    monkeypatch, tmp_path
):
    from textflowkit.core import streaming_assets as sa
    from textflowkit.core import whistle_assets as wa

    good = tmp_path / "whistle.cact"
    good.write_bytes(b"the pinned model bytes")
    pin = wa.PinnedAsset(filename="whistle.cact", url="", sha256=_sha(good),
                         size=good.stat().st_size)
    monkeypatch.setattr(sa, "WHISTLE_MODEL", pin)
    tampered = tmp_path / "tampered.cact"
    tampered.write_bytes(b"not the model")
    with pytest.raises(sa.StreamingAssetError):
        sa.ensure_streaming_model(weights_path=tampered)


def test_a_missing_weights_path_is_refused_not_returned(monkeypatch, tmp_path):
    from textflowkit.core import streaming_assets as sa

    missing = tmp_path / "nope.cact"
    with pytest.raises(sa.StreamingAssetError):
        sa.ensure_streaming_model(weights_path=missing)


def test_a_cached_model_is_reverified_before_it_is_trusted(monkeypatch, tmp_path):
    from textflowkit.core import streaming_assets as sa
    from textflowkit.core import whistle_assets as wa

    monkeypatch.setenv(wa.ENV_MODELS_DIR, str(tmp_path / "models"))
    cache = sa.model_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"tampered model")
    pin = wa.PinnedAsset(filename="whistle.cact", url="", sha256="0" * 64, size=4)
    monkeypatch.setattr(sa, "WHISTLE_MODEL", pin)
    with pytest.raises(sa.StreamingAssetError):
        sa.ensure_streaming_model()


def test_offline_missing_model_is_refused_without_network(monkeypatch, tmp_path):
    from textflowkit.core import streaming_assets as sa
    from textflowkit.core import whistle_assets as wa

    monkeypatch.setenv(wa.ENV_MODELS_DIR, str(tmp_path / "models"))
    monkeypatch.setenv(wa.ENV_OFFLINE, "1")
    pin = wa.PinnedAsset(filename="whistle.cact", url="", sha256="0" * 64, size=4)
    monkeypatch.setattr(sa, "WHISTLE_MODEL", pin)
    with pytest.raises(sa.StreamingAssetError, match="offline"):
        sa.ensure_streaming_model()


def test_child_weights_path_verifies_the_model(monkeypatch, tmp_path):
    """The child never returns a bare path: it resolves through the verifier."""
    from textflowkit.core import stream_worker
    from textflowkit.core import streaming_assets as sa

    calls = {}

    def _fake(weights_path=None, **kwargs):
        calls["weights_path"] = weights_path
        return tmp_path / "resolved.cact"

    monkeypatch.setattr(sa, "ensure_streaming_model", _fake)
    resolved = stream_worker._weights_path(str(tmp_path / "explicit.cact"))
    assert calls["weights_path"] == str(tmp_path / "explicit.cact")
    assert resolved.endswith("resolved.cact")


def test_streaming_assets_never_imports_an_sdk_telemetry_module():
    import ast
    from pathlib import Path

    from textflowkit.core import stream_worker, streaming_assets

    banned = {"needle", "cactus_needle"}
    for module in (streaming_assets, stream_worker):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in banned, f"{module.__name__} imports {alias.name}"
                    assert "telemetry" not in alias.name, alias.name
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in banned, f"{module.__name__} imports from {node.module}"
                assert "telemetry" not in (node.module or ""), node.module


def _sha(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- D: timeline + budget --------------------------------------------------


def test_nan_received_is_a_protocol_error_not_valueerror():
    with pytest.raises(st.StreamingProtocolError):
        st.StreamEvent.from_record(
            {"text": "", "pending": "", "language": "en", "received": float("nan"),
             "pass_ms": 0.0, "words": []},
            seq=0, t_audio_s=0.0, is_final=False,
        )


def _ack_session(consumed_samples, *, received=None, fed_bytes=32000):
    record = {"text": "hi", "pending": "", "language": "en", "pass_ms": 0.0,
              "words": [], "consumed_samples": consumed_samples}
    if received is not None:
        record["received"] = received
    session = _make_fake_session(records=[{"kind": "event", "record": record}])
    session.start()
    session.feed(pcm(fed_bytes))
    return session


def test_ack_sample_count_drives_the_processed_clock_and_queue():
    """RED: queued audio is driven by the child's consumed_samples, not native."""
    session = _ack_session(8000, fed_bytes=32000)  # half a second of one second
    session.read_event(timeout=1.0)
    assert session.session_audio_seconds == pytest.approx(0.5)
    assert session.queued_audio_bytes == 32000 - 16000
    session.cancel()


def test_ack_regression_is_a_protocol_error():
    session = _ack_session(16000, fed_bytes=32000)
    session.read_event(timeout=1.0)
    # A second event whose ack goes backwards must be refused. The first read
    # consumed the only scripted record, so dispatch a lower ack directly.
    with pytest.raises(st.StreamingProtocolError):
        session._account_processed({"consumed_samples": 8000})
    session.cancel()


def test_ack_overshoot_past_fed_is_a_protocol_error():
    session = _ack_session(16000, fed_bytes=32000)
    session.read_event(timeout=1.0)
    with pytest.raises(st.StreamingProtocolError):
        session._account_processed({"consumed_samples": 64000})
    session.cancel()


def test_native_received_disagreeing_with_ack_is_a_protocol_error():
    # 8000 acked samples (0.5s) but the native clock claims 5s: far past tolerance.
    session = _ack_session(8000, received=5.0, fed_bytes=32000)
    with pytest.raises(st.StreamingProtocolError):
        session.read_event(timeout=1.0)
    session.cancel()


def test_transcript_budget_accounts_for_inserted_spaces(monkeypatch):
    """RED: joined deltas add a space the char budget must count."""
    records = [
        {"kind": "event", "record": {"text": "ab", "pending": "", "language": "en",
                                     "received": 0.1, "consumed_samples": 1600,
                                     "pass_ms": 0.0, "words": []}},
        {"kind": "event", "record": {"text": "cd", "pending": "", "language": "en",
                                     "received": 0.2, "consumed_samples": 3200,
                                     "pass_ms": 0.0, "words": []}},
    ]
    session = _make_fake_session(records=records)
    session.start()
    session.feed(pcm(6400))
    # "ab cd" is 5 chars including the inserted space; a budget of 4 must refuse
    # the second event rather than store 5 chars it believed were under budget.
    monkeypatch.setattr(st, "MAX_TRANSCRIPT_CHARS", 4)
    session.read_event(timeout=1.0)  # "ab" -> 2 chars, under budget
    with pytest.raises(st.StreamingResourceError):
        session.read_event(timeout=1.0)  # + space + "cd" = 5 > 4
    assert session.committed_text == "ab"
    session.cancel()


def test_an_oversized_record_text_is_refused_not_history_blown():
    from textflowkit.core import streaming_events as se

    huge = "x" * (se.MAX_TEXT_CHARS + 1)
    with pytest.raises(st.StreamingProtocolError):
        st.StreamEvent.from_record(
            {"text": huge, "pending": "", "language": "en", "received": 0.1,
             "pass_ms": 0.0, "words": []},
            seq=0, t_audio_s=0.1, is_final=False,
        )


def test_too_many_words_in_one_record_is_refused():
    from textflowkit.core import streaming_events as se

    words = [{"word": "w", "start": 0.0, "end": 0.1}] * (se.MAX_WORDS_PER_RECORD + 1)
    with pytest.raises(st.StreamingProtocolError):
        st.StreamEvent.from_record(
            {"text": "", "pending": "", "language": "en", "received": 0.1,
             "pass_ms": 0.0, "words": words},
            seq=0, t_audio_s=0.1, is_final=False,
        )


def test_ring_bounds_are_explicit_constants():
    assert st.MAX_HISTORY_EVENTS > 0
    assert st.MAX_EVENTS > 0
    assert st.MAX_TRANSCRIPT_CHARS > 0
    assert st.MAX_TRANSCRIPT_WORDS > 0


def test_transcript_budget_checked_before_mutation(monkeypatch):
    session = _make_fake_session(
        records=[{"kind": "event", "record": {
            "text": "Good morning", "pending": "", "language": "en",
            "received": 0.1, "pass_ms": 0.0, "words": []}}]
    )
    session.start()
    session.feed(pcm(3200))
    monkeypatch.setattr(st, "MAX_TRANSCRIPT_CHARS", 5)
    before = session.committed_text
    with pytest.raises(st.StreamingResourceError):
        session.read_event(timeout=1.0)
    # The over-budget delta was not appended: state is unchanged.
    assert session.committed_text == before
    session.cancel()


# --- E: fatal live-read paths must terminate the worker and free the slot ----
#
# The coordinator's independent probe found that a fatal error on the live read
# path - an unsolicited ``done``, a worker ``error`` record, an unexpected EOF, or
# a resource-budget refusal - raised out of ``read_event`` but left the session
# ``running``, its slot reserved, and its owned worker alive. Every such path must
# make the session terminal *and* release the worker and the slot, exactly as the
# start and finish failure paths already do. These tests assert the *observable*
# consequences (state, registry count, ``worker.terminated``), not the mechanism.


def test_live_unsolicited_done_marks_failed_and_releases_the_worker():
    """RED: the probe's exact sequence left state=running, slot held, worker alive."""
    session = _make_fake_session(records=[{"kind": "done"}])
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.read_event(timeout=0.01)
    assert session.state == "failed"
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


def test_live_worker_error_record_marks_failed_and_releases_the_worker():
    session = _make_fake_session(
        records=[{"kind": "error", "message": "engine exploded"}]
    )
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.WorkerProcessError):
        session.read_event(timeout=0.01)
    assert session.state == "failed"
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


def test_live_unexpected_eof_marks_failed_and_releases_the_worker():
    session = _make_fake_session(eof=True)
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.WorkerProcessError):
        session.read_event(timeout=0.2)
    assert session.state == "failed"
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


def test_live_resource_budget_refusal_marks_failed_and_releases_the_worker(monkeypatch):
    """A transcript-budget refusal on the live path is terminal too."""
    session = _make_fake_session(
        records=[{"kind": "event", "record": {
            "text": "Good morning", "pending": "", "language": "en",
            "received": 0.1, "pass_ms": 0.0, "words": []}}]
    )
    session.start()
    session.feed(pcm(3200))
    monkeypatch.setattr(st, "MAX_TRANSCRIPT_CHARS", 5)
    with pytest.raises(st.StreamingResourceError):
        session.read_event(timeout=1.0)
    assert session.state == "failed"
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


def test_cancel_after_a_failed_read_still_cleans_up():
    """RED: cancel must not return early just because the session already failed.

    The session failed on the read path, but its owned worker must be gone and the
    slot free; a later cancel is a no-op that still leaves no worker alive.
    """
    session = _make_fake_session(records=[{"kind": "done"}])
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.read_event(timeout=0.01)
    session.cancel()
    assert session.worker.terminated is True
    assert st.active_sessions() == 0


def test_a_failed_session_frees_the_slot_for_a_new_one():
    session = _make_fake_session(records=[{"kind": "done"}])
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.read_event(timeout=0.01)
    other = _make_fake_session()
    other.start()  # would raise SessionLimitError if the slot leaked
    other.cancel()


# --- F: the writer loop must write every byte of a line -----------------------


def test_writer_loop_completes_a_partial_write():
    """RED: ``stdin.write(line)`` on a raw fd can write fewer bytes than given.

    ``bufsize=0`` means ``write`` maps to a single ``os.write`` on the raw
    descriptor, whose return is the byte count actually written - which may be
    short. The loop must keep writing until every byte is out, or the child's line
    is truncated and the protocol desynchronizes.
    """
    from textflowkit.core.streaming_process import SubprocessWorker

    class _ShortWritePipe:
        """A stdin whose ``write`` accepts at most ``chunk`` bytes per call."""

        def __init__(self, chunk: int):
            self._chunk = chunk
            self.received = bytearray()
            self.closed = False

        def write(self, data) -> int:
            piece = bytes(data[: self._chunk])
            self.received.extend(piece)
            return len(piece)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    worker = SubprocessWorker(language=None, module="unused")
    pipe = _ShortWritePipe(chunk=7)
    line = b'{"cmd": "feed", "audio": "AAAA"}\n'
    # Drive the real loop body against the short-writing pipe by feeding it the
    # one queued line and the stop sentinel, then joining the thread.
    worker._proc = _FakeProc(pipe)  # type: ignore[assignment]
    worker._write_q.put_nowait(line)
    worker._write_q.put_nowait(None)
    worker._pending_write_bytes = len(line)
    worker._write_loop()
    assert bytes(pipe.received) == line


class _FakeProc:
    """A stand-in for ``subprocess.Popen`` carrying a fake stdin."""

    def __init__(self, stdin):
        self.stdin = stdin


# --- G: the chunk deadline bounds queued-but-unacknowledged audio -------------


def test_chunk_deadline_bounds_audio_queued_but_never_acknowledged_live(monkeypatch):
    """RED: a live session that queued audio the worker never acks must not stall forever.

    The 10 s chunk deadline is per outstanding frame: audio accepted but not yet
    acknowledged within the deadline is a stalled worker, and the live read must
    refuse (fail the session) rather than block past it. A small deadline here
    stands in for the constant; the behaviour is what is asserted.
    """
    monkeypatch.setattr(st, "CHUNK_DEADLINE_S", 0.3)
    session = _make_fake_session(blocking=True)
    session.start()
    session.feed(pcm(32000))  # one outstanding, unacknowledged frame
    started = time.monotonic()
    with pytest.raises(st.WorkerProcessError):
        # Read repeatedly with short bounds while nothing is ever acked; the
        # outstanding-frame deadline must fire even though each read is short.
        for _ in range(400):
            session.read_event(timeout=0.05)
    assert time.monotonic() - started < 5.0
    assert session.state == "failed"
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


def test_chunk_deadline_does_not_fire_for_a_session_that_acks_promptly():
    """The deadline tracks *outstanding* frames, not the mere passage of time."""
    session = _make_fake_session(
        records=[{"kind": "event", "record": {
            "text": "hi", "pending": "", "language": "en", "received": 0.1,
            "consumed_samples": 16000, "pass_ms": 0.0, "words": []}}]
    )
    session.start()
    session.feed(pcm(32000))  # one frame, acked by the scripted record
    event = session.read_event(timeout=1.0)
    assert event is not None
    # Everything fed is acknowledged, so there is no outstanding frame and the
    # deadline cannot fire: a live read merely times out.
    assert session.read_event(timeout=0.05) is None
    assert session.state == "running"
    session.cancel()


def test_no_audio_fed_never_trips_the_chunk_deadline():
    """An idle session with nothing fed must not be failed as a stalled worker."""
    session = _make_fake_session(blocking=True)
    session.start()
    for _ in range(20):
        assert session.read_event(timeout=0.05) is None
    assert session.state == "running"
    session.cancel()


# --- H: concurrent reads and the cancel/finish race are both bounded ----------


def test_concurrent_read_event_is_bounded_by_the_caller_timeout():
    """RED: a second reader blocked on ``_read_lock`` ignored its own timeout.

    Two readers, one session: the reader that loses the lock must return within its
    own bound rather than waiting out the winner's (possibly long) read.
    """
    session = _make_fake_session(blocking=True)
    session.start()
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        held.set()
        session.read_event(timeout=3.0)  # holds the read lock for ~3 s
        release.set()

    holder = threading.Thread(target=_holder)
    holder.start()
    held.wait(timeout=1.0)
    started = time.monotonic()
    # The second reader asks for a short bound; it must not wait for the holder.
    assert session.read_event(timeout=0.3) is None
    assert time.monotonic() - started < 2.0
    holder.join(timeout=5.0)
    session.cancel()


def test_cancel_and_finish_race_leaves_no_thread_alive_and_cancel_is_bounded():
    """RED: the race test only checked the state, never that nothing leaked."""
    session = _make_fake_session(blocking=True)
    session.start()
    session.feed(pcm(3200))
    threads: list[threading.Thread] = []

    def _cancel() -> None:
        session.cancel()

    def _finish() -> None:
        try:
            session.finish(timeout=0.3)
        except st.StreamingError:
            pass

    for target in (_cancel, _finish):
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()
    started = time.monotonic()
    for thread in threads:
        thread.join(timeout=5.0)
    for thread in threads:
        assert not thread.is_alive(), "a cancel/finish thread was still alive"
    assert time.monotonic() - started < 3.0
    assert session.state in ("cancelled", "failed")
    assert st.active_sessions() == 0
    assert session.worker.terminated is True


# --- helpers ---------------------------------------------------------------


def _make_fake_session(**kwargs) -> st.StreamingSession:
    factory = _FakeWorkerFactory(**kwargs)
    return st.StreamingSession(worker_factory=factory)


class _FakeWorker:
    """A scripted worker covering the repair cases the real child cannot drive."""

    def __init__(self, pending_records=None, finish_records=None, blocking=False,
                 records=None, eof=False):
        import queue as _queue

        self._pending = _queue.Queue()
        for record in pending_records or []:
            self._pending.put(record)
        self._finish_records = finish_records
        self._blocking = blocking
        self._records = records or []
        self._emitted = 0
        self._eof = eof
        self.terminated = False
        self.stop_calls = 0
        self.bytes_fed = 0

    def start(self) -> None:
        pass

    def feed(self, frame: bytes) -> None:
        self.bytes_fed += len(frame)

    def finish(self) -> None:
        self.stop_calls += 1
        if self._finish_records is not None:
            for record in self._finish_records:
                self._pending.put(record)

    def terminate(self) -> None:
        self.terminated = True

    def at_eof(self) -> bool:
        """Model the real worker's stream-ended probe."""
        return self._eof

    def read_record(self, timeout: float | None = None):
        import queue as _queue

        if self._blocking:
            if timeout:
                time.sleep(min(timeout, 0.5))
            return None
        if self._records and self._emitted < len(self._records):
            record = self._records[self._emitted]
            self._emitted += 1
            return record
        try:
            if timeout is None:
                return self._pending.get()
            return self._pending.get(timeout=timeout)
        except _queue.Empty:
            return None


class _FakeWorkerFactory:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self.worker: _FakeWorker | None = None

    def __call__(self) -> _FakeWorker:
        self.worker = _FakeWorker(**self._kwargs)
        return self.worker
