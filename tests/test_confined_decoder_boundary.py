"""The decoder itself must be told which formats a confined input may be.

The signature refusal in `stage_confined_local_media` refuses known manifest
*shapes* before a copy exists. It is defense in depth, not the boundary: it only
covers forms whose leading bytes are in its list, and it is a property of a
Python function, not of the decode that actually opens files. The boundary with
the decoder is which demuxers the decode is allowed to select. Without it, a
confined HLS playlist naming a segment outside the input root makes ffmpeg read
that segment and return its audio.

These tests therefore disable the signature check and assert the *decoder* still
refuses, so a passing run proves the restriction lives in the ffmpeg command
line rather than in the staging helper. That is the check that fails for the
code as it stands (`-format_whitelist` absent), and it keeps meaning after the
signature list is widened or removed.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from textflowkit.sources import acquire
from textflowkit.sources.acquire import AcquisitionError, extract_audio, stage_confined_local_media

# Every format the product supports as a starting input, spelled as ffmpeg
# reports the demuxer to `ffmpeg -demuxers` (see SECURITY.md).
SUPPORTED_DEMUXERS = (
    "wav", "mp3", "mov,mp4,m4a,3gp,3g2,mj2",
    "matroska,webm", "ogg", "flac", "aac",
)


@pytest.fixture
def signatures_disabled(monkeypatch):
    """Neutralize the leading-byte refusal so only the decoder can refuse.

    If a test using this fixture passes, the refusal came from ffmpeg's own
    format restriction; if it fails, nothing on the decode path refused and the
    manifest was followed.
    """
    monkeypatch.setattr(acquire, "assert_direct_local_media", lambda *a, **k: None)
    yield


def _synthetic_segment(path: Path) -> Path:
    """A 2-second MPEG-TS segment, generated locally by ffmpeg (no network)."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:a", "aac", "-f", "mpegts", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _synthetic_wav(path: Path, *, seconds: float = 0.5) -> Path:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x01" * frames)
    return path


def _hls_playlist(segment_reference: str) -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:2.0,\n"
        f"{segment_reference}\n"
        "#EXT-X-ENDLIST\n"
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return root, outside, scratch


def test_hls_relative_reference_is_refused_by_the_decoder(tmp_path, signatures_disabled):
    """The measured exploit: a `..` HLS reference resolved from the staged copy.

    The playlist is staged into `<work_dir>/media`, so a reference relative to
    the playlist escapes the input root without naming an absolute path. Without
    a decoder format restriction ffmpeg loads the outside segment and returns its
    audio (measured: 65388 bytes of PCM), so this test fails on the unfixed code.
    """
    root, outside, scratch = _layout(tmp_path)
    _synthetic_segment(outside / "private-synthetic.ts")
    # The reference is relative to the staged copy, which is what ffmpeg opens.
    staged_dir = scratch / "media"
    playlist = root / "list.m3u8"
    playlist.write_text(
        _hls_playlist("../../outside/private-synthetic.ts"), encoding="utf-8"
    )
    staged = stage_confined_local_media(playlist, work_dir=scratch, input_root=root)
    assert staged.parent == staged_dir

    with pytest.raises(AcquisitionError):
        extract_audio(staged, work_dir=scratch, confined=True)


def test_hls_absolute_reference_is_refused_by_the_decoder(tmp_path, signatures_disabled):
    """An absolute reference outside the root is refused at the decoder too."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    playlist = root / "absolute.m3u8"
    # A path form the HLS demuxer accepts on this platform: forward slashes even
    # on Windows, which is what a caller staging a playlist would write.
    playlist.write_text(_hls_playlist(segment.as_posix()), encoding="utf-8")
    staged = stage_confined_local_media(playlist, work_dir=scratch, input_root=root)

    with pytest.raises(AcquisitionError):
        extract_audio(staged, work_dir=scratch, confined=True)


def test_concat_script_is_refused_by_the_decoder(tmp_path, signatures_disabled):
    """An ffmpeg concat script is refused at the decoder, not only at staging."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    script = root / "list.ffconcat"
    script.write_text(
        f"ffconcat version 1.0\nfile {segment.as_posix()}\n", encoding="utf-8"
    )
    staged = stage_confined_local_media(script, work_dir=scratch, input_root=root)

    with pytest.raises(AcquisitionError, match="ffconcat|Unsafe file name|Failed"):
        extract_audio(staged, work_dir=scratch, confined=True)


