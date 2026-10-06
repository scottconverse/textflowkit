"""The Whistle engine: windows, boundaries, repairs, cancellation, resume.

Every test is deterministic and offline. The native CLI is faked with a small
Python script (``fake_needle``) that prints the real standalone JSON contract,
so no model, no binary, and no network are used. Assets are faked by pointing
``ensure_assets`` at two tiny files in the test's tmp dir.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import textwrap
import threading
import time
import wave
from pathlib import Path

import pytest

from textflowkit.core import whistle as W
from textflowkit.core.cancel import CancelledError
from textflowkit.core.whistle_assets import PinnedAsset, PlatformBinary

# --- fixtures --------------------------------------------------------------


def _write_wav(path: Path, seconds: float, *, sample_rate: int = 16000,
               silence: bool = False) -> Path:
    frames = round(seconds * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        if silence:
            wav.writeframes(b"\x00\x00" * frames)
        else:
            # A quiet non-zero ramp; contents are irrelevant to the fake CLI.
            wav.writeframes(b"\x01\x00" * frames)
    return path


FAKE_NEEDLE = textwrap.dedent(
    '''
    """A stand-in for the native Whistle CLI, for tests only.

    Reads --audio <wav> and prints one JSON object in the real contract:
    {"text", "language", "words":[{"word","start","end","probability"}]}.
    Words are produced by the WORD_SCRIPT env var, a JSON list of
    [word, start, end, probability]; a null probability omits it. Special env
    hooks let a test force a nonzero exit, a hang, or malformed output.
    """
    import json, os, sys, time, wave

    args = sys.argv[1:]
    audio = None
    if "--audio" in args:
        audio = args[args.index("--audio") + 1]

    if os.environ.get("FAKE_NEEDLE_SLEEP"):
        time.sleep(float(os.environ["FAKE_NEEDLE_SLEEP"]))
    if os.environ.get("FAKE_NEEDLE_EXIT"):
        sys.stderr.write("needle.exe: audio limit is 30 s\\n")
        sys.exit(int(os.environ["FAKE_NEEDLE_EXIT"]))
    if os.environ.get("FAKE_NEEDLE_GARBAGE"):
        sys.stdout.write("not json at all\\n")
        sys.exit(0)
    if os.environ.get("FAKE_NEEDLE_NO_WORDS_KEY"):
        sys.stdout.write(json.dumps({"text": "hi"}) + "\\n")
        sys.exit(0)

    words = json.loads(os.environ.get("WORD_SCRIPT", "[]"))
    out = []
    for entry in words:
        w = {"word": entry[0], "start": entry[1], "end": entry[2]}
        if entry[3] is not None:
            w["probability"] = entry[3]
        out.append(w)
    text = " ".join(e[0] for e in words)
    sys.stdout.write(json.dumps({
        "text": text, "language": "en", "words": out,
        "ttft_ms": 1.0, "decode_tps": 2.0,
    }) + "\\n")
    '''
).strip()


@pytest.fixture
def fake_cli(monkeypatch, tmp_path) -> Path:
    """A fake native CLI on disk, wired in as the resolved binary.

    The "binary" is a launcher for the fake script. On Windows it is a ``.cmd``
    shim, which ``CreateProcess`` executes; elsewhere it is a ``.sh`` with the
    exec bit set. Either way the engine launches ``[binary, --model, ...]`` and
    never knows the difference.
    """
    script = tmp_path / "fake_needle.py"
    script.write_text(FAKE_NEEDLE, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "needle.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "needle"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)

    model = tmp_path / "whistle.cact"
    model.write_bytes(b"dummy-model")

    def _fake_assets(*, offline=None, timeout=None, max_bytes=None):
        return exe, model

    monkeypatch.setattr(W, "ensure_assets", _fake_assets)
    # Deterministic provenance hashes (no real pinned assets in the test).
    monkeypatch.setattr(W.whistle_assets, "PLATFORM_BINARIES", _FakeManifest())
    monkeypatch.setattr(W.whistle_assets, "current_platform", lambda: "windows-x86_64")
    return exe


class _FakeManifest(dict):
    def __init__(self):
        super().__init__({
            "windows-x86_64": PlatformBinary(
                folder="windows-x86_64",
                asset=PinnedAsset(
                    filename="needle.exe", url="https://x/y",
                    sha256="aa" * 32, size=1,
                ),
            )
        })


def _word_script(monkeypatch, words):
    monkeypatch.setenv("WORD_SCRIPT", json.dumps(words))


def _engine(**kwargs) -> W.WhistleEngine:
    return W.WhistleEngine(**kwargs)


# --- windows stay <= 30 s --------------------------------------------------


def test_every_window_clip_is_at_most_30_seconds():
    for duration in (1.0, 26.0, 26.1, 30.0, 52.0, 66.0, 600.0, 3600.0):
        windows = W.plan_windows(duration)
        for window in windows:
            assert window.clip_end - window.clip_start <= 30.0 + 1e-9, duration


def test_cores_tile_the_recording_without_gap_or_overlap():
    windows = W.plan_windows(66.0)
    assert windows[0].core_start == 0.0
    assert abs(windows[-1].core_end - 66.0) < 1e-9
    for a, b in itertools.pairwise(windows):
        assert abs(a.core_end - b.core_start) < 1e-9


def test_core_is_at_most_26_seconds():
    for window in W.plan_windows(600.0):
        assert window.core_end - window.core_start <= W.CORE_SECONDS + 1e-9


# --- boundaries and repetition preserved ----------------------------------


def test_a_word_spanning_a_core_boundary_is_owned_by_its_midpoint_not_its_start():
    windows = W.plan_windows(66.0)  # cores 0-26, 26-52, 52-66
    # A word whose *start* (25.0) is in core 0 but whose *midpoint* (26.1) is in
    # core 1. The two rules disagree for this word on purpose: it pins the
    # midpoint rule rather than a start-based one.
    words = [
        W._ParsedWord("before", 24.0, 24.5, 0.9),
        W._ParsedWord("straddle", 25.0, 27.2, 0.9),
        W._ParsedWord("after", 27.4, 27.8, 0.9),
    ]
    owned = W.assign_ownership(windows, words)
    assert [w.text for w in owned].count("straddle") == 1
    core0, core1 = windows[0], windows[1]
    # "straddle" is owned by core 1 (its midpoint), not core 0 (its start).
    assert [w.text for w in W._own_in_core(core0, words, is_last=False)] == ["before"]
    assert "straddle" in [w.text for w in W._own_in_core(core1, words, is_last=False)]


def test_repeated_phrases_are_not_deduped():
    windows = W.plan_windows(20.0)
    words = [
        W._ParsedWord("ask", 1.0, 1.2, 0.9),
        W._ParsedWord("ask", 3.0, 3.2, 0.9),
        W._ParsedWord("ask", 5.0, 5.2, 0.9),
    ]
    owned = W.assign_ownership(windows, words)
    assert [w.text for w in owned] == ["ask", "ask", "ask"]


def test_word_on_the_final_edge_is_kept():
    windows = W.plan_windows(66.0)
    # A word whose midpoint is past the last core start (52-66) is owned there.
    words = [W._ParsedWord("last", 65.5, 65.9, 0.9)]
    owned = W.assign_ownership(windows, words)
    assert [w.text for w in owned] == ["last"]


# --- segmentation ----------------------------------------------------------


def test_sentence_punctuation_and_pauses_split_segments():
    words = [
        W._ParsedWord("Hello.", 0.0, 0.5, 0.9),
        W._ParsedWord("World", 0.6, 1.0, 0.9),
        # a 2 s pause starts a new segment
        W._ParsedWord("Again", 3.0, 3.4, 0.9),
    ]
    segments = W.segment_words(words)
    # "Hello." closes on its punctuation; the 2 s pause separates the other two.
    assert [s.text for s in segments] == ["Hello.", "World", "Again"]


def test_adjacent_words_within_a_sentence_stay_together():
    words = [
        W._ParsedWord("Hello", 0.0, 0.5, 0.9),
        W._ParsedWord("there", 0.55, 1.0, 0.9),
        W._ParsedWord("world.", 1.05, 1.5, 0.9),
    ]
    segments = W.segment_words(words)
    assert [s.text for s in segments] == ["Hello there world."]


def test_segments_never_exceed_the_maximum():
    words = [W._ParsedWord(f"w{i}", i * 1.0, i * 1.0 + 0.9, 0.9) for i in range(40)]
    segments = W.segment_words(words)
    for segment in segments:
        assert segment.end - segment.start <= W.MAX_SEGMENT_SECONDS + 1e-9


# --- timestamp validation and repair --------------------------------------


def _parse(monkeypatch, raw_words, *, clip_start=0.0, clip_end=30.0):
    repairs = W.Repairs()
    parsed = W._parse_words(raw_words, offset=0.0, clip_start=clip_start,
                            clip_end=clip_end, repairs=repairs)
    return parsed, repairs


def test_zero_duration_word_is_repaired_with_a_small_positive_interval():
    parsed, repairs = _parse(None, [
        {"word": "hi", "start": 1.0, "end": 1.0, "probability": 0.5},
    ])
    assert len(parsed) == 1
    assert parsed[0].end > parsed[0].start
    assert repairs.zero_duration_repaired == 1


def _parse_raises(monkeypatch, raw_words, *, clip_start=0.0, clip_end=30.0):
    with pytest.raises(W.WhistleError):
        _parse(monkeypatch, raw_words, clip_start=clip_start, clip_end=clip_end)


def test_reversed_interval_rejects_the_whole_window():
    # A reversed interval is a defect in the engine's answer, not a word to drop:
    # one bad word must not silently cost the good word beside it.
    _parse_raises(
        None,
        [
            {"word": "bad", "start": 5.0, "end": 4.0, "probability": 0.5},
            {"word": "good", "start": 6.0, "end": 6.5, "probability": 0.5},
        ],
    )


def test_nan_and_infinite_timestamps_reject_the_whole_window():
    _parse_raises(
        None,
        [
            {"word": "nan", "start": float("nan"), "end": 1.0, "probability": 0.5},
            {"word": "good", "start": 6.0, "end": 6.5, "probability": 0.5},
        ],
    )
    _parse_raises(
        None,
        [
            {"word": "inf", "start": 1.0, "end": float("inf"), "probability": 0.5},
            {"word": "good", "start": 6.0, "end": 6.5, "probability": 0.5},
        ],
    )


def test_probability_outside_unit_range_rejects_the_whole_window():
    _parse_raises(
        None,
        [
            {"word": "low", "start": 1.0, "end": 1.5, "probability": -0.1},
            {"word": "ok", "start": 3.0, "end": 3.5, "probability": 0.7},
        ],
    )
    _parse_raises(
        None,
        [
            {"word": "high", "start": 2.0, "end": 2.5, "probability": 1.4},
            {"word": "ok", "start": 3.0, "end": 3.5, "probability": 0.7},
        ],
    )


def test_a_word_is_kept_when_every_value_is_valid():
    # The counterpart to the refusals above: all-valid input keeps all words.
    parsed, repairs = _parse(None, [
        {"word": "one", "start": 1.0, "end": 1.5, "probability": 0.5},
        {"word": "two", "start": 2.0, "end": 2.5, "probability": None},
        {"word": "three", "start": 3.0, "end": 3.5, "probability": 0.9},
    ])
    assert [w.text for w in parsed] == ["one", "two", "three"]
    assert repairs.total == 0


def test_grossly_out_of_range_word_is_refused_without_losing_the_window():
    # The window is [0, 30]; a word at 300 s does not belong to it. Being outside
    # this window is a boundary case, not a defect: it is dropped (counted), and
    # the window's own words survive.
    parsed, repairs = _parse(None, [
        {"word": "far", "start": 300.0, "end": 301.0, "probability": 0.5},
        {"word": "here", "start": 1.0, "end": 1.5, "probability": 0.5},
    ])
    assert [w.text for w in parsed] == ["here"]
    assert repairs.rejected_out_of_range == 1


def test_rounding_sized_overshoot_is_clamped_not_rejected():
    parsed, repairs = _parse(None, [
        {"word": "edge", "start": 29.9995, "end": 30.0002, "probability": 0.5},
    ], clip_end=30.0)
    assert len(parsed) == 1
    assert parsed[0].end <= 30.0 + 1e-9
    assert repairs.clamped_to_window == 1


def test_only_a_rounding_sized_overshoot_is_tolerated():
    # The clamp tolerance is explicit: TIMESTAMP_EPSILON_SECONDS = 1e-3, and a
    # word past the window by more than the out-of-range tolerance is refused.
    # Within the out-of-range tolerance but past the edge: clamped.
    parsed, repairs = _parse(None, [
        {"word": "slight", "start": 30.0005, "end": 30.4, "probability": 0.5},
    ], clip_end=30.0)
    assert len(parsed) == 1 and repairs.clamped_to_window == 1


@pytest.mark.parametrize(
    "entry",
    [
        "not-a-dict",
        {"word": "", "start": 1.0, "end": 2.0, "probability": 0.5},
        {"word": "noend", "start": 1.0, "probability": 0.5},
        {"word": "ok", "start": "soon", "end": 2.0, "probability": 0.5},
    ],
)
def test_a_malformed_word_entry_rejects_the_whole_window(entry):
    # Each of these is a defect in the engine's answer. Mixing it with a good
    # word must not yield a partial transcript that looks successful.
    _parse_raises(None, [entry, {"word": "good", "start": 3.0, "end": 3.5,
                                 "probability": 0.5}])


# --- provenance ------------------------------------------------------------


def test_transcript_preserves_media_duration_and_provenance(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "meeting.wav", 40.0)
    _word_script(monkeypatch, [["hello", 0.5, 1.0, 0.9]])
    transcript = _engine().transcribe(audio)
    assert transcript.duration == pytest.approx(40.0, abs=0.01)
    assert transcript.engine == "whistle"
    assert transcript.metadata["model"] == "whistle"
    assert transcript.metadata["device"] == "cpu"
    assert "window_policy" in transcript.metadata
    assert "timestamp_repairs" in transcript.metadata


def test_empty_transcript_is_valid_for_silence(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "silence.wav", 5.0, silence=True)
    _word_script(monkeypatch, [])
    transcript = _engine().transcribe(audio)
    assert transcript.segments == []
    assert transcript.text == ""


def test_nonempty_text_with_no_words_is_an_error(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "audio.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_EXIT", "0")
    _word_script(monkeypatch, [])
    # Force text-without-words by making the CLI print text and no words.
    script = fake_cli.parent / "fake_needle.py"
    script.write_text(
        "import json,sys\nsys.stdout.write(json.dumps({'text':'hi there','language':'en','words':[]})+'\\n')\n",
        encoding="utf-8",
    )
    with pytest.raises(W.WhistleError, match="no usable words"):
        _engine().transcribe(audio)


def test_missing_asset_platform_refusal_is_not_a_whisper_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "ensure_assets", lambda **k: (_ for _ in ()).throw(
        W.WhistleAssetError("Whistle has no published native runtime for Intel Mac; select Whisper")
    ))
    audio = _write_wav(tmp_path / "a.wav", 1.0)
    with pytest.raises(W.WhistleError, match="Whisper"):
        _engine().transcribe(audio)


# --- language and device ---------------------------------------------------


def test_unsupported_language_is_refused_before_acquisition(monkeypatch, tmp_path):
    called = {"assets": False}

    def _assets(**kwargs):
        called["assets"] = True
        return tmp_path / "bin", tmp_path / "model"

    monkeypatch.setattr(W, "ensure_assets", _assets)
    audio = _write_wav(tmp_path / "a.wav", 1.0)
    with pytest.raises(ValueError, match="does not support language"):
        _engine().transcribe(audio, language="xx")
    assert called["assets"] is False  # refused before any asset work


def test_cuda_device_is_refused():
    with pytest.raises(ValueError, match="CPU only"):
        _engine(device="cuda")


def test_unknown_model_is_refused():
    with pytest.raises(ValueError, match="only model is 'whistle'"):
        _engine(model="large")


def test_default_window_timeout_is_the_documented_120_seconds():
    assert W.DEFAULT_WINDOW_TIMEOUT_SECONDS == 120.0
    assert _engine().timeout == 120.0


def test_the_command_never_uses_audio_stream(monkeypatch, tmp_path, fake_cli):
    """Streaming is prohibited: only the standalone transcription path is used."""
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    _word_script(monkeypatch, [["hi", 0.1, 0.5, 0.9]])

    seen = []
    real_popen = W.subprocess.Popen

    def _spy(cmd, *args, **kwargs):
        seen.append(list(cmd))
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(W.subprocess, "Popen", _spy)
    _engine().transcribe(audio)
    assert seen, "the native CLI was never launched"
    for cmd in seen:
        assert "--audio-stream" not in cmd
        assert "--audio" in cmd
        assert "--audio-word-timestamps" in cmd


def test_stderr_is_bounded_and_does_not_leak_into_the_transcript(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    _word_script(monkeypatch, [["hi", 0.1, 0.5, 0.9]])
    # A chatty stderr must not break the run nor appear in the result.
    script = fake_cli.parent / "fake_needle.py"
    script.write_text(
        "import json,sys\n"
        "sys.stderr.write('noise\\n'*10000)\n"
        "sys.stdout.write(json.dumps({'text':'hi','language':'en','words':"
        "[{'word':'hi','start':0.1,'end':0.5,'probability':0.9}]})+'\\n')\n",
        encoding="utf-8",
    )
    transcript = _engine().transcribe(audio)
    assert transcript.text == "hi"
    assert "noise" not in transcript.text


def test_progress_round_trips_through_json():
    progress = W.Progress(
        wav_identity="ab" * 32,
        duration=66.0,
        model_sha256="cd" * 32,
        binary_sha256="ef" * 32,
        language="de",
        completed_core_index=1,
        segments=[{"start": 0.0, "end": 1.0, "text": "hi", "words": []}],
    )
    restored = json.loads(json.dumps(progress.to_dict()))
    assert restored["schema"] == W.PROGRESS_SCHEMA
    assert restored["policy"] == W.WINDOW_POLICY
    assert restored["completed_core_index"] == 1
    assert restored["language"] == "de"


# --- process failure, timeout, cancellation --------------------------------


def test_nonzero_exit_is_an_error(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_EXIT", "1")
    _word_script(monkeypatch, [])
    with pytest.raises(W.WhistleError, match="Whistle failed"):
        _engine().transcribe(audio)


def test_timeout_kills_and_errors(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_SLEEP", "30")
    _word_script(monkeypatch, [])
    with pytest.raises(W.WhistleError, match="timed out"):
        _engine(timeout=1.0).transcribe(audio)


def test_cancellation_during_run_rethrows_cancelled(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_SLEEP", "10")
    _word_script(monkeypatch, [])

    calls = {"n": 0}

    def _check():
        calls["n"] += 1
        if calls["n"] > 3:
            raise CancelledError("stop")

    started = time.monotonic()
    with pytest.raises(CancelledError):
        _engine().transcribe(audio, check_cancel=_check)
    assert time.monotonic() - started < 8.0  # the child was killed, not waited out


def test_garbage_output_is_an_error(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_GARBAGE", "1")
    with pytest.raises(W.WhistleError, match="JSON object"):
        _engine().transcribe(audio)


def test_window_scratch_is_removed_after_a_failure(monkeypatch, tmp_path, fake_cli):
    """Cleanup is asserted against *this run's* scratch root, never global temp.

    Inspecting the process-wide temp directory would race any other process (or
    test) that creates or removes a ``textflowkit-whistle-*`` entry between the
    "before" and "after" snapshots - a failure about someone else's temp files,
    not this engine's cleanup. Instead the engine's scratch root is pinned to a
    private directory for this test, so the only entries the glob can see are the
    ones this run created and must remove.
    """
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    # `tempfile.mkdtemp` reads the TMPDIR/TEMP/TMP environment; pinning all
    # three (and clearing the cached tempdir) makes the engine create its scratch
    # here and nowhere else.
    monkeypatch.setenv("TMPDIR", str(scratch_root))
    monkeypatch.setenv("TEMP", str(scratch_root))
    monkeypatch.setenv("TMP", str(scratch_root))
    monkeypatch.setattr("tempfile.tempdir", None)
    audio = _write_wav(tmp_path / "a.wav", 40.0)
    monkeypatch.setenv("FAKE_NEEDLE_EXIT", "1")
    with pytest.raises(W.WhistleError):
        _engine().transcribe(audio)
    leaked = list(scratch_root.glob("textflowkit-whistle-*"))
    assert leaked == [], f"scratch not cleaned up: {leaked}"


def test_a_window_failure_is_not_retried(monkeypatch, tmp_path, fake_cli):
    """One attempt per window: a nonzero exit errors immediately, not twice."""
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_EXIT", "1")
    launches = {"n": 0}
    real_popen = W.subprocess.Popen

    def _spy(cmd, *args, **kwargs):
        if any("--audio" in str(part) for part in cmd):
            launches["n"] += 1
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(W.subprocess, "Popen", _spy)
    with pytest.raises(W.WhistleError, match="Whistle failed"):
        _engine().transcribe(audio)
    assert launches["n"] == 1  # exactly one launch, no automatic retry


def test_a_timeout_is_not_retried_into_a_double_wait(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    monkeypatch.setenv("FAKE_NEEDLE_SLEEP", "30")
    launches = {"n": 0}
    real_popen = W.subprocess.Popen

    def _spy(cmd, *args, **kwargs):
        # Count only native transcription launches; the kill path also goes
        # through subprocess and must not be mistaken for a retry.
        if any("--audio" in str(part) for part in cmd):
            launches["n"] += 1
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(W.subprocess, "Popen", _spy)
    with pytest.raises(W.WhistleError, match="timed out"):
        _engine(timeout=1.0).transcribe(audio)
    assert launches["n"] == 1  # one 1 s window, not two


# --- strict standalone parsing --------------------------------------------


def test_parse_standalone_accepts_one_object():
    raw = json.dumps({"text": "hi", "language": "en", "words": []})
    text, language, words = W.parse_standalone(raw)
    assert text == "hi" and language == "en" and words == []


def test_parse_standalone_rejects_several_concatenated_records():
    """A stream of records must not be read as "the last one that parses".

    The streaming path loses words into ``pending``; accepting a multi-record
    stream here could let a partial answer masquerade as complete.
    """
    a = json.dumps({"text": "first", "language": "en", "words": []})
    b = json.dumps({"text": "second", "language": "en", "words": []})
    with pytest.raises(W.WhistleError, match="one JSON object"):
        W.parse_standalone(a + "\n" + b)


def test_parse_standalone_rejects_noise_around_the_object():
    obj = json.dumps({"text": "hi", "language": "en", "words": []})
    with pytest.raises(W.WhistleError, match="one JSON object"):
        W.parse_standalone("warning: something\n" + obj)


def test_parse_standalone_rejects_a_truncated_object():
    with pytest.raises(W.WhistleError, match="one JSON object"):
        W.parse_standalone('{"text": "hi", "language": "en", "words": [')


def test_parse_standalone_requires_the_words_key():
    with pytest.raises(W.WhistleError, match="no 'words' key"):
        W.parse_standalone(json.dumps({"text": "hi"}))


def test_parse_standalone_rejects_empty_output():
    with pytest.raises(W.WhistleError, match="no output"):
        W.parse_standalone("   \n  ")


def test_parse_standalone_rejects_a_non_list_words_value():
    with pytest.raises(W.WhistleError, match="words"):
        W.parse_standalone(json.dumps({"text": "hi", "words": {"a": 1}}))


def test_parse_standalone_ignores_a_pending_field_but_keeps_the_object():
    # The standalone object may carry fields we do not read; that is fine. What
    # is refused is a *stream* of objects, not the presence of extra keys.
    raw = json.dumps({"text": "hi", "language": "en", "words": [],
                      "pending": "hi", "ttft_ms": 1.0})
    text, _language, words = W.parse_standalone(raw)
    assert text == "hi" and words == []


# --- stdout is bounded -----------------------------------------------------
#
# A chatty child must not be buffered without limit, and must not leave a
# reader thread or an open pipe behind.


def _chatty_cli(fake_cli: Path, body: str) -> None:
    (fake_cli.parent / "fake_needle.py").write_text(body, encoding="utf-8")


def test_a_child_that_floods_stdout_is_stopped_and_refused(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    # Write far past the cap, then sleep so the child would hang if not killed.
    _chatty_cli(
        fake_cli,
        "import sys, time\n"
        "chunk = b'x' * 65536\n"
        "for _ in range((1024 * 1024 // 65536) * 4):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "    sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
    )
    started = time.monotonic()
    with pytest.raises(W.WhistleError, match="stdout"):
        _engine(timeout=30.0).transcribe(audio)
    # The overflow is caught by the cap, not by waiting out the 30 s timeout.
    assert time.monotonic() - started < 20.0


def test_a_chatty_but_valid_child_still_succeeds(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    # Noise on stderr only; the single stdout object is well under the cap.
    _chatty_cli(
        fake_cli,
        "import json, sys\n"
        "sys.stderr.write('noise\\n' * 20000)\n"
        "sys.stdout.write(json.dumps({'text':'hi','language':'en','words':"
        "[{'word':'hi','start':0.1,'end':0.5,'probability':0.9}]})+'\\n')\n",
    )
    transcript = _engine().transcribe(audio)
    assert transcript.text == "hi"


def test_killed_child_leaves_no_lingering_reader_after_cancel(monkeypatch, tmp_path, fake_cli):
    """A cancelled hang returns promptly and its reader threads do not linger."""
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    _chatty_cli(
        fake_cli,
        "import sys, time\n"
        "sys.stdout.write('{}')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    before = {t.ident for t in _live_helper_threads()}
    calls = {"n": 0}

    def _check():
        calls["n"] += 1
        if calls["n"] > 3:
            raise CancelledError("stop")

    started = time.monotonic()
    with pytest.raises(CancelledError):
        _engine(timeout=60.0).transcribe(audio, check_cancel=_check)
    assert time.monotonic() - started < 8.0  # killed, not waited out
    # The reader threads the engine started must have exited, not stayed
    # blocked on a pipe we forgot to close.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        leak = [t for t in _live_helper_threads() if t.ident not in before]
        if not leak:
            break
        time.sleep(0.1)
    assert not [t for t in _live_helper_threads() if t.ident not in before]


def _live_helper_threads():
    main = threading.current_thread()
    return [t for t in threading.enumerate() if t is not main and t.is_alive()]


# --- child env: telemetry forced off --------------------------------------


def test_child_environment_forces_telemetry_off(monkeypatch, tmp_path, fake_cli):
    """The environment override is asserted on the *production* launch call.

    ``W.subprocess.Popen`` is the real call the engine makes to start the native
    binary, so spying on it checks that the running code passes the telemetry-off
    environment to the actual child - not merely that the ``whistle_child_env``
    helper returns sane values in isolation. A parent that opted in must still be
    overridden, and unrelated variables must survive the override.
    """
    audio = _write_wav(tmp_path / "a.wav", 5.0)
    _word_script(monkeypatch, [["hi", 0.1, 0.5, 0.9]])
    # Parent opts in; the launched child must still see telemetry off.
    monkeypatch.setenv("NEEDLE_TELEMETRY", "1")
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    monkeypatch.setenv("TEXTFLOWKIT_W2_SENTINEL", "keep-me")

    captured = {}
    real_popen = W.subprocess.Popen

    def _spy(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(W.subprocess, "Popen", _spy)
    _engine().transcribe(audio)
    env = captured["env"]
    assert env is not None, "the native launch passed no environment"
    assert env["NEEDLE_TELEMETRY"] == "0"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["CI"] == "1"
    # The override replaces the parent's opt-in values, and unrelated variables
    # are forwarded rather than dropped.
    assert env["TEXTFLOWKIT_W2_SENTINEL"] == "keep-me"


# --- progress and resume ---------------------------------------------------


def test_on_progress_fires_once_per_completed_core(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "meeting.wav", 66.0)
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio, on_progress=snapshots.append)
    assert len(snapshots) == 3  # 66 s -> 3 cores
    assert [s["completed_core_index"] for s in snapshots] == [1, 2, 3]


def test_resume_skips_completed_cores_without_duplicate_words(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "meeting.wav", 66.0)
    # Window 0's clip is [0, 28], so a clip-relative word at 1.0 s is absolute
    # 1.0 s -> core 0. Windows 1 and 2 see clip-relative 1.0 too, i.e. 25.0 s
    # and 53.0 s. Each is owned by exactly one core, so no word repeats.
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])

    snapshots = []
    full = _engine().transcribe(audio, on_progress=snapshots.append)
    assert len(snapshots) == 3
    # Resume from the snapshot after the first core: cores 1 and 2 are skipped,
    # and the resumed run must not re-emit core 0's word.
    resumed = _engine().transcribe(audio, resume_progress=snapshots[0])
    resumed_words = [(w.start, w.text) for seg in resumed.segments for w in seg.words]
    assert len(resumed_words) == len(set(resumed_words))  # no duplicates
    # A resumed run's earlier (adopted) words are a prefix of the full run's.
    full_words = [(w.start, w.text) for seg in full.segments for w in seg.words]
    assert resumed_words == full_words


def test_stale_resume_for_different_audio_is_refused(monkeypatch, tmp_path, fake_cli):
    audio_a = _write_wav(tmp_path / "a.wav", 66.0)
    # A genuinely different recording of the *same length*: the identity must
    # be content-based, so same-size audio is still refused.
    audio_b = _write_wav(tmp_path / "b.wav", 66.0, silence=True)
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio_a, on_progress=snapshots.append)
    with pytest.raises(W.WhistleError, match="different audio"):
        _engine().transcribe(audio_b, resume_progress=snapshots[0])


def test_resume_under_a_different_language_is_refused(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 66.0)
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio, on_progress=snapshots.append)
    with pytest.raises(W.WhistleError, match="different language"):
        _engine().transcribe(audio, language="de", resume_progress=snapshots[0])


def test_corrupt_progress_segment_is_refused(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 66.0)
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio, on_progress=snapshots.append)
    broken = dict(snapshots[0])
    broken["segments"] = [{"start": float("nan"), "end": 1.0, "text": "x"}]
    with pytest.raises(W.WhistleError, match="non-finite"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_schema_mismatch_is_refused(monkeypatch, tmp_path, fake_cli):
    audio = _write_wav(tmp_path / "a.wav", 66.0)
    _word_script(monkeypatch, [["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio, on_progress=snapshots.append)
    broken = dict(snapshots[0])
    broken["schema"] = "other/9"
    with pytest.raises(W.WhistleError, match="schema"):
        _engine().transcribe(audio, resume_progress=broken)


def _snapshot(monkeypatch, tmp_path, fake_cli, *, seconds=66.0, word=None):
    """One real progress snapshot, taken from an actual run."""
    audio = _write_wav(tmp_path / "meeting.wav", seconds)
    _word_script(monkeypatch, [word or ["w", 1.0, 1.5, 0.9]])
    snapshots = []
    _engine().transcribe(audio, on_progress=snapshots.append)
    return audio, snapshots


def test_progress_with_a_zero_index_but_segments_is_refused(monkeypatch, tmp_path, fake_cli):
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])
    broken["completed_core_index"] = 0  # claims no work, but carries a segment
    with pytest.raises(W.WhistleError, match="no completed cores"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_segment_from_much_later_audio_is_refused(monkeypatch, tmp_path, fake_cli):
    """A segment owned past the completed prefix must not be adopted."""
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])  # completed_core_index == 1 (core 0, ends at 26 s)
    broken["segments"] = [{
        "start": 50.0, "end": 52.0, "text": "far later",
        "words": [{"start": 50.0, "end": 50.4, "text": "far"}],
    }]
    with pytest.raises(W.WhistleError, match="beyond the completed cores"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_with_a_word_owned_past_the_prefix_is_refused(monkeypatch, tmp_path, fake_cli):
    """The segment may begin early, but a word's midpoint must be in the prefix."""
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])
    # Segment start is inside the prefix, but the word's midpoint (30 s) is well
    # inside core 1, which the checkpoint has not completed.
    broken["segments"] = [{
        "start": 25.0, "end": 31.0, "text": "late",
        "words": [{"start": 29.6, "end": 30.4, "text": "late"}],
    }]
    with pytest.raises(W.WhistleError, match="owned after the completed cores"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_with_a_segment_starting_early_in_context_is_allowed(
    monkeypatch, tmp_path, fake_cli
):
    """The counterpart: a valid checkpoint whose start reaches back into context.

    Core 0 ends at 26 s; a segment owned by it may start a little before the
    boundary only if its words' midpoints stay inside the prefix. A segment that
    begins at 24 s with all its words inside [0, 26) is legitimate.
    """
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    good = dict(snapshots[0])
    good["segments"] = [{
        "start": 24.0, "end": 25.5, "text": "back",
        "words": [
            {"start": 24.0, "end": 24.4, "text": "back"},
            {"start": 25.0, "end": 25.4, "text": "again"},
        ],
    }]
    resumed = _engine().transcribe(audio, resume_progress=good)
    adopted = [(w.start, w.text) for seg in resumed.segments for w in seg.words]
    assert (24.0, "back") in adopted and (25.0, "again") in adopted


@pytest.mark.parametrize(
    "words",
    [
        [{"start": float("nan"), "end": 1.0, "text": "x"}],          # nested NaN
        [{"start": 2.0, "end": 1.0, "text": "x"}],                    # reversed
        [{"start": 1.0, "end": 1.0, "text": "x"}],                    # zero length
        [{"start": "soon", "end": 2.0, "text": "x"}],                 # non-numeric
        [{"start": 1.0, "end": 1.8, "text": "b"},
         {"start": 0.2, "end": 0.9, "text": "a"}],                    # out of order
    ],
)
def test_corrupt_nested_words_in_a_checkpoint_are_refused(
    monkeypatch, tmp_path, fake_cli, words
):
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])
    broken["segments"] = [{"start": 0.0, "end": 2.0, "text": "x", "words": words}]
    with pytest.raises(W.WhistleError, match="resume progress"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_with_out_of_order_segments_is_refused(monkeypatch, tmp_path, fake_cli):
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])
    broken["segments"] = [
        {"start": 10.0, "end": 12.0, "text": "later", "words": []},
        {"start": 1.0, "end": 2.0, "text": "earlier", "words": []},
    ]
    with pytest.raises(W.WhistleError, match="out-of-order"):
        _engine().transcribe(audio, resume_progress=broken)


