"""A confined local input must not make the decoder read files outside the root.

SEC: a local media file can be a *reference* to other files. An HLS/M3U playlist,
a DASH manifest, or an ffmpeg concat script names segments/paths that the demuxer
opens while decoding. Confined local input is handle-verified and copied into
scratch, but only the top-level file is copied - so a playlist inside the root
can name a file outside it, and ffmpeg follows that reference from the staged
copy. Absolute references survive the copy unchanged; relative ones are resolved
against the scratch directory, so `..` reaches out from there.

These tests pin the boundary at the staging step, because that is where the copy
ffmpeg later opens is created. Ordinary media (WAV, MPEG-TS) must keep working,
and the restriction must apply only when an input root is configured.
"""

from __future__ import annotations

import os
import subprocess
import wave
from pathlib import Path

import pytest

from textflowkit.sources.acquire import AcquisitionError, extract_audio, stage_confined_local_media

# Stable phrases from the refusal messages. Matching on them keeps the tests
# honest about *why* a file was refused (a format restriction), not merely that
# something failed.
HLS_REFUSAL = "indirect media"


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
    """A plain PCM WAV, written without ffmpeg so it is always available."""
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
        "#EXTINF:2.0,\n"
        f"{segment_reference}\n"
        "#EXT-X-ENDLIST\n"
    )


def _decoded_frames(audio: Path) -> int:
    with wave.open(str(audio), "rb") as wav:
        assert wav.getframerate() == 16000
        return wav.getnframes()


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return root, outside, scratch


def test_confined_playlist_absolute_reference_is_refused(tmp_path):
    """The decoder must not consume a segment named by absolute path outside the root."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    playlist = root / "list.m3u8"
    playlist.write_text(_hls_playlist(segment.as_posix()), encoding="utf-8")

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(playlist, work_dir=scratch, input_root=root)

    # Nothing was staged, so there is no copy for ffmpeg to follow.
    assert not (scratch / "media" / "input.m3u8").exists()
    assert segment.exists()  # the test's own fixture file is untouched


def test_confined_playlist_relative_reference_is_refused(tmp_path):
    """A `..` reference resolved against the staged copy must not reach outside either.

    The staged playlist lives in `<work_dir>/media`, so a reference relative to
    it escapes the input root without ever naming an absolute path. Detection
    therefore cannot be a check for absolute paths or for the source location.
    """
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    staged_dir = scratch / "media"
    relative = Path(os.path.relpath(segment, staged_dir)).as_posix()
    assert relative.startswith(".."), relative  # the vector, stated explicitly
    playlist = root / "relative.m3u8"
    playlist.write_text(_hls_playlist(relative), encoding="utf-8")

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(playlist, work_dir=scratch, input_root=root)


def test_confined_playlist_with_in_root_segment_is_refused(tmp_path):
    """The restriction is by format, not by whether the reference escapes.

    A confined playlist naming a segment *inside* the root is refused too: the
    copy is staged into scratch, so a relative reference would no longer resolve
    to the file the caller named, and honouring arbitrary manifest references
    would mean reimplementing each demuxer's path resolution inside the security
    boundary. Refusing the format is the deliberate, documented trade.
    """
    root, _outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(root / "inside-segment.ts")
    playlist = root / "in-root.m3u8"
    playlist.write_text(_hls_playlist(segment.name), encoding="utf-8")

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(playlist, work_dir=scratch, input_root=root)


def test_confined_concat_script_is_refused(tmp_path):
    """The same class of file: an ffmpeg concat script naming another file."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    script = root / "list.txt"
    script.write_text(
        f"ffconcat version 1.0\nfile {segment.as_posix()}\n", encoding="utf-8"
    )

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(script, work_dir=scratch, input_root=root)


def test_confined_dash_manifest_is_refused(tmp_path):
    """An XML manifest (MPEG-DASH) is detected by content, not by `.mpd`."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    manifest = root / "manifest.mpd"
    manifest.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"'
        ' mediaPresentationDuration="PT2S" minBufferTime="PT2S">'
        '<Period><AdaptationSet mimeType="video/mp2t"><Representation id="1"'
        ' bandwidth="1000"><SegmentList duration="2">'
        f'<Initialization sourceURL="{segment.as_posix()}"/>'
        f'<SegmentURL media="{segment.as_posix()}"/>'
        "</SegmentList></Representation></AdaptationSet></Period></MPD>\n",
        encoding="utf-8",
    )

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(manifest, work_dir=scratch, input_root=root)


def test_confined_media_renamed_to_a_playlist_extension_is_still_refused(tmp_path):
    """Detection is by content: the extension is caller-controlled and not trusted."""
    root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    disguised = root / "actually-a-playlist.ts"
    disguised.write_text(_hls_playlist(segment.as_posix()), encoding="utf-8")

    with pytest.raises(AcquisitionError, match=HLS_REFUSAL):
        stage_confined_local_media(disguised, work_dir=scratch, input_root=root)


def test_confined_transport_stream_with_same_extension_still_decodes(tmp_path):
    """The segment extension that carried the reference is ordinary media inside the root."""
    root, _outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(root / "clip.ts")
    staged = stage_confined_local_media(segment, work_dir=scratch, input_root=root)
    assert _decoded_frames(extract_audio(staged, work_dir=scratch)) > 0


def test_confined_wav_still_decodes(tmp_path):
    """Ordinary local media is unaffected by the restriction."""
    root, _outside, scratch = _layout(tmp_path)
    wav = _synthetic_wav(root / "clip.wav")
    staged = stage_confined_local_media(wav, work_dir=scratch, input_root=root)
    assert staged.read_bytes() == wav.read_bytes()
    assert _decoded_frames(extract_audio(staged, work_dir=scratch)) > 0


def test_unconfined_playlist_is_untouched(tmp_path):
    """Without an input root there is no boundary, so the default path is unchanged.

    This also demonstrates what the confined case is protected against: ffmpeg
    really does follow the reference when nothing refuses it first.
    """
    _root, outside, scratch = _layout(tmp_path)
    segment = _synthetic_segment(outside / "private-synthetic.ts")
    playlist = tmp_path / "unconfined.m3u8"
    playlist.write_text(_hls_playlist(segment.as_posix()), encoding="utf-8")

    frames = _decoded_frames(extract_audio(playlist, work_dir=scratch))
    assert frames > 0
