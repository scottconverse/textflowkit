"""The native streaming worker: one child process that owns the DLL.

Run as ``python -m textflowkit.core.stream_worker``. It is *never* imported by
the server or by the CLI: it exists only to be the single owner of the native
engine's global state for exactly one session. The parent
(:mod:`textflowkit.core.streaming`) speaks a newline-delimited JSON protocol to
it over stdio:

    parent -> child   {"cmd": "start", "language": ..., "weights_path": ...}
                      {"cmd": "feed", "audio": "<base64 s16le>"}
                      {"cmd": "finish"}
    child  -> parent  started
                      event {"text": ..., "pending": ..., "words": [...],
                             "consumed_samples": N, "frame_samples": M, ...}
                      final {"text": ..., "consumed_samples": N, ...}  # after finish()
                      error {"message": ...}

``consumed_samples`` is the child's own count of samples it has received and
processed across every fed frame - the parent uses it (not the native engine's
``received`` field) to know how much queued audio has really been consumed.

Audio arrives base64 in JSON - never pickle, never a length-prefixed binary blob
that could desynchronize the stream. The child imports the native engine by
loading the pinned library through :mod:`ctypes` directly; it imports **no**
``needle`` / ``cactus_needle`` SDK and therefore never loads upstream's telemetry
module. Telemetry-off is additionally forced into the child environment by the
parent, reusing :func:`textflowkit.core.whistle_assets.whistle_child_env`.

The engine call itself is the reason this is a process and not a thread: a
native pass can run for seconds and holds global state, so the parent must be
able to cancel a session by killing only this child.
"""

from __future__ import annotations

import base64
import ctypes
import json
import sys

SAMPLE_RATE = 16000

#: Maximum bytes of one protocol line read from stdin. One audio frame is
#: base64 of at most 32 000 bytes (~43 kB) plus a JSON wrapper; 256 KiB is
#: generous headroom, and a line past it is refused rather than buffered.
MAX_CHILD_LINE_BYTES = 256 * 1024

#: The module name the parent launches this worker as. Defined here - the module
#: the name refers to - so the parent's factory and this file cannot disagree.
WORKER_MODULE = "textflowkit.core.stream_worker"


def _emit(keyword: str, payload: dict | None = None) -> None:
    """One protocol line to stdout. Always flushed - the parent reads line-wise."""
    body = json.dumps(payload or {}, ensure_ascii=False)
    sys.stdout.write(f"{keyword} {body}\n")
    sys.stdout.flush()


def _error(message: str) -> None:
    _emit("error", {"message": message})


# --- native binding --------------------------------------------------------


class _Engine:
    """A bound ``libneedle3.dll``. Mirrors the upstream binding exactly.

    Only the streaming symbols are bound: ``needle_load`` to load the pinned
    ``whistle.cact`` into memory, ``needle_stream_transcribe_process`` for one
    chunk, ``needle_stream_transcribe_stop`` for the end-of-stream flush, and
    ``needle_last_error`` for a message when a call returns a negative code.
    """

    def __init__(self, library_path: str, weights_path: str):
        self._lib = ctypes.CDLL(library_path)
        samples = ctypes.POINTER(ctypes.c_float)
        self._lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
        self._lib.needle_load.restype = ctypes.c_int
        self._lib.needle_stream_transcribe_process.argtypes = [
            samples, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_int,
        ]
        self._lib.needle_stream_transcribe_process.restype = ctypes.c_int
        self._lib.needle_stream_transcribe_stop.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self._lib.needle_stream_transcribe_stop.restype = ctypes.c_int
        self._lib.needle_last_error.argtypes = []
        self._lib.needle_last_error.restype = ctypes.c_char_p

        self._buffer = ctypes.create_string_buffer(1 << 18)
        with open(weights_path, "rb") as handle:
            data = handle.read()
        if self._lib.needle_load(data, len(data)) < 0:
            raise RuntimeError(self._last_error())

    def _last_error(self) -> str:
        raw = self._lib.needle_last_error()
        return raw.decode("utf-8", "replace") if raw else "unknown native error"

    def _result(self, code: int) -> dict:
        if code < 0:
            raise RuntimeError(self._last_error())
        return json.loads(self._buffer.value.decode("utf-8", "replace"))

    def process(self, samples: list[float], language: str | None) -> dict:
        array = (ctypes.c_float * len(samples))(*samples) if samples else (ctypes.c_float * 1)()
        language_bytes = language.encode("utf-8") if language else None
        code = self._lib.needle_stream_transcribe_process(
            array, len(samples), language_bytes, None, self._buffer, len(self._buffer)
        )
        return self._result(code)

    def stop(self) -> dict:
        return self._result(
            self._lib.needle_stream_transcribe_stop(self._buffer, len(self._buffer))
        )