def test_progress_with_context_seam_segment_overlap_is_accepted(
    monkeypatch, tmp_path, fake_cli
):
    """A legitimately *overlapping* seam must not be refused as out-of-order.

    A real 4 h run emitted 57 adjacent segment pairs whose intervals overlap at
    core seams (segment *starts* strictly increasing, ends crossing the next
    start by a few hundred ms) because each core's context reaches into its
    neighbour. The previous non-overlap rule refused that valid output. Ordering
    is by start; only a *reversed* start is corruption. This mirrors the real
    seam example (prior 101.84-104.00, next 103.92-107.04) scaled to core 0.
    """
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    good = dict(snapshots[0])  # completed core 0 (ends at 26 s)
    good["segments"] = [
        {"start": 1.84, "end": 4.0, "text": "prior",
         "words": [{"start": 1.84, "end": 2.24, "text": "prior"}]},
        {"start": 3.92, "end": 7.04, "text": "next",
         "words": [{"start": 3.92, "end": 4.16, "text": "next"}]},
    ]
    # Must not raise: the second segment starts after the first (monotonic), even
    # though its interval reaches back before the first segment's end.
    resumed = _engine().transcribe(audio, resume_progress=good)
    adopted = [(seg.start, seg.text) for seg in resumed.segments]
    assert (1.84, "prior") in adopted and (3.92, "next") in adopted


