"""Public streaming core: events, lifecycle, bounds, and the owned worker.

Everything here is deterministic and offline. The native engine is exercised
only through an *injected* worker factory, so a full test run never loads a DLL
or touches the network. The process that actually owns a native runtime
(``textflowkit.core.stream_worker``) is launched for its real Popen lifecycle
only in the one isolated test at the bottom - and that test uses a *stub*
worker module, never the native loader.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import time

import pytest

from textflowkit.core import streaming as st


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts from an empty registry and leaves none behind."""
    st.reset_session_registry()
    yield
    for session in list(st_active_sessions()):
        with contextlib.suppress(Exception):  # a teardown cancel must never fail the run
            session.cancel()
    st.reset_session_registry()


def st_active_sessions():
    with st._registry_lock:
        return set(st._active_sessions)


# --- helpers ---------------------------------------------------------------


def pcm(byte_count: int) -> bytes:
    """A valid, even, non-empty s16le frame of monotone samples."""
    assert byte_count % 2 == 0
    return bytes((i * 7 + 3) & 0xFF for i in range(byte_count))


class FakeWorker:
    """A stand-in child process. Emits the raw records the native worker would.

    The fake models the *real* worker protocol, whose read is bounded: it keeps
    a queue of pending records and ``read_record(timeout)`` returns one, or
    ``None`` if none arrives within the deadline. A ``blocking`` fake is a
    worker that has produced nothing: the read waits out the timeout and returns
    ``None`` rather than blocking the caller forever.

    The ``received`` field of each emitted record is the samples *processed* so
    far - the worker reports what it has consumed, not what was fed - so the
    session's acknowledgment accounting is exercised against honest numbers.
    """

    def __init__(self, records: list[dict], *, fail_on: int | None = None,
                 blocking: bool = False):
        self._script = records
        self._fail_on = fail_on
        self._blocking = blocking
        self._pending: queue.Queue[dict] = queue.Queue()
        self._emitted = 0
        self.started = False
        self.finished = False
        self.terminated = False
        self.feeds = 0
        self.bytes_fed = 0
        self.processed_bytes = 0
        self.stop_calls = 0
        self.rc = 0

    def start(self) -> None:
        self.started = True

    def feed(self, frame: bytes) -> None:
        self.feeds += 1
        if self._fail_on is not None and self.feeds == self._fail_on:
            raise st.WorkerProcessError("native worker crashed while feeding")
        self.bytes_fed += len(frame)
        # Feeding advances the engine's processed clock: this fake processes a
        # fed frame immediately, so every later record reports the fed duration.
        self.processed_bytes = self.bytes_fed

    def finish(self) -> None:
        self.finished = True
        self.stop_calls += 1
        self._pending.put({"kind": "final", "record": self._final_record()})
        self._pending.put({"kind": "done"})

    def terminate(self) -> None:
        self.terminated = True

    @property
    def processed_s(self) -> float:
        return self.processed_bytes / 32000.0

    def read_record(self, timeout: float | None = None):
        """The bounded read the real worker exposes: next record, or None.

        The scripted ``event`` records are emitted **on read**, one per call, not
        per feed: the engine produces a pass per chunk and the parent pulls them
        as it drains. Each carries the current processed clock, so it never
        claims more audio than has been fed. A ``blocking`` fake is silent.
        """
        if self._blocking:
            if timeout:
                time.sleep(min(timeout, 0.5))
            return None
        if self._script and self._emitted < len(self._script):
            record = self._script[self._emitted]
            self._emitted += 1
            return {"kind": "event", "record": dict(record, received=self.processed_s)}
        try:
            if timeout is None:
                return self._pending.get()
            return self._pending.get(timeout=timeout)
        except queue.Empty:
            return None

    def _final_record(self) -> dict:
        # The native stop flushes the last pending tail into committed text.
        last = self._script[-1] if self._script else {"language": "en"}
        tail = last.get("pending") or last.get("text") or ""
        return {
            "text": tail, "pending": "", "language": last.get("language", "en"),
            "received": self.processed_s, "pass_ms": 0.0,
            "words": last.get("words", []),
        }


