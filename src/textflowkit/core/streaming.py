"""The public live-streaming session: PCM in, normalized events out.

This module is the *shared, transport-neutral* core of live transcription. It
knows nothing about WebSockets, HTTP, a browser, or a CLI: an adapter feeds it
audio and reads events, and the adapter is the only place a transport lives.

Design, in one place:

**The backend is an owned subprocess, never in-process native state.** The native
engine (``libneedle3.dll``) keeps global state and is not re-entrant. Binding it
in the server process would let one session's work corrupt another's, and a long
native call would freeze the server. So every session owns **exactly one** child
process (``python -m textflowkit.core.stream_worker``) that loads the DLL via
ctypes and speaks a small line protocol over its stdio. The parent stays
responsive while the child is inside a native call, and cancelling a session
terminates *only that child*, by its own ``Popen`` handle - never by image name,
never with ``os.kill(pid, 0)``.

**Audio contract.** Callers feed PCM **signed-16 little-endian, mono, 16 kHz**. A
frame must be non-empty, an even number of bytes, and at most
:data:`MAX_CHUNK_BYTES` (1 s / 32 000 bytes). Bytes are converted to float32 in
the child. The input queue is bounded at :data:`MAX_QUEUE_SECONDS` seconds of
audio; overflowing it is an explicit :class:`StreamingQueueFullError`, never a
silent drop.

**Event contract.** See :mod:`textflowkit.core.streaming_events`. Committed text
is a delta appended exactly once; pending is replaced; ``finish`` runs the native
stop once and emits one final event whose committed text includes the flushed
tail. ``session_audio_seconds`` and each event's ``t_audio_s`` are derived from
the samples *actually fed*, so they are honest about how much audio the session
saw.

**Determinism and testing.** The child is created through a worker factory.
Production passes :class:`SubprocessWorkerFactory` (a real ``Popen``); tests
inject a fake factory and never launch a process or load a DLL.
"""

from __future__ import annotations

import contextlib
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from textflowkit.core.stream_worker import WORKER_MODULE
from textflowkit.core.streaming_events import (
    SessionLimitError,
    SessionStateError,
    StreamEvent,
    StreamEventKind,
    StreamingError,
    StreamingProtocolError,
    StreamingQueueFullError,
    StreamingResourceError,
    StreamWord,
    WorkerProcessError,
)
from textflowkit.core.streaming_process import SubprocessWorker

__all__ = [
    "CANCEL_DEADLINE_S",
    "CHUNK_DEADLINE_S",
    "DEFAULT_MAX_SESSIONS",
    "FINISH_DEADLINE_S",
    "LANGUAGES",
    "MAX_CHUNK_BYTES",
    "MAX_EVENTS",
    "MAX_KEYWORDS",
    "MAX_KEYWORD_CHARS",
    "MAX_QUEUE_SECONDS",
    "MAX_SESSIONS_CEILING",
    "MAX_TRANSCRIPT_CHARS",
    "MAX_TRANSCRIPT_WORDS",
    "SAMPLE_RATE",
    "START_DEADLINE_S",
    "WORKER_MODULE",
    "SessionLimitError",
    "SessionStateError",
    "StreamEvent",
    "StreamEventKind",
    "StreamWord",
    "StreamingError",
    "StreamingProtocolError",
    "StreamingQueueFullError",
    "StreamingResourceError",
    "StreamingSession",
    "SubprocessWorkerFactory",
    "WorkerFactory",
    "WorkerProcess",
    "WorkerProcessError",
    "active_sessions",
    "cancel_all_sessions",
    "session_ceiling",
]

#: Sample rate of every frame this session accepts, in Hz.
SAMPLE_RATE = 16_000
#: Bytes in one second of mono s16le audio (16 000 samples * 2 bytes).
BYTES_PER_SECOND = SAMPLE_RATE * 2
#: Maximum bytes in a single frame: 1 second.
MAX_CHUNK_BYTES = BYTES_PER_SECOND
#: Maximum seconds of unfed audio the input queue may hold.
MAX_QUEUE_SECONDS = 5
#: Maximum bytes the input queue may hold (:data:`MAX_QUEUE_SECONDS` of audio).
MAX_QUEUE_BYTES = MAX_QUEUE_SECONDS * BYTES_PER_SECOND
#: Ceiling on the number of buffered, unread events: back-pressure, not a drop.
MAX_EVENTS = 1024
#: Ceiling on the retained replay window. Independent of the transcript budget:
#: a long silence produces many empty events, and the window must not grow
#: without bound. The final transcript does not depend on this window.
MAX_HISTORY_EVENTS = 4096
#: Documented hard budget on accumulated committed text for one session.
MAX_TRANSCRIPT_CHARS = 1_000_000
#: Documented hard budget on accumulated words for one session.
MAX_TRANSCRIPT_WORDS = 100_000

#: Deadlines. Each bounds a wait so no single call can hang a caller forever.
START_DEADLINE_S = 30.0
CHUNK_DEADLINE_S = 10.0
FINISH_DEADLINE_S = 30.0
CANCEL_DEADLINE_S = 2.0

#: Process-wide concurrent-session ceiling: default 1, configurable up to 4.
DEFAULT_MAX_SESSIONS = 1
MAX_SESSIONS_CEILING = 4

#: The languages the engine accepts. ``None`` means auto-detect.
LANGUAGES: tuple[str, ...] = ("en", "de", "fr", "es", "it", "nl", "pl")


