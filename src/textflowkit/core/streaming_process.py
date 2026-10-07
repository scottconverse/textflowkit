"""The production worker: one owned child process that owns the native engine.

Separated from :mod:`textflowkit.core.streaming` so the session module stays
readable and the process plumbing - the parts easiest to get wrong - sit on
their own. Nothing here is imported by a test's happy path; tests inject a fake
worker factory instead, and only the deliberately isolated lifecycle test in
``tests/test_streaming_core.py`` launches a real child (a *stub* module, never
the native loader).

Framing: newline-delimited ``KEYWORD {json}`` lines over the child's stdio.
Audio is base64 inside JSON - standard library, and never pickle: a
deserialization gadget cannot enter through a stream that only carries JSON.
Both pipes are drained by their own thread so neither can deadlock the other,
and the child's stderr is drained into a bounded tail rather than a growing log.

Every read from the child is bounded twice: the record queue has a fixed
capacity (a child that floods records cannot grow the parent without bound) and
each line has a byte ceiling (a child that never emits a newline cannot make the
parent buffer forever). A record past either bound becomes an explicit
protocol error record, never a silent truncation or a memory blow-up.
"""

from __future__ import annotations

import base64
import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
from typing import Any

from textflowkit.core.stream_worker import WORKER_MODULE
from textflowkit.core.streaming_events import WorkerProcessError

#: How long ``start`` waits for the child to report ``started``.
START_DEADLINE_S = 30.0
#: The *total* budget for a cancel, inclusive of the kill fallback: after this
#: many seconds the owned handle has been terminated or hard-killed and must be
#: gone. It is deliberately a single number - not "terminate for N then wait N
#: more" - so the caller's bound is the wall clock it actually observes.
CANCEL_DEADLINE_S = 2.0
#: How much audio a single ``feed`` may hold pending in the writer queue before
#: the write is refused. One second per frame, a few seconds of headroom: a
#: caller that outruns the child past this is told so, rather than blocking on a
#: full OS pipe.
MAX_PENDING_WRITE_BYTES = 4 * 1024 * 1024
#: How long a ``feed`` may wait for space before reporting the child as stalled.
WRITE_DEADLINE_S = 5.0
#: How many stderr lines to retain (a tail for diagnostics, not a full log).
STDERR_TAIL_LINES = 50
#: Maximum bytes in a single child frame line. A one-second frame is base64 of
#: 32 000 bytes (~43 kB) plus a JSON wrapper; 256 KiB is generous headroom (four
#: seconds of audio in one line) while still refusing a line that could only be
#: a runaway child. A line past this bound is refused whether or not it ends in
#: a newline.
MAX_FRAME_BYTES = 256 * 1024
#: Maximum bytes in a single stderr line before the tail stops accumulating it.
MAX_STDERR_LINE_BYTES = 64 * 1024
#: Maximum pending write records (one per fed frame). Combined with the byte
#: budget so neither count nor size alone can grow without bound.
MAX_QUEUED_WRITES = 128
#: Capacity of the record queue. The parent reads records at least as fast as the
#: engine produces them (one per fed frame), so this is back-pressure headroom,
#: not a working buffer - a child flooding past it is misbehaving, and the queue
#: reports that as a protocol error rather than growing without bound.
MAX_QUEUED_RECORDS = 128