#: Two committed passes: the first has a pending tail and no committed text,
#: the second commits "Good morning" and leaves "everyone" pending. ``received``
#: is filled in by the fake from the bytes actually fed.
_RECORDS = [
    {"text": "", "pending": "Good", "language": "en", "pass_ms": 19.9, "words": []},
    {"text": "Good morning", "pending": "everyone", "language": "en", "pass_ms": 37.9,
     "words": [{"word": "Good", "start": 0.1, "end": 0.32, "probability": 0.94},
               {"word": "morning", "start": 0.33, "end": 0.71, "probability": 0.88}]},
]


def make_session(records=_RECORDS, **kwargs):
    """A session whose native worker is the given fake, never a real process."""
    return st.StreamingSession(worker_factory=lambda: FakeWorker(list(records), **kwargs))


# --- StreamEvent: parsing and validation -----------------------------------


def test_event_parses_native_record_into_documented_schema():
    event = st.StreamEvent.from_record(
        {"text": "hi", "pending": "there", "language": "en", "received": 1.5,
         "pass_ms": 12.0, "words": [{"word": "hi", "start": 0.0, "end": 0.4,
                                     "probability": 0.9}]},
        seq=3, t_audio_s=1.5, is_final=False,
    )
    assert event.seq == 3
    assert event.committed_delta == "hi"
    assert event.pending == "there"
    assert event.language == "en"
    assert event.t_audio_s == 1.5
    assert event.pass_ms == 12.0
    assert event.is_final is False
    assert event.kind is st.StreamEventKind.TRANSCRIPT
    assert len(event.words) == 1
    assert event.words[0].text == "hi"


def test_a_non_final_event_with_a_pending_tail_is_reported_as_pending():
    event = st.StreamEvent.from_record(
        {"text": "", "pending": "tail", "language": "en", "received": 1.0, "pass_ms": 1.0,
         "words": []},
        seq=0, t_audio_s=1.0, is_final=False,
    )
    assert event.kind is st.StreamEventKind.TRANSCRIPT
    assert event.pending == "tail"
    assert event.committed_delta == ""


def test_event_is_json_serializable_plain_data():
    event = st.StreamEvent.from_record(
        {"text": "hi", "pending": "", "language": "en", "received": 1.0, "pass_ms": 1.0,
         "words": []},
        seq=0, t_audio_s=1.0, is_final=False,
    )
    payload = event.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {
        "seq", "kind", "committed_delta", "pending", "is_final", "t_audio_s",
        "language", "pass_ms", "words",
    }


def test_final_event_carries_the_whole_flushed_tail():
    event = st.StreamEvent.from_record(
        {"text": "and the tail", "pending": "", "language": "en", "received": 3.0,
         "pass_ms": 0.0, "words": [{"word": "tail", "start": 2.0, "end": 2.5,
                                    "probability": 0.7}]},
        seq=5, t_audio_s=3.0, is_final=True,
    )
    assert event.is_final is True
    assert event.kind is st.StreamEventKind.FINAL
    assert event.committed_delta == "and the tail"


@pytest.mark.parametrize("bad", [
    {"word": "w", "start": 1.0, "end": 0.5},              # end before start
    {"word": "w", "start": -0.2, "end": 0.5},             # negative start
    {"word": "w", "start": float("nan"), "end": 0.5},     # NaN
    {"word": "w", "start": 0.0, "end": float("inf")},     # infinite
])
def test_event_rejects_word_timing_that_is_not_finite_ordered_nonnegative(bad):
    bad = dict(bad, probability=0.9)
    with pytest.raises(st.StreamingProtocolError):
        st.StreamEvent.from_record(
            {"text": "x", "pending": "", "language": "en", "received": 1.0,
             "pass_ms": 1.0, "words": [bad]},
            seq=0, t_audio_s=1.0, is_final=False,
        )


def test_event_rejects_a_t_audio_s_that_disagrees_with_received():
    with pytest.raises(st.StreamingProtocolError):
        st.StreamEvent.from_record(
            {"text": "", "pending": "", "language": "en", "received": 2.0,
             "pass_ms": 1.0, "words": []},
            seq=0, t_audio_s=1.5, is_final=False,
        )


# --- lifecycle -------------------------------------------------------------


def test_context_manager_runs_start_feed_read_finish():
    session = make_session()
    with session as live:
        live.feed(pcm(3200))
        first = live.read_event(timeout=1.0)
        assert first.seq == 0
        assert first.committed_delta == ""
        second = live.read_event(timeout=1.0)
        assert second.committed_delta == "Good morning"
        assert second.t_audio_s == pytest.approx(0.1)  # audio actually fed
        assert second.pending == "everyone"
    assert session.finished is True
    assert session.final_event is not None
    assert session.final_event.is_final is True
    assert session.worker.finished is True