# --- worker interface ------------------------------------------------------


@runtime_checkable
class WorkerProcess(Protocol):
    """The child-process surface a session drives.

    Implemented by the real :class:`SubprocessWorker` and by fakes in tests. A
    worker owns exactly one native engine instance and is not shared.

    The read is **bounded by the worker itself**: :meth:`read_record` waits at
    most ``timeout`` seconds and returns ``None`` if nothing arrived. The session
    never checks a clock and then calls a blocking read - that pattern cannot
    enforce a deadline (the call can block forever). Enforcement lives where the
    wait actually happens.
    """

    def start(self) -> None:
        """Launch the child and load the model. Raises on failure."""

    def feed(self, frame: bytes) -> None:
        """Hand one s16le frame to the engine."""

    def finish(self) -> None:
        """Ask the engine to flush its tail (run the native stop once)."""

    def terminate(self) -> None:
        """Terminate and reap *only this worker's* own process handle."""

    def read_record(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next raw record, or ``None`` if none arrives in ``timeout``.

        A record is ``{"kind": "event"|"final"|"done"|"error", ...}``. A
        ``timeout`` of ``None`` may block until a record is ready or the stream
        ends; any concrete timeout must return ``None`` rather than block past
        it.
        """


WorkerFactory = Callable[[], WorkerProcess]


class SubprocessWorkerFactory:
    """Production factory: one :class:`SubprocessWorker` per session."""

    def __init__(
        self,
        *,
        language: str | None = None,
        weights_path: Any = None,
        python: str | None = None,
        env: dict[str, str] | None = None,
        module: str = WORKER_MODULE,
    ):
        self._language = language
        self._weights_path = weights_path
        self._python = python
        self._env = env
        self._module = module

    def __call__(self) -> SubprocessWorker:
        return self.for_language(self._language)

    def for_language(self, language: str | None) -> SubprocessWorker:
        return SubprocessWorker(
            language=language,
            weights_path=self._weights_path,
            python=self._python,
            env=self._env,
            module=self._module,
        )


# --- session registry: bounded, configurable ceiling -----------------------

_registry_lock = threading.Lock()
_active_sessions: set[StreamingSession] = set()
_max_sessions = DEFAULT_MAX_SESSIONS


def active_sessions() -> int:
    """How many streaming sessions this process currently holds open."""
    with _registry_lock:
        return len(_active_sessions)


def session_ceiling() -> int:
    """The process-wide concurrent-session ceiling."""
    with _registry_lock:
        return _max_sessions


def cancel_all_sessions() -> int:
    """Cancel every live session and return how many were cancelled.

    A server's shutdown path calls this *before* draining the job workers: a live
    stream owns a native child process, and a process exit that left one running
    would orphan it. Each session's ``cancel`` is what tears the child down, so it
    is called here rather than waiting for the interpreter to collect the session.
    Cancelling is best-effort per session - one that is already gone must not stop
    the rest - so a single failure is swallowed and the count reflects the rest.
    """
    with _registry_lock:
        sessions = list(_active_sessions)
    cancelled = 0
    for session in sessions:
        try:
            session.cancel()
        except Exception:  # noqa: BLE001, S112 - one dead session must not block the others
            continue
        cancelled += 1
    return cancelled


def reset_session_registry(max_sessions: int | None = None) -> None:
    """Clear the registry and optionally set the ceiling.

    For tests, and for a host that wants to raise the ceiling to its validated
    maximum (:data:`MAX_SESSIONS_CEILING`). Above the ceiling is refused: the
    native engine's global state is why the bound exists at all.

    The ceiling is validated *before* the registry is cleared, and clearing is
    refused while sessions are live: dropping the registry's strong references to
    a running session would leave its child process owned by nothing. Cancel the
    live sessions first.
    """
    global _max_sessions
    if max_sessions is not None:
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int):
            raise ValueError("max_sessions must be an integer")
        if not 1 <= max_sessions <= MAX_SESSIONS_CEILING:
            raise ValueError(f"max_sessions must be between 1 and {MAX_SESSIONS_CEILING}")
    with _registry_lock:
        if _active_sessions:
            raise SessionLimitError(
                f"cannot reset the registry while {len(_active_sessions)} "
                "session(s) are active; cancel them first"
            )
        if max_sessions is not None:
            _max_sessions = max_sessions


# --- the session -----------------------------------------------------------


class StreamingSession:
    """One live transcription session: PCM in, normalized events out.

    Usage::

        with StreamingSession() as live:
            live.feed(pcm_frame)              # s16le mono 16 kHz, <= 1 s
            event = live.read_event(timeout=1)  # a StreamEvent, or None
        transcript = live.final_transcript    # a canonical Transcript

    The session is ephemeral: it owns one worker process, and finishing or
    cancelling releases it. It never persists audio or events and never couples
    to the durable job store - a live session is not a resumable job.
    """

    def __init__(
        self,
        *,
        language: str | None = None,
        keywords: Sequence[str] | None = None,
        worker_factory: WorkerFactory | None = None,
        weights_path: Any = None,
        audio_queue_seconds: int = MAX_QUEUE_SECONDS,
        auto_start: bool = False,
    ):
        if language is not None and language not in LANGUAGES:
            raise StreamingProtocolError(
                f"language {language!r} is not one of {', '.join(LANGUAGES)} (or None)"
            )
        _validate_counts(audio_queue_seconds, MAX_QUEUE_SECONDS, "audio_queue_seconds")
        self._weights_path = weights_path
        self._audio_queue_seconds = audio_queue_seconds
        self._max_queue_bytes = audio_queue_seconds * BYTES_PER_SECOND

        self.language = language
        self.keywords = _validate_keywords(keywords)

        self._factory: WorkerFactory = worker_factory or SubprocessWorkerFactory(
            language=language, weights_path=weights_path
        )

        self._state = "created"  # created -> running -> finished | cancelled | failed
        self._feed_lock = threading.Lock()
        # Serialize the read path: two concurrent read_event calls on one session
        # would each pull records from the worker and split the output stream -
        # one call would steal the other's event. The read holds this lock only
        # across a single bounded worker read, never across a cancel.
        self._read_lock = threading.Lock()
        # Guards the terminal transition (state + slot release) so cancel and a
        # failing finish cannot both write the state or double-release a slot.
        self._terminal_lock = threading.Lock()
        self._worker: WorkerProcess | None = None
        self._samples_fed = 0        # samples handed to the worker (what was accepted)
        self._samples_processed = 0  # samples the worker has confirmed processing
        #: Deadline for the oldest *outstanding* (fed but unacknowledged) audio.
        #: Set when audio becomes outstanding and cleared once the worker has
        #: acknowledged everything fed. None means no audio is outstanding, so the
        #: chunk deadline cannot fire for a genuinely idle session.
        self._chunk_deadline_at: float | None = None
        self._seq = 0
        self._events: queue.Queue = queue.Queue(maxsize=MAX_EVENTS)
        # Bounded replay window: recent events, dropped oldest-first. The final
        # transcript is built from the canonical accumulator below, never from
        # this window, so trimming the window never loses committed text.
        self._history: deque[StreamEvent] = deque(maxlen=MAX_HISTORY_EVENTS)
        self._history_oldest_seq = 0
        self._committed_parts: list[str] = []
        self._committed_chars = 0
        self._word_count = 0
        # Canonical word accumulator, independent of the bounded replay window:
        # the final transcript's word timings come from here, so history
        # trimming never affects them.
        self._all_words: list[StreamWord] = []
        self._last_seen_language: str | None = None
        self._final_event: StreamEvent | None = None
        self._failure: Exception | None = None
        self._failure_lock = threading.Lock()
        #: True once ``finish`` has begun. A live read that sees the child's
        #: stdout end (or a bare ``done``) while this is False has lost the worker
        #: unexpectedly and must error, not spin on ``None``.
        self._finishing = False
        self._registered = False
        self._session_id = ""

        if auto_start:
            self.start()

    # -- identity and state --

    @property
    def id(self) -> str:
        """A stable id for this session; the source of its final transcript."""
        return self._session_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def worker(self) -> WorkerProcess | None:
        return self._worker

    @property
    def cancelled(self) -> bool:
        return self._state == "cancelled"

    @property
    def finished(self) -> bool:
        return self._state == "finished"

    @property
    def final_event(self) -> StreamEvent | None:
        return self._final_event

    @property
    def committed_text(self) -> str:
        """The transcript committed so far, deltas joined by a single space."""
        return " ".join(part for part in self._committed_parts if part)

    @property
    def session_audio_seconds(self) -> float:
        """Seconds of audio the engine has **processed** (samples / 16 000).

        This is the session timeline that events carry: the sample clock the
        engine has advanced to, not the amount of audio merely accepted. On a
        finished session it equals the fed duration, because every fed sample is
        processed by the time the tail flushes.
        """
        return self._samples_processed / SAMPLE_RATE

    #: Alias for the brief's wording ("session audio seconds via actual samples").
    @property
    def duration_s(self) -> float:
        return self.session_audio_seconds

    @property
    def fed_audio_seconds(self) -> float:
        """Seconds of audio the caller has fed (accepted), processed or not."""
        return self._samples_fed / SAMPLE_RATE

    @property
    def queued_audio_bytes(self) -> int:
        """Bytes fed but not yet confirmed processed by the worker.

        Derived from actual sample counts on both sides - fed minus processed -
        so it stays correct whether the worker drains one chunk or many per read,
        rather than being zeroed on any single record.
        """
        return (self._samples_fed - self._samples_processed) * 2

    @property
    def final_transcript(self):
        """The canonical :class:`Transcript` for a finished session.

        ``source`` is ``stream:<id>``; the duration is the audio actually fed;
        the committed text becomes one segment carrying the word timings seen.
        Only a finished session has a final transcript.
        """
        if self._state != "finished":
            raise SessionStateError(
                f"a final transcript exists only for a finished session "
                f"(state={self._state!r})"
            )
        return self._build_transcript()

    # -- lifecycle --

    def start(self) -> StreamingSession:
        """Reserve a session slot, launch the owned worker, load the model.

        Cancel-safe against a cancel that races this call from another thread: a
        cancel that lands while ``start`` is launching wins. The worker is still
        terminated and the slot still released, and the state is left
        ``cancelled`` - ``start`` never resurrects a session another thread has
        already made terminal. ``SessionStateError`` is raised to the caller so it
        learns the start did not take.
        """
        if self._state != "created":
            raise SessionStateError(f"cannot start a session in state {self._state!r}")
        _acquire_slot(self)
        # A cancel may have run between the state check and the slot acquisition
        # (both take different locks). If it did, the session is already terminal:
        # do not launch a worker and do not overwrite the state.
        if self._state != "created":
            _release_slot(self)
            raise SessionStateError(f"cannot start a session in state {self._state!r}")
        try:
            self._worker = self._factory()
            # Publish the worker before starting it: a cancel racing here can then
            # terminate *this* worker. Re-check the state first so a cancel that
            # landed between the slot acquisition and now does not leave an
            # unstarted child behind.
            if self._state != "created":
                _release_slot(self)
                raise SessionStateError(f"cannot start a session in state {self._state!r}")
            self._worker.start()
        except BaseException:
            # The worker may have been created (a real child launched) before the
            # failure: terminate it so a failed start never leaks a process, then
            # release the slot and mark the session failed.
            if self._worker is not None:
                with contextlib.suppress(Exception):
                    self._worker.terminate()
            self._fail_and_release()
            raise
        # Final cancel check: a cancel that landed while the worker was launching
        # has set the state to ``cancelled`` and terminated ``self._worker`` (which
        # by then is this just-started worker). Do not overwrite that decision.
        if self._state != "created":
            with contextlib.suppress(Exception):
                self._worker.terminate()
            _release_slot(self)
            raise SessionStateError(f"cannot start a session in state {self._state!r}")
        # The worker exposes a *pull* read (read_record(timeout)); the session
        # never keeps a generator whose next() cannot be interrupted. The stream
        # advances only as records are read, so nothing is replayed.
        self._state = "running"
        return self

    def feed(self, audio: bytes | bytearray | memoryview) -> int:
        """Queue one PCM frame; returns the number of bytes accepted.

        ``audio`` is s16le mono 16 kHz, non-empty, an even number of bytes, and
        at most :data:`MAX_CHUNK_BYTES`. A frame that would push the queue past
        its bound raises :class:`StreamingQueueFullError` - nothing is dropped.
        The call never blocks on the native engine, so a cancel can proceed.
        """
        with self._feed_lock:
            if self._state != "running":
                raise SessionStateError(f"cannot feed a {self._state} session")
            payload = bytes(audio)
            self._validate_frame(payload)
            if self.queued_audio_bytes + len(payload) > self._max_queue_bytes:
                raise StreamingQueueFullError(
                    f"input queue would exceed {self._audio_queue_seconds}s of audio "
                    f"(held {self.queued_audio_bytes} bytes, adding {len(payload)}, "
                    f"limit {self._max_queue_bytes})"
                )
            assert self._worker is not None
            self._worker.feed(payload)  # may raise WorkerProcessError
            self._samples_fed += len(payload) // 2
            # Audio just became outstanding, so arm the chunk deadline for it if it
            # is not already armed (the deadline is on the *oldest* outstanding
            # frame; feeding more while the first is unacknowledged must not extend
            # the caller's bound).
            if self._samples_processed < self._samples_fed and self._chunk_deadline_at is None:
                self._chunk_deadline_at = time.monotonic() + CHUNK_DEADLINE_S
            return len(payload)

    def read_event(self, timeout: float | None = None) -> StreamEvent | None:
        """Return the next :class:`StreamEvent`, or ``None`` if none is ready.

        ``timeout`` bounds how long this call may wait for the worker. The wait
        is enforced *inside* :meth:`WorkerProcess.read_record`, which returns
        ``None`` when nothing arrived in time - the session never checks a clock
        and then calls a blocking read, because that cannot bound the read. A
        worker failure or a protocol violation raises. The read holds no lock the
        cancel path needs, so a cancel from another thread always proceeds.

        Concurrent reads are serialized: only one consumer may pull records from
        the worker at a time, so two readers cannot split the stream and steal
        each other's events. The lock is acquired with the caller's own bound, so a
        second reader waiting behind a long read does not wait past its own
        ``timeout``: it returns ``None`` instead. ``timeout=None`` waits for the
        lock without a bound, matching an unbounded read.
        """
        _validate_timeout(timeout)
        # Bound the wait for the read lock by the caller's timeout so one reader
        # cannot hold another past its own bound. A caller that passes no timeout
        # asked to wait indefinitely, and does.
        acquired = self._read_lock.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            return None
        try:
            return self._read_event_locked(timeout)
        finally:
            self._read_lock.release()

    def _read_event_locked(self, timeout: float | None) -> StreamEvent | None:
        failure = self._failure
        if failure is not None:
            raise failure
        if not self._events.empty():
            event = self._events.get_nowait()
            return None if event is _SENTINEL else event
        if self._worker is None or self._state != "running":
            return None
        # Bounded read: ``timeout`` is capped by the outstanding-frame deadline, so
        # audio accepted but never acknowledged cannot hold a live read past the
        # chunk deadline. The cap is a wall clock the read itself honours.
        record = self._read_one(timeout=self._bounded_read_timeout(timeout))
        if record is not None:
            self._dispatch(record)
        else:
            # A ``None`` read during a live (not-finishing) read means either the
            # timeout elapsed or the child's stream ended. Distinguish them: an
            # unexpected end-of-stream while the session is still running is an
            # error, not an endless stream of ``None``.
            self._raise_if_stream_ended()
            self._raise_if_chunk_stalled()
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            return None
        return None if event is _SENTINEL else event

    def _raise_if_stream_ended(self) -> None:
        if self._state != "running" or self._finishing:
            return
        at_eof = getattr(self._worker, "at_eof", None)
        if callable(at_eof) and at_eof():
            self._fail_live(
                WorkerProcessError(
                    "the streaming worker's output ended before the session was finished"
                )
            )

    def _bounded_read_timeout(self, timeout: float | None) -> float | None:
        """Cap a live read's timeout by the outstanding-frame chunk deadline.

        The chunk deadline is a bound on *how long audio may sit accepted but
        unacknowledged*, not on a single call. A caller that reads in many short
        steps must still see the deadline fire, so each read waits at most the
        time remaining until the oldest outstanding frame hits the deadline. With
        no outstanding audio (nothing fed that is unacknowledged) the caller's own
        timeout stands: an idle session is never failed for silence.
        """
        if self._samples_processed >= self._samples_fed or self._chunk_deadline_at is None:
            return timeout
        remaining = self._chunk_deadline_at - time.monotonic()
        if remaining <= 0:
            return 0.0
        if timeout is None:
            return remaining
        return min(timeout, remaining)

    def _raise_if_chunk_stalled(self) -> None:
        """Fail a live session whose queued audio outlived the chunk deadline."""
        if self._state != "running" or self._finishing:
            return
        if self._samples_processed >= self._samples_fed or self._chunk_deadline_at is None:
            return
        if time.monotonic() >= self._chunk_deadline_at:
            outstanding = self._samples_fed - self._samples_processed
            self._fail_live(
                WorkerProcessError(
                    f"the streaming worker did not acknowledge {outstanding} queued "
                    f"sample(s) within {CHUNK_DEADLINE_S:.0f}s; the child is not "
                    "consuming its input"
                )
            )

    def _fail_live(self, error: Exception) -> None:
        """Make a live (non-finish) failure terminal: sticky error, worker gone, slot free.

        Every fatal path on the live read side - an unsolicited ``done``, a worker
        ``error`` record, an unexpected EOF, a stalled worker, a resource-budget
        refusal - routes through here. The session must become ``failed`` *and*
        release its owned worker and its slot, exactly as the start and finish
        failure paths do. Raising without this cleanup is what left a failed read's
        session ``running``, its slot held, and its child alive.
        """
        with self._failure_lock:
            if self._failure is None:
                self._failure = error
        self._fail_and_release(error)
        raise error

    def events_since(self, seq: int = 0) -> list[StreamEvent]:
        """Every retained event with ``seq`` >= ``seq``, in order.

        The replay window is bounded (:data:`MAX_HISTORY_EVENTS`), so very old
        events are dropped oldest-first - a consumer that falls too far behind
        must re-sync from the canonical transcript, not from this window. If the
        window has dropped events at or after ``seq``, they are simply absent
        here; :meth:`events_expired_before` names the oldest retained sequence so
        a caller can detect the gap rather than mistake it for an empty span.
        """
        return [e for e in self._history if e.seq >= seq]

    def events_expired_before(self) -> int:
        """The lowest ``seq`` still retained; anything below it is gone."""
        return self._history_oldest_seq

    def finish(self, timeout: float | None = None) -> StreamEvent:
        """Flush the tail: run the native stop once, then release the worker.

        Returns the single final :class:`StreamEvent`. Idempotent - a second
        call returns the same final event without touching the worker again.

        Exactly one *real* final record must arrive before ``done``; the flush is
        never fabricated. Any failure - a timeout, a protocol error, a resource
        budget - terminates the worker, marks the session ``failed``, and
        releases its slot, so a failed finish never leaves a slot reserved or a
        child alive.
        """
        _validate_timeout(timeout)
        if self._state == "finished":
            assert self._final_event is not None
            return self._final_event
        if self._state == "cancelled":
            raise SessionStateError("cannot finish a cancelled session")
        if self._state != "running":
            raise SessionStateError(f"cannot finish a session in state {self._state!r}")

        deadline = time.monotonic() + (timeout if timeout is not None else FINISH_DEADLINE_S)
        assert self._worker is not None
        # Serialize against a concurrent read_event: the finish drain and a live
        # read must never both pull from the worker, or one would steal records
        # from the other. Cancel does not take this lock, so it still proceeds. The
        # wait for the lock is itself bounded by the finish deadline, so a finish
        # behind a long read still returns within its budget rather than blocking
        # past it.
        remaining = deadline - time.monotonic()
        if not self._read_lock.acquire(timeout=max(remaining, 0.0)):
            self._fail_and_release()
            raise WorkerProcessError(
                "the streaming worker was busy in another read past the finish deadline"
            )
        try:
            return self._finish_locked(deadline)
        finally:
            self._read_lock.release()

    def _finish_locked(self, deadline: float) -> StreamEvent:
        self._finishing = True
        final_raw: dict[str, Any] | None = None
        done_seen = False
        try:
            assert self._worker is not None
            self._worker.finish()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerProcessError(
                        f"the streaming worker did not finish within "
                        f"{FINISH_DEADLINE_S:.0f}s"
                    )
                # Enforce the deadline at the read itself: read_record returns
                # None once `remaining` elapses, so a worker that never reports
                # the tail cannot hold finish() forever.
                raw = self._worker.read_record(timeout=remaining)
                if raw is None:
                    if not _worker_alive_or_reading(self._worker):
                        raise WorkerProcessError(
                            "the streaming worker exited before reporting its final record"
                        )
                    continue
                kind = raw.get("kind")
                if kind == "event":
                    self._dispatch(raw)
                elif kind == "final":
                    if final_raw is not None:
                        raise StreamingProtocolError(
                            "the worker reported more than one final record"
                        )
                    final_raw = raw.get("record") or {}
                elif kind == "error":
                    raise WorkerProcessError(raw.get("message", "worker error"))
                elif kind == "done":
                    done_seen = True
                    break
                elif kind is None:
                    raise StreamingProtocolError(f"worker record has no kind: {raw}")
            if not done_seen:
                raise StreamingProtocolError("the worker never reported done after finish")
            if final_raw is None:
                raise StreamingProtocolError(
                    "the worker finished without a final record; the flush is never "
                    "fabricated"
                )
            self._finalize(final_raw)
        except BaseException:
            self._fail_and_release()
            raise
        finally:
            self._finishing = False
            if self._worker is not None:
                self._worker.terminate()
        assert self._final_event is not None
        return self._final_event

    def cancel(self) -> None:
        """Terminate and release this session's own worker within the deadline.

        Bounded and explicit: the child is terminated through its owned handle
        (with a kill fallback on the *same* handle), never by image name. After
        cancel the session is terminal and cannot be fed. A cancel racing a
        finish wins: the state is ``cancelled`` and cannot be overwritten.
        """
        with self._terminal_lock:
            if self._state in ("finished", "cancelled", "failed"):
                return
            self._state = "cancelled"
            if self._worker is not None:
                # Releasing the owned handle must never raise out of cancel: the
                # session is already terminal, and failing to reap only this
                # child must not become an error the caller has to handle.
                with contextlib.suppress(Exception):
                    self._worker.terminate()
            _release_slot(self)
        # A full queue means the reader is not draining; it needs no sentinel,
        # and cancel must still succeed.
        with contextlib.suppress(queue.Full):
            self._events.put_nowait(_SENTINEL)

    def _fail_and_release(self, error: Exception | None = None) -> None:
        """Mark the session failed (unless a cancel won), stop its worker, free its slot.

        A failed session must not leak its owned child: the worker is terminated
        (bounded by its own cancel deadline) and the slot released, so a failure on
        any path - start, finish, or a live read - leaves nothing running. If a
        cancel already made the session terminal, this is a no-op for the state,
        but the worker is still terminated here so a failed-then-cancelled session
        never keeps a child alive.
        """
        with self._terminal_lock:
            already_terminal = self._state not in ("running", "created")
            if not already_terminal:
                self._state = "failed"
                if error is not None:
                    with self._failure_lock:
                        if self._failure is None:
                            self._failure = error
                _release_slot(self)
        if already_terminal:
            return
        # Terminate the owned worker outside the terminal lock: a kill can take up
        # to the cancel budget and must not block another thread's terminal
        # transition. ``terminate`` acts only on this session's own handle.
        if self._worker is not None:
            with contextlib.suppress(Exception):
                self._worker.terminate()
        with contextlib.suppress(queue.Full):
            self._events.put_nowait(_SENTINEL)

    def close(self) -> None:
        """Release the session if it is still running (a cancel, else a no-op)."""
        if self._state == "running":
            self.cancel()

    # -- context manager --

    def __enter__(self) -> StreamingSession:  # noqa: PYI034 - returns self, but 3.10 lacks typing.Self
        if self._state == "created":
            self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._state == "running":
            if exc_type is None:
                self.finish()
            else:
                self.cancel()
        else:
            self.close()

    def __del__(self):  # pragma: no cover - best-effort only
        # Release during interpreter teardown must never raise.
        with contextlib.suppress(Exception):
            if self._state == "running":
                self.cancel()

    # -- internals --

    def _validate_frame(self, payload: bytes) -> None:
        if not payload:
            raise StreamingProtocolError("an audio frame must not be empty")
        if len(payload) % 2 != 0:
            raise StreamingProtocolError(
                f"an audio frame must be whole 16-bit samples, got {len(payload)} bytes"
            )
        if len(payload) > MAX_CHUNK_BYTES:
            raise StreamingProtocolError(
                f"an audio frame may be at most {MAX_CHUNK_BYTES} bytes (1 s), "
                f"got {len(payload)}"
            )

    def _read_one(self, *, timeout: float | None) -> dict[str, Any] | None:
        """Read exactly one record from the worker, bounded by ``timeout``.

        The bound lives in the worker's own timed read. A worker error becomes a
        sticky failure so a later read re-raises it rather than waiting again.
        """
        assert self._worker is not None
        try:
            return self._worker.read_record(timeout=timeout)
        except WorkerProcessError as exc:
            # A worker failure while reading live is terminal: the child is gone or
            # cancelled, so the session must release its slot and mark failed
            # rather than leave a half-dead session holding a registry entry.
            self._fail_live(exc)

    def _dispatch(self, raw: dict[str, Any]) -> None:
        """Account for one worker record and emit it if it is an event.

        The record's ``consumed_samples`` (the child's own ack) advances the
        processed-sample clock; the event's ``t_audio_s`` is that clock, so an
        event never claims more audio than the child has actually consumed. A
        bare ``done`` outside ``finish`` is a protocol error, and an ``error``
        record becomes a sticky failure. Every fatal outcome here makes the
        session terminal through :meth:`_fail_live`.
        """
        kind = raw.get("kind")
        if kind == "error":
            self._fail_live(WorkerProcessError(raw.get("message", "worker error")))
        if kind == "event":
            try:
                self._emit(raw.get("record") or {})
            except StreamingError as exc:
                # A resource-budget refusal or a malformed event on the live path
                # is terminal: the worker is stopped and the slot released.
                self._fail_live(exc)
            return
        if kind == "done":
            # ``done`` is part of the finish handshake only. Seeing it on a live
            # read - before any final record and outside finish() - means the
            # child declared the stream over on its own; that is a protocol
            # error, never a silent transition to finished.
            self._fail_live(
                StreamingProtocolError(
                    "the worker reported done during a live read; done belongs to finish()"
                )
            )

    def _emit(self, raw: dict[str, Any]) -> None:
        """Normalize and enqueue one raw worker record as a (non-final) event."""
        self._account_processed(raw)
        event = StreamEvent.from_record(
            raw, seq=self._seq, t_audio_s=self.session_audio_seconds, is_final=False
        )
        self._accumulate(event)
        self._seq += 1
        self._remember(event)
        self._enqueue(event)

    def _account_processed(self, raw: dict[str, Any]) -> None:
        """Advance the processed-sample clock from the child's own ack.

        The authoritative count is the child's ``consumed_samples`` - the samples
        it actually received and processed frame by frame - *not* the native
        engine's ``received`` field, which is the engine's internal clock and is
        only cross-checked against the ack, within a tolerance.

        Regressions (an ack below the current clock) and overshoot (an ack past
        what was fed) are protocol errors, not silently clamped: the timeline
        must be honest or the session must fail.
        """
        consumed = raw.get("consumed_samples")
        if consumed is not None:
            samples = self._as_sample_count(consumed, "consumed_samples")
            # Cross-check the native engine's own clock against the ack when it
            # is present; a disagreement past tolerance is a broken timeline.
            received = raw.get("received")
            if received is not None and not isinstance(received, bool) and isinstance(
                received, (int, float)
            ):
                native = float(received) * SAMPLE_RATE
                if math.isfinite(native) and abs(native - samples) > SAMPLE_RATE:
                    raise StreamingProtocolError(
                        f"worker 'received' ({float(received)}s) disagrees with the "
                        f"child's consumed_samples acks ({samples}) beyond tolerance"
                    )
            if samples < self._samples_processed:
                raise StreamingProtocolError(
                    f"worker consumed_samples regressed ({samples} < "
                    f"{self._samples_processed})"
                )
            if samples > self._samples_fed:
                raise StreamingProtocolError(
                    f"worker consumed_samples ({samples}) exceeds what was fed "
                    f"({self._samples_fed})"
                )
            self._samples_processed = samples
            self._rearm_chunk_deadline()
            return
        # Fallback for a worker that reports only the native clock (e.g. the
        # legacy field): convert seconds to samples, still validated.
        received = raw.get("received")
        if received is None:
            return
        value = self._as_sample_count(received, "received", seconds=True)
        processed = round(value * SAMPLE_RATE)
        self._samples_processed = max(self._samples_processed, min(processed, self._samples_fed))
        self._rearm_chunk_deadline()

    def _rearm_chunk_deadline(self) -> None:
        """Clear the chunk deadline once everything fed is acknowledged.

        If an ack advanced the processed clock to catch up with what was fed, no
        audio is outstanding and the deadline cannot fire. Any audio still
        outstanding keeps the *existing* deadline - it is the bound on the oldest
        unacknowledged frame and must not be pushed back by a partial ack.
        """
        if self._samples_processed >= self._samples_fed:
            self._chunk_deadline_at = None

    @staticmethod
    def _as_sample_count(value: Any, name: str, *, seconds: bool = False) -> float:
        """Validate a numeric timeline field; NaN/inf is a protocol error."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StreamingProtocolError(
                f"worker record {name!r} is {type(value).__name__}, expected a number"
            )
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise StreamingProtocolError(
                f"worker record {name!r} ({number}) is not a finite non-negative "
                "cumulative value"
            )
        return number

    def _remember(self, event: StreamEvent) -> None:
        """Append to the bounded replay window, tracking the oldest retained seq."""
        self._history.append(event)
        if len(self._history) == self._history.maxlen:
            oldest = self._history[0]
            self._history_oldest_seq = oldest.seq

    def _accumulate(self, event: StreamEvent) -> None:
        """Fold one event into the canonical accumulators, budget checked first.

        Every budget is validated *before* any accumulator is mutated, so a
        rejected event leaves the session exactly as it was - a huge unknown
        record cannot partially blow the history and then be refused.
        """
        delta = event.committed_delta
        # The committed text a consumer sees is deltas joined by a single space,
        # so each non-empty delta after the first contributes a leading space.
        # The budget must account for those inserted spaces, or the stored text
        # can exceed the budget the accumulator believes it enforced.
        added_chars = len(delta) + (1 if delta and self._committed_parts else 0)
        new_chars = self._committed_chars + added_chars
        new_words = self._word_count + len(event.words)
        if new_chars > MAX_TRANSCRIPT_CHARS:
            raise StreamingResourceError(
                f"session transcript would exceed its budget of "
                f"{MAX_TRANSCRIPT_CHARS} characters ({new_chars}); the session is "
                "left intact at its last good event rather than silently truncated"
            )
        if new_words > MAX_TRANSCRIPT_WORDS:
            raise StreamingResourceError(
                f"session transcript would exceed its budget of "
                f"{MAX_TRANSCRIPT_WORDS} words ({new_words})"
            )
        # Budgets passed: now mutate.
        if delta:
            self._committed_chars = new_chars
            self._committed_parts.append(delta)
        self._word_count = new_words
        self._all_words.extend(event.words)
        if event.language:
            self._last_seen_language = event.language

    def _enqueue(self, event: StreamEvent) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full as exc:
            raise StreamingResourceError(
                f"the event queue is full ({MAX_EVENTS}); the consumer is not "
                "reading, and back-pressure is the only alternative to a silent drop"
            ) from exc

    def _finalize(self, final_raw: dict[str, Any]) -> None:
        # The engine has processed everything fed by the time the tail flushes,
        # so the timeline lands on the fed duration even if no event reported it.
        self._samples_processed = self._samples_fed
        self._account_processed(final_raw)
        event = StreamEvent.from_record(
            final_raw, seq=self._seq, t_audio_s=self.session_audio_seconds, is_final=True
        )
        if event.pending:
            raise StreamingProtocolError(
                "the final record still has a pending tail; the flush left text "
                "provisional, which the final event must not do"
            )
        self._accumulate(event)
        self._seq += 1
        self._remember(event)
        self._final_event = event
        self._enqueue(event)
        with self._terminal_lock:
            if self._state == "cancelled":
                # A cancel won the race while the tail was flushing: keep the
                # cancelled state, but the final event is still recorded.
                return
            self._state = "finished"
            _release_slot(self)

    def _build_transcript(self):
        from textflowkit.core.model import Segment, Transcript, WordTiming

        # Words come from the canonical accumulator, not the bounded replay
        # window: trimming the window must never drop a word from the final
        # transcript.
        words = [
            WordTiming(start=w.start, end=w.end, text=w.text)
            for w in self._all_words
        ]
        text = self.committed_text
        segments = (
            [Segment(start=0.0, end=self.session_audio_seconds, text=text, words=words)]
            if text or words
            else []
        )
        return Transcript(
            source=f"stream:{self._session_id}",
            language=self._last_language(),
            segments=segments,
            duration=self.session_audio_seconds,
            engine="whistle-streaming",
            metadata={"events": self._seq, "live": True},
        )

    def _last_language(self) -> str | None:
        return self._last_seen_language or self.language


