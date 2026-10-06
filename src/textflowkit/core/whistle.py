"""The Whistle speech-to-text engine.

Whistle runs as a native CLI (see :mod:`textflowkit.core.whistle_assets`) over
one ``.cact`` model. This module wraps it behind the same tiny :class:`Engine`
interface the rest of the pipeline already speaks, so nothing downstream has to
know which engine produced a transcript.

The native CLI transcribes **one clip of at most 30 seconds** in a single pass
and refuses a longer file outright. Long audio is therefore cut into short,
overlapping windows here and the results stitched back onto the recording's own
timeline:

- Each window owns a **26 s core**, with up to **2 s of context** taken from
  either side. So every standalone call sees at most 30 s, and the core it is
  responsible for is at most 26 s.
- Words that fall in the overlap between two windows are assigned to exactly one
  of them by a **midpoint rule**: a word belongs to the window whose core
  contains the word's midpoint; the very last core also takes the final edge.
  Adjacent windows thus contribute no duplicate word, and a repeated phrase in
  the audio is preserved as many times as it is spoken - nothing here dedupes by
  text.
- Word times come back relative to the window and are shifted to **absolute**
  recording time.

Only the committed standalone output is used. The streaming mode's provisional
``pending`` text is never read.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textflowkit.core import whistle_assets
from textflowkit.core.cancel import CancelledError
from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.core.whistle_assets import (
    WhistleAssetError,
    ensure_assets,
    whistle_child_env,
)

#: The single logical model name Whistle exposes. The native CLI reads whichever
#: ``.cact`` it is given, so there is exactly one correct answer here.
WHISTLE_MODEL_NAME = "whistle"

#: The languages upstream advertises for Whistle. A requested language is
#: checked against this *before* any asset is fetched, so a typo costs nothing.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "de", "fr", "es", "it", "nl", "pl")

#: The native CLI refuses a clip longer than this; every window stays under it.
MAX_CLIP_SECONDS = 30.0
#: The span each window is *responsible* for. Context either side is the slack.
CORE_SECONDS = 26.0
#: Context taken each side of the core, capped so core + 2 * context <= 30.
MAX_CONTEXT_SECONDS = 2.0

#: Wall-clock ceiling for one window's native call.
DEFAULT_WINDOW_TIMEOUT_SECONDS = 120.0

#: A repaired zero-or-negative-duration word gets this length, if the bounds
#: allow it. One millisecond matches the subtitle timestamp resolution.
REPAIR_INTERVAL_SECONDS = 0.001

#: Timestamps are compared with this slack so a value that is merely rounded
#: (e.g. 26.000000001) is not rejected as out of bounds.
TIMESTAMP_EPSILON_SECONDS = 1e-3

#: A word is allowed to reach this far outside its window before it is refused
#: as grossly out of range rather than trimmed. The engine's own boundaries and
#: the window edges do not always agree to the millisecond.
WORD_BOUNDS_TOLERANCE_SECONDS = 0.5

#: Read size when streaming a WAV. Keeps memory flat on a long recording.
_WAV_BLOCK_FRAMES = 16000  # one second of 16 kHz mono s16le

#: Hard ceiling on the native CLI's stdout. The committed standalone answer is
#: one small JSON object - a few kilobytes at most for a 30 s clip. A child that
#: writes past this is not producing that answer (a runaway loop, a corrupted
#: build, or a stream of records), so the run is refused rather than buffered.
#: 1 MiB is generous for the answer and small enough that a chatty child cannot
#: exhaust memory.
MAX_STDOUT_BYTES = 1024 * 1024

#: Resume-progress schema version. Bumped if the stored shape changes.
PROGRESS_SCHEMA = "textflowkit.whistle.progress/1"
#: The windowing policy, recorded so a resume computed under a different policy
#: is refused rather than silently reused.
WINDOW_POLICY = f"core={CORE_SECONDS:g};context<={MAX_CONTEXT_SECONDS:g};max={MAX_CLIP_SECONDS:g}"

#: A pause at least this long (or sentence-ending punctuation) starts a new
#: segment, so a long transcript becomes sane SRT/VTT cues rather than one block.
PAUSE_SPLIT_SECONDS = 0.8
#: A single segment is never allowed to run longer than this.
MAX_SEGMENT_SECONDS = 12.0
#: A sentence-ending character here closes a segment.
_SENTENCE_END = ".!?。！？"


class WhistleError(RuntimeError):
    """Raised when the Whistle engine cannot produce a transcript."""


# --- audio reading ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int

    @property
    def duration(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0


def read_wav_info(audio_path: str | Path) -> _WavInfo:
    """Return the format and frame count of a PCM WAV, without loading it."""
    try:
        with wave.open(str(audio_path), "rb") as wav:
            info = _WavInfo(
                sample_rate=wav.getframerate(),
                channels=wav.getnchannels(),
                sample_width=wav.getsampwidth(),
                frame_count=wav.getnframes(),
            )
    except (OSError, EOFError, wave.Error) as exc:
        raise WhistleError(f"cannot read audio '{audio_path}': {exc}") from exc
    if info.sample_rate <= 0:
        raise WhistleError(f"audio '{audio_path}' has no sample rate")
    if info.channels <= 0:
        raise WhistleError(f"audio '{audio_path}' has no channels")
    return info


@dataclass(frozen=True, slots=True)
class _Window:
    index: int
    #: Absolute recording time of the core this window owns.
    core_start: float
    core_end: float
    #: Absolute recording time of everything this window is given (core + context).
    clip_start: float
    clip_end: float


def plan_windows(duration: float, *, core: float = CORE_SECONDS,
                 context: float = MAX_CONTEXT_SECONDS,
                 max_clip: float = MAX_CLIP_SECONDS) -> list[_Window]:
    """Tile ``[0, duration)`` into cores with bounded context either side.

    Cores are consecutive and non-overlapping and cover the whole recording.
    Each window's clip is its core widened by ``context`` on each side, clamped
    to the recording, and never longer than ``max_clip``. The core is never
    longer than ``max_clip - 2 * context``.
    """
    if duration <= 0:
        return []
    core = min(core, max_clip - 2 * context)
    if core <= 0:
        raise WhistleError("core window is not positive after context is subtracted")

    windows: list[_Window] = []
    index = 0
    start = 0.0
    while start < duration - TIMESTAMP_EPSILON_SECONDS:
        end = min(start + core, duration)
        clip_start = max(0.0, start - context)
        clip_end = min(duration, end + context)
        # Never exceed the native limit, whatever the boundary arithmetic did.
        if clip_end - clip_start > max_clip:
            clip_end = clip_start + max_clip
        windows.append(_Window(index, start, end, clip_start, clip_end))
        start = end
        index += 1
    return windows


def _write_window_wav(source: str | Path, window: _Window, destination: Path) -> None:
    """Copy one window's frames from ``source`` into a standalone WAV.

    Read in blocks and written in blocks: no full recording is ever held in
    memory, and only the window's own frames are written.
    """
    info = read_wav_info(source)
    first_frame = round(window.clip_start * info.sample_rate)
    last_frame = round(window.clip_end * info.sample_rate)
    first_frame = max(0, min(first_frame, info.frame_count))
    last_frame = max(first_frame, min(last_frame, info.frame_count))
    remaining = last_frame - first_frame

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(source), "rb") as src:
            src.setpos(first_frame)
            with wave.open(str(destination), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                while remaining > 0:
                    frames = min(_WAV_BLOCK_FRAMES, remaining)
                    data = src.readframes(frames)
                    if not data:
                        break
                    out.writeframesraw(data)
                    remaining -= frames
    except (OSError, EOFError, wave.Error) as exc:
        destination.unlink(missing_ok=True)
        raise WhistleError(f"cannot slice audio for transcription: {exc}") from exc


# --- word validation and repair ------------------------------------------


@dataclass(slots=True)
class _ParsedWord:
    text: str
    start: float  # absolute
    end: float  # absolute
    probability: float | None


@dataclass(slots=True)
class Repairs:
    """Counts of the timestamp repairs and refusals applied to engine output."""

    zero_duration_repaired: int = 0
    clamped_to_window: int = 0
    rejected_reversed: int = 0
    rejected_nonfinite: int = 0
    rejected_probability: int = 0
    rejected_out_of_range: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "zero_duration_repaired": self.zero_duration_repaired,
            "clamped_to_window": self.clamped_to_window,
            "rejected_reversed": self.rejected_reversed,
            "rejected_nonfinite": self.rejected_nonfinite,
            "rejected_probability": self.rejected_probability,
            "rejected_out_of_range": self.rejected_out_of_range,
        }

    @property
    def total(self) -> int:
        return sum(self.to_dict().values())


def _own_word(entry: Any) -> _ParsedWord:
    """Validate one raw word and return it in clip-relative time, or raise.

    Raises :class:`WhistleError` for an entry whose *shape* or *values* are not
    something the engine could have meant to say - a non-dict, a missing or
    non-string word, a missing timestamp, a non-numeric or non-finite timestamp,
    or a probability outside [0, 1]. Such an entry is a defect in the engine's
    answer, not a word to be discarded: the caller rejects the whole window
    rather than silently lose whatever speech sat next to it.
    """
    if not isinstance(entry, dict):
        raise WhistleError("Whistle returned a word entry that is not an object")
    text = entry.get("word")
    if not isinstance(text, str) or not text.strip():
        raise WhistleError("Whistle returned a word entry with no word text")
    start = entry.get("start")
    end = entry.get("end")
    if start is None or end is None:
        raise WhistleError("Whistle returned a word entry with no timestamps")
    try:
        s = float(start)
        e = float(end)
    except (TypeError, ValueError) as exc:
        raise WhistleError("Whistle returned a word with a non-numeric timestamp") from exc
    if not (math.isfinite(s) and math.isfinite(e)):
        raise WhistleError("Whistle returned a word with a non-finite timestamp")

    probability = entry.get("probability")
    prob: float | None = None
    if probability is not None:
        try:
            prob = float(probability)
        except (TypeError, ValueError) as exc:
            raise WhistleError("Whistle returned a word with a non-numeric probability") from exc
        if not math.isfinite(prob) or prob < 0.0 or prob > 1.0:
            raise WhistleError("Whistle returned a word with a probability outside [0, 1]")
    return _ParsedWord(text=text.strip(), start=s, end=e, probability=prob)


def _place_word(word: _ParsedWord, *, offset: float, clip_start: float, clip_end: float,
                repairs: Repairs) -> _ParsedWord | None:
    """Shift a validated word to absolute time, repair or drop it, or refuse.

    Returns the word placed, or ``None`` when the word is outside the window and
    therefore not this window's speech (a counted refusal, not a defect in the
    answer's shape). A rounding-sized overshoot is clamped; a zero-or-negative
    duration inside the window is repaired deterministically to a small positive
    interval. A word that cannot be repaired inside the window is refused.
    """
    abs_start = word.start + offset
    abs_end = word.end + offset

    # Gross out-of-range: the word does not belong to this window at all. The
    # tolerance makes the boundary explicit: a word may reach up to
    # WORD_BOUNDS_TOLERANCE_SECONDS past an edge (the engine's own boundaries and
    # the window edges do not agree to the millisecond); beyond that it is not
    # this window's speech.
    if (abs_start < clip_start - WORD_BOUNDS_TOLERANCE_SECONDS
            or abs_end > clip_end + WORD_BOUNDS_TOLERANCE_SECONDS):
        repairs.rejected_out_of_range += 1
        return None

    # Reversed (end strictly before start) by more than the rounding tolerance is
    # a defect, not a word: refuse the whole window rather than drop it silently.
    if abs_end < abs_start - TIMESTAMP_EPSILON_SECONDS:
        raise WhistleError(
            "Whistle returned a word whose end precedes its start"
        )

    # A rounding-sized overshoot is clamped to the window, not dropped.
    if abs_start < clip_start or abs_end > clip_end:
        abs_start = max(abs_start, clip_start)
        abs_end = min(abs_end, clip_end)
        repairs.clamped_to_window += 1

    # Zero/negative duration: repair deterministically inside the window. A
    # duration at or below the rounding tolerance counts as zero.
    if abs_end - abs_start <= TIMESTAMP_EPSILON_SECONDS:
        repaired_end = abs_start + REPAIR_INTERVAL_SECONDS
        if repaired_end > clip_end:
            # No room after the start: walk the start back instead, but never
            # before the window begins.
            repaired_start = abs_end - REPAIR_INTERVAL_SECONDS
            if repaired_start < clip_start:
                repairs.rejected_out_of_range += 1
                return None
            abs_start = repaired_start
        else:
            abs_end = repaired_end
        repairs.zero_duration_repaired += 1

    return _ParsedWord(text=word.text, start=abs_start, end=abs_end,
                       probability=word.probability)


def _parse_words(raw_words: Any, *, offset: float, clip_start: float, clip_end: float,
                 repairs: Repairs) -> list[_ParsedWord]:
    """Validate one window's words and shift them to absolute time.

    A word whose *values* are unusable - a non-finite or reversed interval, a
    probability outside [0, 1], a malformed entry - is a defect in the engine's
    answer, so the whole window is refused (:class:`WhistleError`) rather than
    that word being silently dropped and the rest of the window kept. Silently
    losing one word of many would quietly corrupt the transcript while looking
    like success; a caller must instead see that this window failed and decide.

    A word that is merely *outside this window* - beyond the tolerance at the
    edge - is not this window's speech, and is refused with a counted reason.
    That is a normal boundary case across windows, not a defect.
    """
    if not isinstance(raw_words, list):
        raise WhistleError("Whistle 'words' was not a list")
    placed: list[_ParsedWord] = []
    for entry in raw_words:
        word = _place_word(_own_word(entry), offset=offset, clip_start=clip_start,
                           clip_end=clip_end, repairs=repairs)
        if word is not None:
            placed.append(word)
    return placed


def assign_ownership(windows: list[_Window], words: list[_ParsedWord]) -> list[_ParsedWord]:
    """Partition words across windows by the midpoint rule, in time order.

    A convenience over :func:`_own_in_core` for a whole plan at once: exactly
    one window owns each word, and the result is sorted by absolute time so it
    reads as one transcript. Used by tests to assert the plan tiles without
    loss or duplication; the engine applies the same rule window by window.
    """
    kept: list[_ParsedWord] = []
    for i, window in enumerate(windows):
        kept.extend(_own_in_core(window, words, is_last=i == len(windows) - 1))
    kept.sort(key=lambda w: (w.start, w.end))
    return kept


# --- segmentation ----------------------------------------------------------


def segment_words(words: list[_ParsedWord]) -> list[Segment]:
    """Group owned words into sentence/pause-sized segments.

    A new segment begins after sentence-ending punctuation or a pause at least
    :data:`PAUSE_SPLIT_SECONDS` long, and a segment never runs past
    :data:`MAX_SEGMENT_SECONDS`. Words with no text between them are joined with
    a single space.
    """
    segments: list[Segment] = []
    current: list[_ParsedWord] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(w.text for w in current if w.text)
        if not text:
            return
        segments.append(
            Segment(
                start=current[0].start,
                end=current[-1].end,
                text=text,
                words=[
                    WordTiming(start=w.start, end=w.end, text=w.text) for w in current
                ],
            )
        )

    for word in words:
        if current:
            gap = word.start - current[-1].end
            would_exceed = (word.end - current[0].start) > MAX_SEGMENT_SECONDS
            if gap >= PAUSE_SPLIT_SECONDS or would_exceed:
                flush()
                current = []
        current.append(word)
        if current[-1].text.endswith(tuple(_SENTENCE_END)):
            flush()
            current = []
    flush()
    return segments


# --- process launch --------------------------------------------------------


@dataclass(slots=True)
class _LaunchResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    #: True when the child wrote more than :data:`MAX_STDOUT_BYTES` and was cut
    #: off. Recorded rather than raised inside the reader thread so the run is
    #: failed, and the child killed, on the main thread.
    stdout_overflowed: bool = False


def _kill_owned_tree(proc: subprocess.Popen) -> None:
    """Terminate exactly this process's tree, and nothing by image name."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _launch_argv(command: list[str]) -> list[str]:
    """The argv ``Popen`` should actually run.

    Windows ``CreateProcess`` cannot execute a ``.cmd``/``.bat`` directly, so a
    batch launcher is routed through ``cmd.exe /c``. This never uses
    ``shell=True``: the program and its arguments stay a single argv list, so
    there is no shell-metacharacter surface. In production the resolved binary
    is upstream's ``needle.exe``, so this branch is not taken; it exists so a
    batch-file launcher is handled correctly rather than silently failing.
    """
    if os.name == "nt" and command and command[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
    return command


def _run_native(
    command: list[str],
    *,
    timeout: float,
    check_cancel: Callable[[], None] | None,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = 8192,
) -> _LaunchResult:
    """Run the native CLI once, bounded in time and in output size.

    Cancellation is polled while the child runs and the child's exact owned
    process tree is killed on timeout or cancellation. stdout and stderr are
    drained on their own threads so a child cannot deadlock on a full pipe.

    Both streams are bounded. stderr keeps only a tail. stdout is capped at
    ``stdout_limit``: the committed standalone answer is one small JSON object,
    so a child that writes past the cap is misbehaving, and the run is failed
    and the child killed rather than buffering an unbounded stream.
    """
    env = whistle_child_env()
    if check_cancel is not None:
        check_cancel()
    try:
        proc = subprocess.Popen(
            _launch_argv(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(Path(tempfile.gettempdir())),
        )
    except OSError as exc:
        raise WhistleError(f"Whistle runtime could not start: {exc}") from exc

    out_chunks: list[bytes] = []
    err_tail = bytearray()
    overflowed = threading.Event()
    # Everything the reader threads touch is guarded by this lock, so the main
    # thread can read a consistent snapshot after the child exits or is killed.
    out_lock = threading.Lock()
    err_lock = threading.Lock()

    def _read_out() -> None:
        assert proc.stdout is not None
        total = 0
        try:
            while chunk := proc.stdout.read(65536):
                with out_lock:
                    remaining = stdout_limit - total
                    if remaining <= 0:
                        overflowed.set()
                        break
                    out_chunks.append(chunk[:remaining])
                    total += len(chunk)
                if total >= stdout_limit:
                    overflowed.set()
                    break
        except (ValueError, OSError):
            # The main thread closed the pipe as part of a kill; that is not an
            # output error.
            pass

    def _read_err() -> None:
        assert proc.stderr is not None
        try:
            while chunk := proc.stderr.read(4096):
                with err_lock:
                    err_tail.extend(chunk)
                    if len(err_tail) > stderr_limit:
                        del err_tail[:-stderr_limit]
        except (ValueError, OSError):
            pass

    out_thread = threading.Thread(target=_read_out, daemon=True)
    err_thread = threading.Thread(target=_read_err, daemon=True)
    out_thread.start()
    err_thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False

    def _abandon() -> None:
        """Kill the child, then close the pipes so the readers cannot block."""
        _kill_owned_tree(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    try:
        while proc.poll() is None:
            if check_cancel is not None:
                try:
                    check_cancel()
                except CancelledError:
                    _abandon()
                    raise
            if overflowed.is_set():
                # The child flooded stdout; stop it now rather than let it run
                # to the timeout with a pipe we have stopped reading.
                _abandon()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _abandon()
                break
            time.sleep(0.05)
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        returncode = proc.returncode if proc.returncode is not None else -1
    except BaseException:
        _abandon()
        raise
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    with out_lock:
        out_bytes = b"".join(out_chunks)
    with err_lock:
        err_bytes = bytes(err_tail)
    stdout = out_bytes.decode("utf-8", errors="replace")
    stderr = err_bytes.decode("utf-8", errors="replace")
    if overflowed.is_set():
        raise WhistleError(
            f"Whistle wrote more than {stdout_limit} bytes to stdout and was stopped; "
            "that is not a valid standalone answer"
        )
    return _LaunchResult(returncode=returncode, stdout=stdout, stderr=stderr,
                         timed_out=timed_out)


def parse_standalone(raw: str) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Parse the native CLI's standalone stdout into ``(text, language, words)``.

    The standalone path prints exactly **one** JSON object. stdout must consist
    of that one object and nothing else; if anything else is present the answer
    is not trustworthy and the whole run is refused. This is deliberate:

    - an empty run is a separate, valid case (handled before this call), so
      reaching here the object is required;
    - stray non-JSON noise on the same stream, a truncated object, or several
      objects (a streaming mode's concatenated records, for instance) must not be
      quietly skipped to find one that happens to parse. The streaming path
      loses words, so accepting its output here could let a partial answer
      masquerade as a complete one;
    - the object's ``words`` key is required: "the engine returned no words" and
      "the engine's answer could not be read" must not be confused. A non-empty
      ``text`` with no corresponding words is refused later by the caller.

    The streaming ``pending`` field is ignored, but it cannot appear here: the
    standalone path does not emit it, and several records is a refusal, not an
    invitation to read the last one.
    """
    stripped = (raw or "").strip()
    if not stripped:
        raise WhistleError("Whistle produced no output")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise WhistleError(
            "Whistle output was not one JSON object (it may be truncated, noisy, "
            "or several concatenated records)"
        ) from exc
    if not isinstance(obj, dict):
        raise WhistleError("Whistle output was not a JSON object")
    if "words" not in obj:
        raise WhistleError("Whistle output had no 'words' key")
    text = str(obj.get("text") or "")
    language = obj.get("language")
    words = obj.get("words")
    if not isinstance(words, list):
        raise WhistleError("Whistle 'words' was not a list")
    return text, (str(language) if language else None), words


# --- progress / resume -----------------------------------------------------


@dataclass(slots=True)
class Progress:
    """Serializable progress for a resumable Whistle transcription.

    Carries everything needed to decide whether a stored progress may be
    reused: the schema and window policy, a content identity for the WAV (the
    SHA-256 of the whole file, streamed - see :func:`wav_identity`), the
    duration, and the model/binary/language configuration, plus the completed
    cores and the segments accumulated so far. :func:`validate_progress` refuses
    anything whose inputs or shape do not match, so a progress from different
    media or a different engine configuration is never silently reused.
    """

    wav_identity: str
    duration: float
    model_sha256: str
    binary_sha256: str
    language: str | None
    completed_core_index: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    #: How many cores the whole recording is cut into. Purely informational - it
    #: lets a caller display "block N/M" - and deliberately *not* part of the
    #: validation contract, so a resume computed without it is still accepted
    #: (``validate_progress`` reads only the fields it needs and ignores extras).
    total_cores: int = 0
    schema: str = PROGRESS_SCHEMA
    policy: str = WINDOW_POLICY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "policy": self.policy,
            "wav_identity": self.wav_identity,
            "duration": self.duration,
            "model_sha256": self.model_sha256,
            "binary_sha256": self.binary_sha256,
            "language": self.language,
            "completed_core_index": self.completed_core_index,
            "segments": self.segments,
            "total_cores": self.total_cores,
        }


def wav_identity(audio_path: str | Path) -> str:
    """A content identity for a WAV: the SHA-256 of the whole file.

    Streamed in blocks, so a long recording is hashed without being held in
    memory. A cheaper "size + head + tail" identity was rejected: two different
    recordings of the same length and the same leading and trailing kilobyte
    would collide, and a resume would then silently reuse work computed from
    other audio. Resume safety is worth the one extra streaming read.
    """
    import hashlib

    path = Path(audio_path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise WhistleError(f"cannot read audio identity for '{audio_path}': {exc}") from exc
    return digest.hexdigest()


def _progress_prefix_limit(windows: list[_Window], completed: int) -> float:
    """The latest absolute time a completed prefix may own words up to.

    A completed run of ``completed`` cores may own any word whose midpoint falls
    in one of those cores. The boundary of the last completed core is the limit.
    A segment may *start* a little before it - the context policy lets the first
    word of a core reach back into the preceding overlap - but ownership is by
    midpoint, so no word may have a midpoint past the completed prefix's edge.
    """
    if completed <= 0:
        return 0.0
    return windows[completed - 1].core_end + TIMESTAMP_EPSILON_SECONDS


_WORD_SHAPE_TOLERANCE = 1e-6


def _validate_progress_words(entry: Any, *, segment: dict[str, Any],
                             prefix_limit: float) -> list[dict[str, Any]]:
    """Validate one segment's nested words, or raise.

    A resume checkpoint is data from another process (or another day), so its
    nested words are validated exactly as freshly parsed engine output is: an
    object with text and finite, non-negative, non-reversed timestamps, in
    sequence, each owned within the completed prefix. Corrupt data is refused -
    never silently trimmed into something that merely looks valid.
    """
    words = entry.get("words") or []
    if not isinstance(words, list):
        raise WhistleError("resume progress segment has a corrupt word list")
    checked: list[dict[str, Any]] = []
    previous_start = -1.0
    for word in words:
        if not isinstance(word, dict):
            raise WhistleError("resume progress holds a corrupt word")
        text = word.get("text")
        if not isinstance(text, str):
            raise WhistleError("resume progress word has no text")
        try:
            w_start = float(word["start"])
            w_end = float(word["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WhistleError("resume progress word has no usable timestamps") from exc
        if not (math.isfinite(w_start) and math.isfinite(w_end)):
            raise WhistleError("resume progress holds a non-finite word timestamp")
        if w_start < -_WORD_SHAPE_TOLERANCE or w_end <= w_start:
            raise WhistleError("resume progress holds a non-positive word interval")
        if w_start < segment["start"] - WORD_BOUNDS_TOLERANCE_SECONDS:
            raise WhistleError("resume progress word starts before its segment")
        if w_end > segment["end"] + WORD_BOUNDS_TOLERANCE_SECONDS:
            raise WhistleError("resume progress word runs past its segment")
        if w_start + TIMESTAMP_EPSILON_SECONDS < previous_start:
            raise WhistleError("resume progress holds out-of-order words")
        # Ownership is by the word's midpoint: it must lie inside the completed
        # prefix, or these words were not produced by the work being resumed.
        midpoint = (w_start + w_end) / 2.0
        if prefix_limit and midpoint > prefix_limit:
            raise WhistleError(
                "resume progress holds words owned after the completed cores"
            )
        previous_start = w_start
        checked.append({"start": w_start, "end": w_end, "text": text})
    return checked


def validate_progress(raw: Any, *, wav_identity_value: str, duration: float,
                      model_sha256: str, binary_sha256: str,
                      language: str | None, windows: list[_Window]) -> Progress:
    """Validate a stored progress against the current run, or raise.

    Refuses: a different schema or window policy, different media (identity or
    duration), a different model, binary, or language, a corrupt or untrusted
    shape (a non-finite timestamp, a negative or reversed interval, a nested
    word that does not match its segment, words out of sequence), an index out
    of range, a non-empty segment list with a completed index of zero, segments
    beyond the completed work, and segments whose start order is not monotonic.
    Segment intervals are *allowed* to overlap at context seams - that is how a
    real windowed run emits them - so only reversed start order is refused, never
    a legitimate overlap. This is what keeps a resume from silently mixing two
    recordings or reusing data computed another way; nothing here is sanitized -
    a bad checkpoint is refused so a caller who meant to resume learns their
    inputs no longer match.
    """
    if not isinstance(raw, dict):
        raise WhistleError("resume progress is not an object")
    if raw.get("schema") != PROGRESS_SCHEMA:
        raise WhistleError("resume progress has an incompatible schema")
    if raw.get("policy") != WINDOW_POLICY:
        raise WhistleError("resume progress was produced under a different window policy")
    if raw.get("wav_identity") != wav_identity_value:
        raise WhistleError("resume progress refers to different audio")
    try:
        stored_duration = float(raw.get("duration"))
    except (TypeError, ValueError) as exc:
        raise WhistleError("resume progress has no usable duration") from exc
    if not math.isfinite(stored_duration) or abs(stored_duration - duration) > TIMESTAMP_EPSILON_SECONDS:
        raise WhistleError("resume progress duration does not match the audio")
    if raw.get("model_sha256") != model_sha256:
        raise WhistleError("resume progress was produced by a different model")
    if raw.get("binary_sha256") != binary_sha256:
        raise WhistleError("resume progress was produced by a different runtime")
    if raw.get("language") != language:
        raise WhistleError("resume progress was produced with a different language")

    completed = raw.get("completed_core_index")
    if not isinstance(completed, int) or isinstance(completed, bool):
        raise WhistleError("resume progress has no completed core index")
    if completed < 0 or completed > len(windows):
        raise WhistleError("resume progress completed core index is out of range")

    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, list):
        raise WhistleError("resume progress has no segment list")
    if completed == 0 and segments_raw:
        raise WhistleError(
            "resume progress claims no completed cores but carries segments"
        )

    prefix_limit = _progress_prefix_limit(windows, completed)
    segments: list[dict[str, Any]] = []
    previous_start = -1.0
    for entry in segments_raw:
        if not isinstance(entry, dict):
            raise WhistleError("resume progress holds a corrupt segment")
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WhistleError("resume progress holds a corrupt segment") from exc
        if not (math.isfinite(start) and math.isfinite(end)):
            raise WhistleError("resume progress holds a non-finite timestamp")
        if start < 0 or end <= start - TIMESTAMP_EPSILON_SECONDS:
            raise WhistleError("resume progress holds a negative or reversed interval")
        if end > duration + TIMESTAMP_EPSILON_SECONDS:
            raise WhistleError("resume progress segment runs past the audio")
        # Ordering is by segment *start*, which is monotonic in engine output.
        # Interval non-overlap is deliberately not required: adjacent segments
        # produced from neighbouring context windows legitimately overlap at the
        # seam (a core's last segment can end a few hundred ms after the next
        # core's first segment begins, because each core's context reaches into
        # the other). Reversed start order is still refused - that is the signal
        # of shuffling or corruption, and it is the same rule the engine uses
        # when it emits the list.
        if start + TIMESTAMP_EPSILON_SECONDS < previous_start:
            raise WhistleError("resume progress holds out-of-order segments")
        # A segment may not begin inside audio the completed cores never covered.
        # Its start may reach back by the context policy, but no further.
        if prefix_limit and start > prefix_limit:
            raise WhistleError("resume progress holds segments beyond the completed cores")
        text = entry.get("text")
        if not isinstance(text, str):
            raise WhistleError("resume progress segment has no text")
        words = _validate_progress_words(entry, segment={"start": start, "end": end},
                                         prefix_limit=prefix_limit)
        previous_start = start
        segments.append({"start": start, "end": end, "text": text, "words": words})
    # The block count comes from the window plan this run rebuilt from the real
    # audio, not from the stored body: an older body written before ``total_cores``
    # existed still validates, and this derives the honest N for "block N/M"
    # rather than trusting a value the checkpoint happened to carry. The number
    # of windows is a property of the audio and the window policy - both already
    # validated above - so it is authoritative here.
    return Progress(
        wav_identity=wav_identity_value,
        duration=duration,
        model_sha256=model_sha256,
        binary_sha256=binary_sha256,
        language=language,
        completed_core_index=completed,
        segments=segments,
        total_cores=len(windows),
    )


# --- the engine ------------------------------------------------------------


class WhistleEngine:
    """Whistle, driven over its native CLI.

    Selected explicitly by name; it never replaces the default engine on its
    own. CPU only: an explicit non-CPU device is refused rather than quietly
    ignored.
    """

    name = "whistle"

    def __init__(
        self,
        model: str = WHISTLE_MODEL_NAME,
        device: str | None = None,
        *,
        timeout: float = DEFAULT_WINDOW_TIMEOUT_SECONDS,
    ):
        if model != WHISTLE_MODEL_NAME:
            raise ValueError(
                f"unknown Whistle model '{model}'; the only model is '{WHISTLE_MODEL_NAME}'"
            )
        if device is not None and device.lower() not in ("cpu", ""):
            raise ValueError(
                f"Whistle runs on CPU only; device '{device}' is not supported. "
                "Omit the device, or select the Whisper engine for GPU."
            )
        self.model_name = model
        self.device = "cpu"
        self.timeout = timeout
        self._lock = threading.RLock()

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def validate_language(language: str | None) -> str | None:
        """Check a requested language before any asset is fetched.

        Returns the language unchanged, or raises for one Whistle does not
        advertise. ``None`` means "let the engine detect it".
        """
        if language is None:
            return None
        code = str(language).lower()
        if code not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Whistle does not support language '{language}'; "
                f"choose from {', '.join(SUPPORTED_LANGUAGES)}"
            )
        return code

    # -- the transcribe entry point ----------------------------------------

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        check_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        resume_progress: dict[str, Any] | None = None,
        offline: bool | None = None,
    ) -> Transcript:
        """Transcribe 16 kHz mono PCM WAV audio.

        ``check_cancel`` raises to abort; it is polled while each native call
        runs and between windows. ``on_progress`` receives a serializable
        progress snapshot after each completed core, for durable resume.
        ``resume_progress`` reuses a validated prior snapshot and skips the
        cores it already completed. ``offline`` forbids the asset download.

        Only committed standalone output is used; the streaming ``pending``
        field is never read.
        """
        language = self.validate_language(language)
        source = Path(audio_path)
        info = read_wav_info(source)
        if info.sample_width != 2 or info.channels != 1 or info.sample_rate != 16000:
            raise WhistleError(
                "Whistle requires 16 kHz mono 16-bit PCM WAV; got "
                f"{info.sample_rate} Hz, {info.channels} channel(s), "
                f"{info.sample_width * 8}-bit"
            )
        windows = plan_windows(info.duration)

        try:
            binary_path, model_path = ensure_assets(offline=offline)
        except WhistleAssetError as exc:
            raise WhistleError(str(exc)) from exc

        binary_sha = whistle_assets.PLATFORM_BINARIES[
            whistle_assets.current_platform()
        ].asset.sha256
        model_sha = whistle_assets.WHISTLE_MODEL.sha256
        identity = wav_identity(source)

        # Resume: adopt a validated snapshot, or start clean. A snapshot that
        # fails validation is a refusal, never a silent fresh start, so a caller
        # who meant to resume learns their inputs no longer match.
        completed_cores = 0
        segments: list[Segment] = []
        if resume_progress is not None:
            adopted = validate_progress(
                resume_progress,
                wav_identity_value=identity,
                duration=info.duration,
                model_sha256=model_sha,
                binary_sha256=binary_sha,
                language=language,
                windows=windows,
            )
            completed_cores = adopted.completed_core_index
            segments = [Segment.from_dict(entry) for entry in adopted.segments]

        repairs = Repairs()
        scratch = Path(tempfile.mkdtemp(prefix="textflowkit-whistle-"))
        try:
            return self._run_windows(
                source=source,
                windows=windows,
                binary_path=binary_path,
                model_path=model_path,
                language=language,
                check_cancel=check_cancel,
                on_progress=on_progress,
                repairs=repairs,
                scratch=scratch,
                completed_cores=completed_cores,
                segments=segments,
                identity=identity,
                duration=info.duration,
                model_sha=model_sha,
                binary_sha=binary_sha,
            )
        finally:
            _cleanup(scratch)

    def _run_windows(self, *, source: Path, windows: list[_Window], binary_path: Path,
                     model_path: Path, language: str | None,
                     check_cancel: Callable[[], None] | None,
                     on_progress: Callable[[dict[str, Any]], None] | None,
                     repairs: Repairs, scratch: Path, completed_cores: int,
                     segments: list[Segment], identity: str, duration: float,
                     model_sha: str, binary_sha: str) -> Transcript:
        base_cmd = [str(binary_path), "--model", str(model_path), "--audio-word-timestamps"]
        if language:
            base_cmd.extend(["--audio-language", language])

        detected_language = language
        for window in windows:
            if check_cancel is not None:
                check_cancel()
            if window.index < completed_cores:
                continue  # already done in a prior run

            clip = scratch / f"window-{window.index:04d}.wav"
            _write_window_wav(source, window, clip)
            try:
                result = _run_native(
                    [*base_cmd, "--audio", str(clip)],
                    timeout=self.timeout,
                    check_cancel=check_cancel,
                )
            finally:
                clip.unlink(missing_ok=True)

            # One attempt per window. There is no automatic retry: a timeout or
            # a nonzero exit is reported at once (after at most one 120 s window),
            # not retried into a 240 s wait. A caller who wants to try again does
            # so explicitly.
            if result.timed_out:
                raise WhistleError(
                    f"Whistle timed out after {self.timeout:g}s on window "
                    f"{window.index} (audio {window.clip_start:.3f}-"
                    f"{window.clip_end:.3f}s)"
                )
            if result.returncode != 0:
                detail = result.stderr.strip()[-500:] or "unknown error"
                raise WhistleError(
                    f"Whistle failed on window {window.index} "
                    f"(exit {result.returncode}): {detail}"
                )

            text, window_language, raw_words = parse_standalone(result.stdout)
            if window_language and detected_language is None:
                detected_language = window_language

            parsed = _parse_words(
                raw_words,
                offset=window.clip_start,
                clip_start=window.clip_start,
                clip_end=window.clip_end,
                repairs=repairs,
            )
            # A window that returned text but no usable words is an error: the
            # engine claimed speech and gave us nothing to time it with.
            if text.strip() and not parsed:
                raise WhistleError(
                    f"Whistle returned text but no usable words for window "
                    f"{window.index} (audio {window.clip_start:.3f}-"
                    f"{window.clip_end:.3f}s)"
                )

            # Own only the words whose midpoint falls in this core, then append
            # this window's segments. Ownership is applied per window so the
            # running segment list mirrors a from-scratch run.
            owned = _own_in_core(window, parsed, is_last=window.index == len(windows) - 1)
            segments.extend(segment_words(owned))

            if on_progress is not None:
                on_progress(
                    Progress(
                        wav_identity=identity,
                        duration=duration,
                        model_sha256=model_sha,
                        binary_sha256=binary_sha,
                        # The *requested* language, not the detected one: a
                        # resume must match the configuration that was asked
                        # for, and an auto-detected value would never equal the
                        # None a fresh run requests.
                        language=language,
                        completed_core_index=window.index + 1,
                        segments=[s.to_dict() for s in segments],
                        total_cores=len(windows),
                    ).to_dict()
                )

        return Transcript(
            source=str(source),
            language=detected_language,
            segments=segments,
            duration=duration,  # the real media duration, including trailing silence
            engine=self.name,
            metadata={
                "model": WHISTLE_MODEL_NAME,
                "model_sha256": model_sha,
                "binary_sha256": binary_sha,
                "device": "cpu",
                "window_policy": WINDOW_POLICY,
                "cores": len(windows),
                "timestamp_repairs": repairs.to_dict(),
                "resumed_from_core": completed_cores if completed_cores else None,
            },
        )


def _own_in_core(window: _Window, words: list[_ParsedWord], *,
                 is_last: bool) -> list[_ParsedWord]:
    """Words this window owns.

    A word is owned by the window whose core contains its midpoint. The last
    window additionally owns any word whose midpoint lies at or past its core
    start (the trailing edge, including exactly the final boundary), so a word
    on the very end of the recording is not lost.
    """
    kept: list[_ParsedWord] = []
    for word in words:
        midpoint = (word.start + word.end) / 2.0
        in_core = window.core_start - TIMESTAMP_EPSILON_SECONDS <= midpoint < (
            window.core_end - TIMESTAMP_EPSILON_SECONDS
        )
        if in_core or (is_last and midpoint >= window.core_start - TIMESTAMP_EPSILON_SECONDS):
            kept.append(word)
    return kept


def _cleanup(path: Path) -> None:
    import shutil

    for _ in range(10):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)


def get_whistle_engine(model: str = WHISTLE_MODEL_NAME, device: str | None = None,
                       **kwargs: Any) -> WhistleEngine:
    """Build a :class:`WhistleEngine` with the given options validated."""
    return WhistleEngine(model=model, device=device, **kwargs)