def _s16le_to_float(samples_bytes: bytes) -> list[float]:
    """signed-16 little-endian -> float32 in [-1, 1]. Pure standard library."""
    import array

    values = array.array("h")
    values.frombytes(samples_bytes)
    if sys.byteorder == "big":
        values.byteswap()
    return [v / 32768.0 for v in values]


# --- protocol loop ---------------------------------------------------------


def _weights_path(explicit: str | None) -> str:
    # Resolve and *verify* the model through the shared streaming asset helper:
    # an explicit path is validated against the pin in place, and a missing
    # cached model is downloaded (unless offline) and verified before it is used.
    # Never a bare path - an unverified or absent model must not reach the loader.
    try:
        from textflowkit.core.streaming_assets import ensure_streaming_model

        return str(ensure_streaming_model(weights_path=explicit))
    except Exception as exc:
        raise RuntimeError(f"cannot locate the pinned whistle.cact: {exc}") from exc


def _library_path() -> str:
    # The streaming library is a separate, versioned asset under the streaming
    # cache dir. Its resolution lives in streaming_assets; imported lazily so a
    # missing asset surfaces as a protocol error, not an import failure.
    from textflowkit.core.streaming_assets import ensure_streaming_library

    return str(ensure_streaming_library())


def _iter_commands():
    """Yield protocol lines from stdin, refusing any line past the byte ceiling.

    A bounded ``readline`` means a caller that never sends a newline cannot make
    the child buffer without limit; the oversize line is reported as a protocol
    error and dropped rather than parsed.
    """
    while True:
        raw = sys.stdin.readline(MAX_CHILD_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_CHILD_LINE_BYTES and not raw.endswith("\n"):
            _error(f"command line exceeded {MAX_CHILD_LINE_BYTES} bytes")
            # Discard the rest of the runaway line before resynchronizing.
            while raw and not raw.endswith("\n"):
                raw = sys.stdin.readline(MAX_CHILD_LINE_BYTES + 1)
            continue
        yield raw


def main(argv: list[str] | None = None) -> int:
    engine = None
    language = None
    #: Samples this child has actually *received and processed* on a frame, kept
    #: by the child itself. The parent subtracts queued audio from this count -
    #: not from the native engine's own ``received`` field, which is the engine's
    #: internal clock and need not match what was delivered frame by frame.
    consumed_samples = 0
    for raw in _iter_commands():
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _error(f"malformed command: {exc}")
            continue
        command = message.get("cmd")
        try:
            if command == "start":
                language = message.get("language")
                engine = _Engine(_library_path(), _weights_path(message.get("weights_path")))
                _emit("started")
            elif command == "feed":
                if engine is None:
                    _error("feed before start")
                    continue
                audio = base64.b64decode(message.get("audio", ""), validate=True)
                samples = len(audio) // 2
                consumed_samples += samples
                record = engine.process(_s16le_to_float(audio), language)
                record["consumed_samples"] = consumed_samples
                record["frame_samples"] = samples
                _emit("event", record)
            elif command == "finish":
                if engine is None:
                    _error("finish before start")
                    continue
                record = engine.stop()
                record["consumed_samples"] = consumed_samples
                _emit("final", record)
                _emit("done")
                return 0
            else:
                _error(f"unknown command: {command!r}")
        except Exception as exc:  # noqa: BLE001 - report and keep the pipe open
            _error(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a child process
    sys.exit(main())
