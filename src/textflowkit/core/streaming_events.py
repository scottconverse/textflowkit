"""The typed event record shared by every live-streaming consumer.

A :class:`StreamEvent` is one normalized step of a live transcription. It is the
public, transport-neutral shape the rest of the product speaks: an adapter (a
WebSocket, a CLI tail, another tool) serializes :meth:`StreamEvent.to_dict` and
does nothing else to it. Keeping one canonical event object is what lets the
session stay shared while transports multiply - the same reason
:class:`textflowkit.core.model.Transcript` exists for finished transcriptions.

Contract, stated once and enforced here:

- ``committed_delta`` is a **delta**: the text the engine newly committed on the
  pass that produced this event. A consumer joins deltas with a space (an empty
  delta contributes nothing). It is never cumulative and never re-sent.
- ``pending`` is a **replacement**: the unconfirmed tail as of this event. It
  supersedes the previous event's pending and must never be appended as final
  text. On the final event the engine has flushed its tail into the committed
  text, so ``pending`` is empty.
- ``t_audio_s`` is the **session sample timeline**: seconds of audio this session
  has consumed, relative to the session's first sample (never the machine clock).
- ``words`` carry ``start``/``end`` relative to the same session zero and are
  required to be finite, non-negative, and ordered (``start <= end``).
- ``seq`` is a **monotonic** output sequence, assigned by the session, one per
  emitted event.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "MAX_PENDING_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_WORDS_PER_RECORD",
    "PROTOCOL_ERROR",
    "QUEUE_FULL_ERROR",
    "RESOURCE_ERROR",
    "SESSION_LIMIT_ERROR",
    "SESSION_STATE_ERROR",
    "WORKER_ERROR",
    "SessionLimitError",
    "SessionStateError",
    "StreamEvent",
    "StreamEventKind",
    "StreamWord",
    "StreamingError",
    "StreamingProtocolError",
    "StreamingQueueFullError",
    "StreamingResourceError",
    "WorkerProcessError",
]

# --- per-record ceilings ---------------------------------------------------
#
# One worker record can carry arbitrary text, so each field is bounded before it
# is normalized. These are per-*record* caps, independent of the per-session
# transcript budget in :mod:`textflowkit.core.streaming`: a single record past a
# cap is refused outright, so unknown huge text cannot enter the replay history
# or the accumulator at all.

#: Maximum characters of committed ``text`` in one record.
MAX_TEXT_CHARS = 64 * 1024
#: Maximum characters of ``pending`` in one record.
MAX_PENDING_CHARS = 64 * 1024
#: Maximum word entries in one record.
MAX_WORDS_PER_RECORD = 4096

# --- error codes -----------------------------------------------------------
#
# Every streaming failure is one of these, so a transport maps a failure to an
# HTTP status or a close code by reading ``error.code`` rather than by matching
# message text. The values are frozen; a new failure adds a new code.

PROTOCOL_ERROR = "STREAMING_PROTOCOL_ERROR"
SESSION_STATE_ERROR = "STREAMING_SESSION_STATE_ERROR"
SESSION_LIMIT_ERROR = "STREAMING_SESSION_LIMIT_ERROR"
QUEUE_FULL_ERROR = "STREAMING_QUEUE_FULL"
RESOURCE_ERROR = "STREAMING_RESOURCE_BUDGET_EXCEEDED"
WORKER_ERROR = "STREAMING_WORKER_ERROR"


class StreamingError(RuntimeError):
    """Base class for every live-streaming failure, carrying a stable code."""

    code = "STREAMING_ERROR"


class StreamingProtocolError(StreamingError):
    """A frame, an event record, or a call argument violated the contract.

    Raised for an odd or empty audio frame, an oversize frame, an unknown
    language, a word timing that is not finite/ordered/non-negative, or a raw
    worker record that is not the documented shape. The session or the worker
    that produced it is not trusted further in the same call.
    """

    code = PROTOCOL_ERROR


class StreamingQueueFullError(StreamingError):
    """The bounded input queue would exceed its audio limit.

    The refusal is explicit: no frame is silently dropped to make room. The
    caller is expected to slow down, or to accept that it has outrun the
    engine, rather than to keep feeding and lose audio invisibly.
    """

    code = QUEUE_FULL_ERROR


class StreamingResourceError(StreamingError):
    """A documented hard budget for one session was exceeded.

    The transcript accumulator and the queued event count have fixed ceilings.
    Crossing one is a refusal, never a silent truncation: the caller sees the
    error and keeps whatever it had already been given.
    """

    code = RESOURCE_ERROR


class SessionStateError(StreamingError):
    """A lifecycle method was called in a state that does not allow it."""

    code = SESSION_STATE_ERROR


class SessionLimitError(StreamingError):
    """Starting this session would exceed the process-wide concurrent ceiling."""

    code = SESSION_LIMIT_ERROR


class WorkerProcessError(StreamingError):
    """The owned worker process failed, died, or could not be launched."""

    code = WORKER_ERROR


# --- events ----------------------------------------------------------------


class StreamEventKind(str, Enum):
    """What a :class:`StreamEvent` represents on the output sequence."""

    #: A transcript step: the engine committed text, or has a pending tail.
    TRANSCRIPT = "transcript"
    #: The end-of-stream flush: ``finish`` ran the native stop and the tail is
    #: now committed. Exactly one per session.
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class StreamWord:
    """One word with its interval relative to the session's sample zero."""

    text: str
    start: float
    end: float
    probability: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "probability": self.probability,
        }


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One normalized live-transcription event. See the module docstring."""

    seq: int
    committed_delta: str
    pending: str
    is_final: bool
    t_audio_s: float
    language: str | None = None
    pass_ms: float | None = None
    words: tuple[StreamWord, ...] = field(default_factory=tuple)

    @property
    def kind(self) -> StreamEventKind:
        return StreamEventKind.FINAL if self.is_final else StreamEventKind.TRANSCRIPT

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-serializable mapping. The wire shape for every adapter."""
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "committed_delta": self.committed_delta,
            "pending": self.pending,
            "is_final": self.is_final,
            "t_audio_s": self.t_audio_s,
            "language": self.language,
            "pass_ms": self.pass_ms,
            "words": [w.to_dict() for w in self.words],
        }

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        *,
        seq: int,
        t_audio_s: float,
        is_final: bool,
    ) -> StreamEvent:
        """Normalize one raw worker record into a validated event.

        ``record`` is the engine's per-chunk object (``text``, ``pending``,
        ``words``, ``language``, ``received``, ``pass_ms``). The session owns
        ``seq`` and the final ``t_audio_s`` (both derived from its own state);
        ``received`` is cross-checked against ``t_audio_s`` so a record whose
        timeline disagrees with the session timeline is refused rather than
        trusted.

        Raises :class:`StreamingProtocolError` for any field that violates the
        contract.
        """
        if not isinstance(record, dict):
            raise StreamingProtocolError(
                f"worker record is {type(record).__name__}, expected an object"
            )
        committed = _as_text(record.get("text", ""), "text", MAX_TEXT_CHARS)
        pending = _as_text(record.get("pending", ""), "pending", MAX_PENDING_CHARS)
        language = record.get("language") or None
        if language is not None and not isinstance(language, str):
            raise StreamingProtocolError("worker record 'language' is not a string")
        pass_ms = _as_number(record.get("pass_ms", 0.0), "pass_ms", allow_none=True)
        received = _as_number(record.get("received", t_audio_s), "received")

        # When the child supplies its own ``consumed_samples`` ack, the session
        # timeline is built from *that*, and the native ``received`` is only a
        # cross-check (enforced in the session, with a tolerance). Only when no
        # ack is present does the record's ``received`` have to match the session
        # timeline closely.
        if record.get("consumed_samples") is None:
            try:
                received_ok = math.isfinite(received) and math.isclose(
                    received, t_audio_s, rel_tol=0.0, abs_tol=1e-3
                )
            except (TypeError, ValueError):
                received_ok = False
            if not received_ok:
                raise StreamingProtocolError(
                    f"worker record 'received' ({received}) disagrees with the session "
                    f"timeline ({t_audio_s})"
                )

        raw_words = record.get("words") or []
        if not isinstance(raw_words, (list, tuple)):
            raise StreamingProtocolError("worker record 'words' is not a list")
        if len(raw_words) > MAX_WORDS_PER_RECORD:
            raise StreamingProtocolError(
                f"worker record has {len(raw_words)} words, past the "
                f"{MAX_WORDS_PER_RECORD} per-record ceiling"
            )
        words = tuple(_parse_word(w) for w in raw_words)
        return cls(
            seq=seq,
            committed_delta=committed,
            pending=pending,
            is_final=is_final,
            t_audio_s=float(t_audio_s),
            language=language,
            pass_ms=pass_ms,
            words=words,
        )