def test_final_event_flushes_the_tail_and_records_actual_duration():
    # The final record is the flushed tail (text carries it, pending empty);
    # it must appear as the final committed text, never left provisional and
    # never silently dropped.
    records = [
        {"text": "and then", "pending": "join", "language": "en", "received": 1.0,
         "pass_ms": 5.0, "words": []},
        {"text": "joining.", "pending": "", "language": "en", "received": 2.0,
         "pass_ms": 5.0, "words": []},
    ]
    session = make_session(records)
    session.start()
    session.feed(pcm(6400))
    session.finish()
    assert session.final_event.committed_delta == "joining."
    assert session.final_event.pending == ""
    assert session.final_event.is_final is True
    # duration is derived from samples actually fed (6400 bytes / 32000 per s)
    assert session.duration_s == pytest.approx(0.2)


def test_final_transcript_is_canonical_and_source_names_the_stream():
    session = make_session()
    session.start()
    session.feed(pcm(3200))
    session.finish()
    transcript = session.final_transcript
    assert transcript.source == f"stream:{session.id}"
    # The default fixture's flush commits the "everyone" tail.
    assert transcript.text == "Good morning everyone"
    assert transcript.duration == pytest.approx(0.1)
    assert transcript.language == "en"


def test_final_transcript_is_refused_before_finish():
    session = make_session()
    session.start()
    with pytest.raises(st.SessionStateError):
        _ = session.final_transcript
    session.cancel()


def test_seq_is_monotonic_and_committed_delta_is_emitted_exactly_once():
    session = make_session()
    session.start()
    session.feed(pcm(3200))
    session.read_event(timeout=1.0)
    session.read_event(timeout=1.0)
    session.finish()
    events = session.events_since(0)
    assert [e.seq for e in events] == [0, 1, 2]
    assert [e.committed_delta for e in events] == ["", "Good morning", "everyone"]
    assert session.committed_text == "Good morning everyone"


def test_feed_rejects_odd_length_and_empty_frames():
    session = make_session()
    session.start()
    with pytest.raises(st.StreamingProtocolError):
        session.feed(b"\x00\x01\x02")  # odd
    with pytest.raises(st.StreamingProtocolError):
        session.feed(b"")  # empty
    session.cancel()


def test_feed_rejects_oversize_frame():
    session = make_session()
    session.start()
    with pytest.raises(st.StreamingProtocolError):
        session.feed(pcm(st.MAX_CHUNK_BYTES + 2))
    session.cancel()


def test_unknown_language_is_refused_but_none_is_auto():
    with pytest.raises(st.StreamingProtocolError):
        st.StreamingSession(language="klingon",
                            worker_factory=lambda: FakeWorker([]))
    # None is fine (auto-detect); it is not validated away.
    st.StreamingSession(language=None, worker_factory=lambda: FakeWorker([]))


def test_all_seven_languages_are_accepted():
    for language in st.LANGUAGES:
        session = st.StreamingSession(language=language,
                                      worker_factory=lambda: FakeWorker([]))
        assert session.language == language


# --- input queue bound: <= 5 s, no silent drops, explicit refusal ----------


def test_audio_queue_over_5s_is_refused_not_dropped():
    session = make_session()
    session.start()
    # 5 s exactly (160 000 bytes = 5 * 32000) is the bound; each frame is 1 s.
    for _ in range(5):
        session.feed(pcm(st.MAX_CHUNK_BYTES))  # 5 x 1 s queued (worker never drains a fake)
    with pytest.raises(st.StreamingQueueFullError):
        session.feed(pcm(st.MAX_CHUNK_BYTES))  # the 6th second would exceed 5 s
    # Nothing was silently discarded: the queue is still exactly 5 s.
    assert session.queued_audio_bytes == 5 * st.MAX_CHUNK_BYTES


def test_queue_free_space_reflects_fed_bytes():
    session = make_session()
    session.start()
    assert session.queued_audio_bytes == 0
    session.feed(pcm(32000))
    assert session.queued_audio_bytes == 32000


# --- finish: the native stop runs exactly once -----------------------------