#: A sentinel event pushed onto the queue when a session is cancelled, so a
#: blocked reader wakes instead of waiting out its timeout.
_SENTINEL = StreamEvent(
    seq=-1, committed_delta="", pending="", is_final=True, t_audio_s=0.0
)

#: Maximum length of a single keyword, and the maximum number of keywords.
MAX_KEYWORD_CHARS = 256
MAX_KEYWORDS = 32


def _validate_timeout(timeout: float | None) -> None:
    """A public timeout must be a finite, non-negative number (or ``None``)."""
    if timeout is None:
        return
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(
            f"timeout must be a finite number or None, got {type(timeout).__name__}"
        )
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"timeout must be finite and non-negative, got {timeout!r}")


def _validate_counts(value: int, maximum: int, name: str) -> None:
    """A size/count argument must be a real integer (never a bool) in range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _validate_keywords(keywords: Sequence[str] | None) -> tuple[str, ...]:
    """Validate and bound the keywords argument, or refuse it.

    The brief requires that an accepted ``keywords`` argument is *forwarded and
    bounded*, or removed - it must not be silently ignored. Bounded here: each
    keyword is a string under :data:`MAX_KEYWORD_CHARS`, and the count is under
    :data:`MAX_KEYWORDS`.
    """
    if not keywords:
        return ()
    if isinstance(keywords, (str, bytes)):
        raise StreamingProtocolError("keywords must be a sequence of strings, not a single string")
    items = list(keywords)
    if len(items) > MAX_KEYWORDS:
        raise StreamingProtocolError(
            f"at most {MAX_KEYWORDS} keywords are accepted, got {len(items)}"
        )
    for item in items:
        if not isinstance(item, str):
            raise StreamingProtocolError(
                f"a keyword must be a string, got {type(item).__name__}"
            )
        if not item or len(item) > MAX_KEYWORD_CHARS:
            raise StreamingProtocolError(
                f"a keyword must be 1..{MAX_KEYWORD_CHARS} characters, got {len(item)}"
            )
    return tuple(items)


def _worker_alive_or_reading(worker: WorkerProcess) -> bool:
    """Whether a ``None`` read could still yield data (worker alive, not at EOF).

    A worker without an ``at_eof`` probe is assumed alive; the concrete
    :class:`SubprocessWorker` provides one, so a finish that sees a closed stream
    fails fast instead of looping until the deadline.
    """
    at_eof = getattr(worker, "at_eof", None)
    if callable(at_eof):
        return not at_eof()
    return True


def _acquire_slot(session: StreamingSession) -> None:
    with _registry_lock:
        if len(_active_sessions) >= _max_sessions:
            raise SessionLimitError(
                f"the process already holds {len(_active_sessions)} streaming "
                f"session(s); the ceiling is {_max_sessions}"
            )
        _active_sessions.add(session)
        session._registered = True
        if not session._session_id:
            session._session_id = f"{int(time.time() * 1000):x}-{id(session) & 0xFFFF:04x}"


def _release_slot(session: StreamingSession) -> None:
    """Remove a session from the registry, if it is registered.

    Guarded by the session's own ``_registered`` flag checked-and-cleared under
    the lock, so a re-entrant call - for instance from ``__del__`` running while
    a slot is already being released - is a no-op and cannot double-release. The
    set is only ever *mutated* while holding ``_registry_lock``; no user code
    runs inside the critical section, so a destructor that reacquires the lock
    cannot deadlock the thread that is releasing.
    """
    with _registry_lock:
        if not session._registered:
            return
        session._registered = False
        _active_sessions.discard(session)