def _as_text(value: Any, field_name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StreamingProtocolError(
            f"worker record {field_name!r} is {type(value).__name__}, expected a string"
        )
    if len(value) > maximum:
        raise StreamingProtocolError(
            f"worker record {field_name!r} is {len(value)} characters, past the "
            f"{maximum} per-record ceiling"
        )
    return value


def _as_number(value: Any, field_name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StreamingProtocolError(
            f"worker record {field_name!r} is {type(value).__name__}, expected a number"
        )
    number = float(value)
    if not math.isfinite(number):
        raise StreamingProtocolError(f"worker record {field_name!r} is not finite")
    return number


def _parse_word(raw: Any) -> StreamWord:
    if not isinstance(raw, dict):
        raise StreamingProtocolError(
            f"word entry is {type(raw).__name__}, expected an object"
        )
    text = raw.get("word", raw.get("text", ""))
    if not isinstance(text, str):
        raise StreamingProtocolError("word 'word' is not a string")
    start = _as_number(raw.get("start"), "word start")
    end = _as_number(raw.get("end"), "word end")
    if start < 0.0:
        raise StreamingProtocolError(f"word start ({start}) is negative")
    if end < 0.0:
        raise StreamingProtocolError(f"word end ({end}) is negative")
    if end < start:
        raise StreamingProtocolError(
            f"word end ({end}) precedes word start ({start})"
        )
    probability = raw.get("probability")
    if probability is not None:
        probability = _as_number(probability, "word probability")
        if not 0.0 <= probability <= 1.0:
            raise StreamingProtocolError(
                f"word probability ({probability}) is outside [0, 1]"
            )
    return StreamWord(text=text, start=start, end=end, probability=probability)