def test_playlist_renamed_to_a_media_extension_is_refused_by_the_decoder(
    tmp_path, signatures_disabled
):
    """A playlist named `.mp3` is refused at the decoder, not by extension."""
    root, outside, scratch = _layout(tmp_path)
    _synthetic_segment(outside / "private-synthetic.ts")
    disguised = root / "renamed.mp3"
    disguised.write_text(
        _hls_playlist("../../outside/private-synthetic.ts"), encoding="utf-8"
    )
    staged = stage_confined_local_media(disguised, work_dir=scratch, input_root=root)

    with pytest.raises(AcquisitionError):
        extract_audio(staged, work_dir=scratch, confined=True)


@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".webm", ".mkv", ".mp4"])
def test_supported_formats_still_decode_under_the_restriction(tmp_path, suffix):
    """The restriction must not cost support for any documented input format."""
    _root, _outside, scratch = _layout(tmp_path)
    source = tmp_path / f"clip{suffix}"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "pcm_s16le" if suffix == ".wav" else "libmp3lame"
            if suffix == ".mp3" else "flac" if suffix == ".flac"
            else "libvorbis" if suffix in (".ogg", ".webm", ".mkv")
            else "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    audio = extract_audio(source, work_dir=scratch, confined=True)
    with wave.open(str(audio), "rb") as wav:
        assert wav.getnframes() > 0


def test_plain_wav_and_mp4_still_decode(tmp_path):
    """The two ordinary inputs named in the directive keep working unchanged."""
    _root, _outside, scratch = _layout(tmp_path)
    wav_source = _synthetic_wav(tmp_path / "plain.wav")
    mp4_source = tmp_path / "plain.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac", str(mp4_source),
        ],
        check=True,
        capture_output=True,
    )
    for source in (wav_source, mp4_source):
        audio = extract_audio(source, work_dir=scratch, confined=True)
        with wave.open(str(audio), "rb") as wav:
            assert wav.getnframes() > 0


def test_ffprobe_is_restricted_for_confined_inputs(tmp_path):
    """ffprobe opens an input the same way ffmpeg does, so it needs the whitelist too.

    `enforce_predecode_limits` probes the *staged* media before `extract_audio`
    runs, so the decode restriction alone would leave the probe as the first
    thing to follow a confined playlist's outside reference.
    """
    from textflowkit.core.service import _probe_duration

    _root, outside, scratch = _layout(tmp_path)
    _synthetic_segment(outside / "private-synthetic.ts")
    staged = scratch / "media" / "rel.m3u8"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        _hls_playlist("../../outside/private-synthetic.ts"), encoding="utf-8"
    )

    # Unconfined, the probe really does follow the reference (2s segment).
    assert _probe_duration(staged) == pytest.approx(2.0, abs=0.5)
    # Confined, the playlist is not on the whitelist, so the probe returns
    # "unknown" instead of reading the outside segment.
    assert _probe_duration(staged, confined=True) is None


def test_ffprobe_still_probes_supported_formats(tmp_path):
    """The probe restriction must not cost duration checks for ordinary media."""
    from textflowkit.core.service import _probe_duration

    _root, _outside, _scratch = _layout(tmp_path)
    wav = _synthetic_wav(tmp_path / "probe.wav", seconds=0.5)
    assert _probe_duration(wav, confined=True) == pytest.approx(0.5, abs=0.2)