def test_progress_with_reversed_start_segment_is_still_refused(
    monkeypatch, tmp_path, fake_cli
):
    """The seam fix narrows the rule to *start* order, not remove it.

    A later segment whose start precedes an earlier one is the real signal of
    shuffled or corrupt data and must still be refused.
    """
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    broken = dict(snapshots[0])
    broken["segments"] = [
        {"start": 3.92, "end": 7.04, "text": "next", "words": []},
        # Starts *before* the prior segment started: reversed order.
        {"start": 1.84, "end": 4.00, "text": "prior", "words": []},
    ]
    with pytest.raises(W.WhistleError, match="out-of-order"):
        _engine().transcribe(audio, resume_progress=broken)


def test_a_saved_complete_progress_validates_and_derives_its_block_count(
    monkeypatch, tmp_path, fake_cli
):
    """A real saved body without ``total_cores`` stays valid; N is derived.

    The coordinator's 4 h run (555 cores, 2875 segments) wrote its progress
    before ``total_cores`` existed. `validate_progress` must accept that legacy
    body and derive the block count from the window plan it rebuilt from the
    same audio, so a resumed display can still say "block N/M".
    """
    audio, snapshots = _snapshot(monkeypatch, tmp_path, fake_cli)
    legacy = dict(snapshots[-1])  # a completed-prefix body from a real run
    legacy.pop("total_cores", None)  # as an older engine wrote it
    adopted = W.validate_progress(
        legacy,
        wav_identity_value=legacy["wav_identity"],
        duration=legacy["duration"],
        model_sha256=legacy["model_sha256"],
        binary_sha256=legacy["binary_sha256"],
        language=legacy["language"],
        windows=W.plan_windows(legacy["duration"]),
    )
    assert adopted.total_cores == len(W.plan_windows(legacy["duration"]))
    # And the engine, handed the legacy body, adopts it without re-running core 0.
    resumed = _engine().transcribe(audio, resume_progress=legacy)
    assert resumed.segments  # adopted segments survived