def test_finish_calls_native_stop_exactly_once():
    session = make_session()
    session.start()
    session.finish()
    assert session.worker.stop_calls == 1
    session.finish()  # idempotent: no second stop
    assert session.worker.stop_calls == 1


# --- terminal state: feeding a finished session is an error ----------------


def test_feed_after_finish_is_refused_with_the_terminal_state():
    session = make_session()
    session.start()
    session.finish()
    with pytest.raises(st.SessionStateError, match="finished"):
        session.feed(pcm(3200))


# --- cancel: bounded release of the owned worker ---------------------------


def test_cancel_terminates_and_releases_the_owned_worker_within_2s():
    session = make_session()
    session.start()
    started = time.monotonic()
    session.cancel()
    assert time.monotonic() - started < 2.0
    assert session.worker.terminated is True
    assert session.cancelled is True
    with pytest.raises(st.SessionStateError, match="cancelled"):
        session.feed(pcm(3200))


def test_cancel_after_finish_is_a_noop():
    session = make_session()
    session.start()
    session.finish()
    # finish() already released the worker; cancel must change nothing.
    assert session.worker.terminated is True
    stop_calls = session.worker.stop_calls
    session.cancel()
    assert session.finished is True
    assert session.cancelled is False
    assert session.worker.stop_calls == stop_calls


# --- worker failure is explicit --------------------------------------------


def test_a_worker_failure_surfaces_as_a_worker_process_error():
    session = make_session(fail_on=1)
    session.start()
    with pytest.raises(st.WorkerProcessError):
        session.feed(pcm(3200))


def test_bad_protocol_frame_from_worker_raises_protocol_error():
    # A record whose committed text is not a string must be refused, not coerced.
    session = make_session([{"text": 5, "pending": "", "language": "en",
                             "pass_ms": 1.0, "words": []}])
    session.start()
    session.feed(pcm(3200))
    with pytest.raises(st.StreamingProtocolError):
        session.read_event(timeout=1.0)


# --- read must not block cancel --------------------------------------------


def test_read_event_returns_none_on_timeout_and_does_not_wedge():
    # A worker that has produced nothing yet: the read times out to None rather
    # than blocking, and a cancel from another thread is still honoured.
    session = make_session([])
    session.start()
    started = time.monotonic()
    assert session.read_event(timeout=0.05) is None
    assert time.monotonic() - started < 1.0
    session.cancel()
    assert session.cancelled is True


def test_read_event_honours_timeout_against_a_blocking_worker():
    """A worker whose read blocks until data arrives must still time out.

    This is the real protocol, not the empty-generator case: the worker is
    silent, so ``read_event(timeout=...)`` has to return ``None`` within the
    deadline. A read that blocks the caller past its own timeout is the defect
    this guards.
    """
    session = make_session(blocking=True)
    session.start()
    started = time.monotonic()
    assert session.read_event(timeout=0.1) is None
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"read_event blocked for {elapsed:.2f}s past its timeout"
    session.cancel()


def test_finish_honours_its_deadline_against_a_blocking_worker():
    """finish() must not block forever when the worker never reports done."""
    session = make_session(blocking=True)
    session.start()
    started = time.monotonic()
    with pytest.raises(st.WorkerProcessError):
        session.finish(timeout=0.2)
    assert time.monotonic() - started < 1.5


# --- transcript budget: explicit resource error, never silent truncation ---


def test_transcript_budget_is_documented_and_explicit(monkeypatch):
    assert st.MAX_TRANSCRIPT_CHARS == 1_000_000
    assert st.MAX_TRANSCRIPT_WORDS == 100_000
    session = make_session()
    session.start()
    session.feed(pcm(3200))
    session.read_event(timeout=1.0)  # event 0: empty commit, does not trip the budget
    monkeypatch.setattr(st, "MAX_TRANSCRIPT_CHARS", 5)
    with pytest.raises(st.StreamingResourceError, match="budget"):
        session.read_event(timeout=1.0)  # "Good morning" (12 chars) exceeds 5


# --- session registry: bounded, configurable ceiling <= 4 ------------------


def test_only_one_concurrent_session_by_default():
    st.reset_session_registry()
    first = make_session()
    first.start()
    second = make_session()
    with pytest.raises(st.SessionLimitError):
        second.start()
    first.cancel()
    st.reset_session_registry()