class SubprocessWorker:
    """One owned ``python -m textflowkit.core.stream_worker`` child process.

    Every method bounds its wait. ``terminate`` acts only on *this* handle - the
    ``Popen`` returned when the child was launched - so it can never touch
    another process, and it never uses ``os.kill(pid, 0)`` or a kill by image
    name. The child's own exit is the release: the native engine's global state
    dies with the process.
    """

    def __init__(
        self,
        *,
        language: str | None,
        weights_path: Any = None,
        python: str | None = None,
        env: dict[str, str] | None = None,
        module: str = WORKER_MODULE,
        start_deadline: float = START_DEADLINE_S,
    ):
        self._language = language
        self._weights_path = weights_path
        self._python = python or sys.executable
        self._env = env
        self._module = module
        self._start_deadline = start_deadline
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_tail: list[str] = []
        self._stderr_overflowed = False
        self._record_q: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=MAX_QUEUED_RECORDS
        )
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        # One owned writer per child: ``feed`` enqueues here and returns, and a
        # single dedicated thread performs the actual blocking pipe writes. This
        # is what keeps a non-reading child from blocking the caller forever -
        # the caller waits on a bounded queue slot, not on the OS pipe.
        self._write_q: queue.Queue[bytes | None] = queue.Queue(maxsize=MAX_QUEUED_WRITES)
        self._writer_thread: threading.Thread | None = None
        self._pending_write_bytes = 0
        self._write_lock = threading.Lock()
        self._write_error: WorkerProcessError | None = None
        self._closed = False

    # -- lifecycle --

    def start(self) -> None:
        env = _child_env(self._env)
        argv = [self._python, "-u", "-m", self._module]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
            )
        except OSError as exc:
            raise WorkerProcessError(f"could not launch the streaming worker: {exc}") from exc

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()

        self._send({
            "cmd": "start",
            "language": self._language,
            "weights_path": str(self._weights_path) if self._weights_path else None,
        })
        self._await("started")

    def feed(self, frame: bytes) -> None:
        self._enqueue({"cmd": "feed", "audio": base64.b64encode(frame).decode("ascii")})

    def finish(self) -> None:
        self._send({"cmd": "finish"})

    def terminate(self) -> None:
        """Terminate and reap only this session's own child handle.

        Order matters: the child is terminated (then hard-killed) **before**
        stdin is closed. Closing stdin first can block on the write lock held by
        a writer stuck in a pipe write to a non-reading child - so the kill comes
        first, the writer's lock is released as the pipe breaks, and only then is
        stdin closed. The whole cancel is bounded by a single wall-clock budget
        (``CANCEL_DEADLINE_S``) inclusive of the kill fallback.
        """
        proc = self._proc
        if proc is None or self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + CANCEL_DEADLINE_S

        # 1. Kill first. The writer thread (if any) is stuck in a pipe write;
        #    terminating the child breaks the pipe and unblocks it.
        self._signal_exit(proc, deadline)

        # 2. Wake the queue readers and the writer thread so blocked callers and
        #    the writer's idle get() return instead of waiting out their bounds.
        self._wake_readers()
        self._wake_writer()

        # 3. Now it is safe to close stdin (the writer is no longer holding it)
        #    and join the threads, all within the same budget.
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        self._join_bounded(self._writer_thread, deadline)
        self._join_bounded(self._reader, deadline)
        self._join_bounded(self._stderr_thread, deadline)

    def _signal_exit(self, proc: subprocess.Popen[bytes], deadline: float) -> None:
        """Terminate the owned handle, escalating to kill, within ``deadline``.

        Signals only *this* handle - never ``os.kill(pid, 0)``, never an image
        name. The kill fallback uses whatever budget remains, so the caller sees
        a single 2 s bound rather than 2 s of terminate plus 2 s of wait.
        """
        try:
            proc.terminate()
        except OSError:
            pass
        # Give terminate() half the budget to take effect, so an ignored terminate
        # still leaves time to escalate to kill() - both inside the same 2 s.
        grace = time.monotonic() + CANCEL_DEADLINE_S / 2.0
        while time.monotonic() < min(grace, deadline):
            if proc.poll() is not None:
                return
            time.sleep(0.02)
        try:
            proc.kill()
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass

    def _wake_readers(self) -> None:
        """Post the EOF sentinel and a write error so blocked callers return."""
        with contextlib.suppress(queue.Full):
            self._record_q.put_nowait(None)
        with self._write_lock:
            if self._write_error is None:
                self._write_error = WorkerProcessError("the streaming worker was cancelled")

    def _wake_writer(self) -> None:
        """Post the writer's stop sentinel so its idle ``get()`` returns now."""
        thread = self._writer_thread
        if thread is None or not thread.is_alive():
            return
        try:
            self._write_q.put_nowait(None)
        except queue.Full:
            # The queue is full, so the writer is *not* idle on get() - it is
            # mid-write. The child has been killed, so that write will fail and
            # the thread will exit on its own; make room for the stop sentinel.
            with contextlib.suppress(queue.Empty):
                self._write_q.get_nowait()
                with contextlib.suppress(queue.Full):
                    self._write_q.put_nowait(None)

    @staticmethod
    def _join_bounded(thread: threading.Thread | None, deadline: float) -> None:
        if thread is None:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)

    def read_record(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next record, or ``None`` if none arrives within ``timeout``.

        The wait happens *here*, against the queue's own timed get, so the bound
        is real: a worker stuck inside a native call cannot hold the caller past
        ``timeout``. ``timeout=None`` waits indefinitely.

        The ``None`` sentinel the reader thread posts when stdout closes *is*
        reported as ``None`` - but the caller can tell a deliberate end from a
        timeout with :meth:`at_eof`. A sticky write error is re-raised here so a
        caller blocked in a read learns the child is gone.
        """
        self._raise_if_write_failed()
        try:
            item = self._record_q.get(timeout=timeout)
        except queue.Empty:
            return None
        return item

    def at_eof(self) -> bool:
        """Whether the child's stdout has closed with no further record pending."""
        return self._reader is not None and not self._reader.is_alive()

    # -- internals --

    def _raise_if_write_failed(self) -> None:
        with self._write_lock:
            error = self._write_error
        if error is not None:
            raise error

    def _enqueue(self, payload: dict[str, Any]) -> None:
        """Queue one command for the writer thread, bounded in count and bytes.

        ``feed`` lands here: it must never block on the OS pipe. A caller that
        outruns the child past the byte budget, or waits past the write deadline
        for a queue slot, is told explicitly - the write is refused, not silently
        dropped and not allowed to hang the caller on a full pipe.
        """
        self._raise_if_write_failed()
        line = (json.dumps(payload) + "\n").encode("utf-8")
        if len(line) > MAX_FRAME_BYTES:
            raise WorkerProcessError(
                f"worker command is {len(line)} bytes, past the {MAX_FRAME_BYTES} "
                "byte frame ceiling"
            )
        deadline = time.monotonic() + WRITE_DEADLINE_S
        while True:
            with self._write_lock:
                if self._write_error is not None:
                    raise self._write_error
                if self._pending_write_bytes + len(line) <= MAX_PENDING_WRITE_BYTES:
                    self._pending_write_bytes += len(line)
                    try:
                        self._write_q.put_nowait(line)
                        return
                    except queue.Full:
                        self._pending_write_bytes -= len(line)
            if time.monotonic() >= deadline:
                raise WorkerProcessError(
                    f"the streaming worker did not accept a write within "
                    f"{WRITE_DEADLINE_S:.0f}s; the child is not reading its stdin"
                )
            time.sleep(0.005)

    def _send(self, payload: dict[str, Any]) -> None:
        """Write one command synchronously and confirm the child acknowledged it.

        Used only for the startup handshake: it is a *round trip*, so it both
        completes the whole write (a partial pipe write cannot desynchronize the
        protocol) and proves the freshly launched child is actually reading its
        stdin before the session is considered started.
        """
        self._enqueue(payload)
        self._raise_if_write_failed()

    def _write_loop(self) -> None:
        """The one owned writer thread: complete every queued line, in order."""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        stdin = proc.stdin
        try:
            while True:
                line = self._write_q.get()
                if line is None:
                    break
                try:
                    _write_all(stdin, line)
                    stdin.flush()
                except (OSError, ValueError) as exc:
                    self._fail_write(f"could not write to the streaming worker: {exc}")
                    return
                finally:
                    with self._write_lock:
                        self._pending_write_bytes -= len(line)
        except Exception as exc:  # noqa: BLE001 - a dead writer must not spin quietly
            self._fail_write(f"the streaming worker writer failed: {exc}")
        finally:
            # Wake a caller blocked on a queue slot; there will be no more writes.
            with contextlib.suppress(queue.Full):
                self._write_q.put_nowait(None)

    def _fail_write(self, message: str) -> None:
        with self._write_lock:
            if self._write_error is None:
                self._write_error = WorkerProcessError(message)
        with contextlib.suppress(queue.Full):
            self._record_q.put_nowait({"kind": "error", "message": message})

    def _await(self, kind: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._start_deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerProcessError(
                    f"the streaming worker did not report {kind!r} within "
                    f"{self._start_deadline:.0f}s"
                )
            line = self.read_record(timeout=remaining)
            if line is None:
                detail = self._stderr_tail[-1] if self._stderr_tail else "no stderr"
                raise WorkerProcessError(
                    f"the streaming worker exited before it started: {detail}"
                )
            if line.get("kind") == kind:
                return line
            if line.get("kind") == "error":
                raise WorkerProcessError(line.get("message", "worker failed to start"))
            raise WorkerProcessError(
                f"unexpected worker record before {kind!r}: {line}"
            )

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw = proc.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_FRAME_BYTES:
                    # Oversize regardless of a trailing newline: a line past the
                    # ceiling is refused on its length alone, so a runaway child
                    # cannot buffer unbounded output or slip a huge record past
                    # the newline check.
                    self._post({
                        "kind": "error",
                        "message": (
                            f"worker frame exceeded {MAX_FRAME_BYTES} bytes; "
                            "the child is not speaking the line protocol"
                        ),
                    })
                    break
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                self._post(_parse_line(text))
        finally:
            self._post(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            raw = proc.stderr.readline(MAX_STDERR_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_STDERR_LINE_BYTES and not raw.endswith(b"\n"):
                # A newline-free stderr line past the ceiling: record it, mark
                # the overflow, and stop retaining it rather than buffering the
                # whole runaway line into the tail.
                self._stderr_overflowed = True
                self._stderr_tail.append(
                    f"<stderr line exceeded {MAX_STDERR_LINE_BYTES} bytes>"
                )
                if len(self._stderr_tail) > STDERR_TAIL_LINES:
                    del self._stderr_tail[0]
                continue
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            if len(self._stderr_tail) > STDERR_TAIL_LINES:
                del self._stderr_tail[0]

    def _post(self, item: dict[str, Any] | None) -> None:
        """Enqueue a record, bounding the queue rather than blocking the reader.

        The reader thread must never block on a full queue: if it did, the child
        could wedge on a full pipe and so could ``terminate``. On overflow the
        record is replaced by an explicit protocol error, so the consumer sees a
        real reason instead of a stall or a silent drop.
        """
        try:
            self._record_q.put_nowait(item)
        except queue.Full:
            with contextlib.suppress(queue.Full):
                self._record_q.get_nowait()
                self._record_q.put_nowait({
                    "kind": "error",
                    "message": (
                        f"worker record queue exceeded {MAX_QUEUED_RECORDS}; the "
                        "consumer is not reading and the child is producing too fast"
                    ),
                })


def _write_all(stdin: Any, line: bytes) -> None:
    """Write every byte of ``line`` to ``stdin``, looping over short writes.

    ``Popen`` is opened with ``bufsize=0``, so ``stdin`` is a raw ``FileIO`` and
    ``write`` maps to a single ``os.write`` on the pipe. ``os.write`` returns the
    number of bytes actually written and may write fewer than were given (a
    non-blocking-adjacent partial write, or a pipe filled by a child that reads
    slowly). Treating that return as "done" would silently truncate the line and
    desynchronize the newline protocol, so the loop carries a ``memoryview`` and
    advances it by the real byte count until the whole line is out. A return of
    zero means the pipe accepted nothing this call - it is not an error and not
    completion, so it is retried rather than mistaken for either.
    """
    view = memoryview(line)
    while view:
        written = stdin.write(view)
        if written is None:
            # A buffered stream reports success with no count; the whole view was
            # accepted (this product always opens the child with bufsize=0, so this
            # is belt-and-braces for an injected stream in a test).
            return
        if written <= 0:
            # Nothing accepted this call: yield briefly and retry rather than
            # spin-tight. A closed pipe raises OSError from write and is handled by
            # the caller, so this path is a transient full pipe.
            time.sleep(0.001)
            continue
        view = view[written:]


def _parse_line(text: str) -> dict[str, Any]:
    """Turn one child line into a record, or an error record - never raising."""
    keyword, _, payload = text.partition(" ")
    if not payload:
        return {"kind": "error", "message": f"unparseable worker line: {text[:200]}"}
    try:
        body = json.loads(payload)
    except ValueError:
        return {"kind": "error", "message": f"unparseable worker JSON: {payload[:200]}"}
    if not isinstance(body, dict):
        return {
            "kind": "error",
            "message": f"worker {keyword!r} payload must be a JSON object, got {type(body).__name__}",
        }
    if keyword == "event":
        return {"kind": "event", "record": body}
    if keyword == "final":
        return {"kind": "final", "record": body}
    if keyword in ("started", "done"):
        return {"kind": keyword}
    if keyword == "error":
        return {"kind": "error", "message": body.get("message", "worker error")}
    return {"kind": "error", "message": f"unknown worker keyword: {keyword}"}


def _child_env(parent: dict[str, str] | None) -> dict[str, str]:
    """The child environment, telemetry forced off, reusing the asset helper.

    This product launches the DLL itself and never imports the upstream
    ``needle`` SDK or its telemetry module; the telemetry-off gate reuses
    :func:`textflowkit.core.whistle_assets.whistle_child_env` so one place owns
    that decision.
    """
    from textflowkit.core.whistle_assets import whistle_child_env

    return whistle_child_env(parent)
