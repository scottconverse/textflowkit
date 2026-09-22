"""Renderer correctness, including timestamp formats."""

from __future__ import annotations

from tests.test_model import sample
from textflowkit.core.timeutil import srt_timestamp, vtt_timestamp
from textflowkit.render import SUPPORTED_FORMATS, render


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


def test_txt_plain_omits_speakers_by_default():
    out = render(sample(), "txt")
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