def test_session_ceiling_is_configurable_up_to_four():
    st.reset_session_registry(max_sessions=4)
    sessions = [make_session() for _ in range(4)]
    for session in sessions:
        session.start()
    fifth = make_session()
    with pytest.raises(st.SessionLimitError):
        fifth.start()
    for session in sessions:
        session.cancel()
    st.reset_session_registry()


def test_ceiling_above_four_is_refused():
    with pytest.raises(ValueError):
        st.reset_session_registry(max_sessions=5)
    st.reset_session_registry()


def test_releasing_a_session_frees_a_slot():
    st.reset_session_registry(max_sessions=2)
    a = make_session()
    b = make_session()
    a.start()
    b.start()
    a.finish()
    c = make_session()
    c.start()  # a finished, so a slot is free
    b.cancel()
    c.cancel()
    st.reset_session_registry()


# --- the real owned child process (isolated; never the native loader) ------

_STUB_WORKER = r'''
import base64, json, sys


def emit(keyword, payload=None):
    sys.stdout.write(keyword + " " + json.dumps(payload or {}) + "\n")
    sys.stdout.flush()


received = 0.0
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    message = json.loads(line)
    cmd = message.get("cmd")
    if cmd == "start":
        emit("started")
    elif cmd == "feed":
        audio = base64.b64decode(message.get("audio", ""), validate=True)
        received += len(audio) / 32000.0
        emit("event", {"text": "", "pending": "tick", "language": "en",
                       "received": received, "pass_ms": 1.0, "words": []})
    elif cmd == "finish":
        emit("final", {"text": "the tail", "pending": "", "language": "en",
                       "received": received, "pass_ms": 0.0, "words": []})
        emit("done")
        break
'''


def _drain(worker, timeout: float = 5.0) -> list[dict]:
    """Read every record the worker has ready now, bounded by ``timeout``.

    The production worker exposes only the per-record bounded read; a test that
    wants "whatever it has produced so far" drains with ``read_record(None)``
    until it returns ``None``.
    """
    out: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = worker.read_record(timeout=0.2)
        if record is None:
            break
        out.append(record)
        if record.get("kind") == "done":
            break
    return out


def _stub_module(tmp_path, monkeypatch):
    """Put a stub worker module on a path the real child can import.

    The child is a fresh interpreter, so it does not inherit this process's
    ``sys.path``; the stub directory is placed on the child's ``PYTHONPATH``
    instead, via the env mapping the worker is launched with.
    """
    package = tmp_path / "stubpkg"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "worker.py").write_text(_STUB_WORKER, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), os.environ.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return "stubpkg.worker", env


def test_a_real_owned_child_is_launched_and_finished(tmp_path, monkeypatch):
    """The production factory launches a real Popen and drives it end to end."""
    module, env = _stub_module(tmp_path, monkeypatch)
    from textflowkit.core.streaming_process import SubprocessWorker

    worker = SubprocessWorker(language=None, module=module, env=env)
    worker.start()
    assert worker._proc is not None and worker._proc.poll() is None
    worker.feed(pcm(3200))
    events = [r for r in _drain(worker) if r["kind"] == "event"]
    assert events and events[0]["record"]["pending"] == "tick"
    worker.finish()
    finals = [r for r in _drain(worker) if r["kind"] == "final"]
    assert finals and finals[0]["record"]["text"] == "the tail"
    worker.terminate()
    # The owned process is really gone, and it exited with a code.
    assert worker._proc is not None
    assert worker._proc.poll() is not None


def test_cancel_terminates_only_the_owned_child(tmp_path, monkeypatch):
    """cancel() kills the owned Popen within the deadline, by its own handle."""
    module, env = _stub_module(tmp_path, monkeypatch)
    from textflowkit.core.streaming_process import SubprocessWorker

    worker = SubprocessWorker(language=None, module=module, env=env)
    worker.start()
    proc = worker._proc
    assert proc is not None and proc.poll() is None
    started = time.monotonic()
    worker.terminate()
    assert time.monotonic() - started < 2.0
    assert proc.poll() is not None  # terminated and reaped


def test_a_child_that_dies_before_start_is_a_worker_error():
    from textflowkit.core.streaming_process import SubprocessWorker

    worker = SubprocessWorker(language=None, module="textflowkit.core.no_such_worker")
    with pytest.raises(st.WorkerProcessError):
        worker.start()
