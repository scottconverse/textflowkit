"""Renderer correctness, including timestamp formats."""

from __future__ import annotations

from tests.test_model import sample
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.timeutil import srt_timestamp, vtt_timestamp
from textflowkit.render import SUPPORTED_FORMATS, render, render_bytes, write_all


def test_srt_timestamp_format():
    assert srt_timestamp(0) == "00:00:00,000"
    assert srt_timestamp(3661.5) == "01:01:01,500"
    assert srt_timestamp(-5) == "00:00:00,000"


def test_vtt_timestamp_uses_period():
    assert vtt_timestamp(3661.5) == "01:01:01.500"


def test_srt_blocks():
    out = render(sample(), "srt")
    assert "1\n00:00:00,000 --> 00:00:02,500" in out
    assert "A: Hello there." in out
    assert out.strip().endswith("eres audaz")


def test_vtt_header_and_cues():
    out = render(sample(), "vtt")
    assert out.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.500" in out
    assert "<v A>Hello there." in out


def unlabelled() -> Transcript:
    """A transcript with no diarization at all."""
    return Transcript(
        source="https://example.com/y",
        language="en",
        segments=[
            Segment(0.0, 2.5, "Hello there."),
            Segment(2.5, 6.0, "General Kenobi."),
        ],
    )


def test_txt_labels_speakers_at_the_normal_render_path():
    """TXT joins srt/vtt/md in carrying the labels it was given (A5)."""
    out = render(sample(), "txt")
    assert out.splitlines()[0] == "A: Hello there."
    assert out.splitlines()[1] == "B: General Kenobi."
    # The third segment carries no speaker, so it must not borrow one.
    assert out.splitlines()[2] == "eres audaz"


def test_txt_bytes_and_write_all_agree_with_render():
    """render_bytes and the file writer are the same rendering as render()."""
    tr = sample()
    expected = "A: Hello there.\nB: General Kenobi.\neres audaz\n"
    assert render(tr, "txt") == expected
    assert render_bytes(tr, "txt").decode("utf-8") == expected


def test_txt_write_all_publishes_labelled_text(tmp_path):
    (path,) = write_all(sample(), formats=["txt"], output_dir=tmp_path, stem="talk")
    assert path.read_text(encoding="utf-8") == (
        "A: Hello there.\nB: General Kenobi.\neres audaz\n"
    )


def test_txt_unlabelled_transcript_stays_plain():
    tr = unlabelled()
    expected = "Hello there.\nGeneral Kenobi.\n"
    assert render(tr, "txt") == expected
    assert render_bytes(tr, "txt").decode("utf-8") == expected


def test_txt_callers_can_still_suppress_speakers():
    from textflowkit.render.txt import render_txt

    out = render_txt(sample(), speaker=False)
    assert out.splitlines()[0] == "Hello there."


def test_txt_with_speakers():
    from textflowkit.render.txt import render_txt

    out = render_txt(sample(), speaker=True)
    assert out.splitlines()[0] == "A: Hello there."


def test_txt_with_timestamps():
    from textflowkit.render.txt import render_txt

    out = render_txt(sample(), timestamps=True)
    assert out.splitlines()[0].startswith("[    0.00] ")


def test_markdown_has_anchor():
    out = render(sample(), "md", title="Talk")
    assert out.startswith("# Talk")
    assert "`00:00:00`" in out


def test_json_format_supported():
    out = render(sample(), "json")
    assert '"segments"' in out


def test_unknown_format_raises():
    # note: docx/pdf are supported now, but as BINARY formats - see
    # test_docx_is_binary_not_text below.
    try:
        render(sample(), "xyzzy")
    except ValueError as exc:
        assert "unsupported format" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_supported_formats_cover_core_set():
    for fmt in ("txt", "srt", "vtt", "json", "md"):
        assert fmt in SUPPORTED_FORMATS



def test_binary_formats_are_not_returned_as_text():
    """render() is the text path; binary formats must redirect to render_bytes."""
    for fmt in ("docx", "pdf"):
        try:
            render(sample(), fmt)
        except ValueError as exc:
            assert "binary" in str(exc)
        else:
            raise AssertionError(f"render() should refuse {fmt}")


def test_supported_formats_include_export_formats():
    from textflowkit.render import BINARY_FORMATS, TEXT_FORMATS

    for fmt in ("txt", "srt", "vtt", "json", "md"):
        assert fmt in TEXT_FORMATS
    for fmt in ("docx", "pdf"):
        assert fmt in BINARY_FORMATS
        assert fmt in SUPPORTED_FORMATS
