"""Renderer correctness, including timestamp formats."""

from __future__ import annotations

from tests.test_model import sample
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.timeutil import srt_timestamp, vtt_timestamp
from textflowkit.render import SUPPORTED_FORMATS, render, render_bytes, write_all

HOSTILE_TEXT = "line one\n\nline two with --> arrow & <tag>"


def hostile() -> Transcript:
    """A translation carrying every character that can escape a cue (A11)."""
    return Transcript(
        source="https://example.com/x",
        language="en",
        segments=[
            Segment(0.0, 2.5, "source", speaker="A", translated_text=HOSTILE_TEXT),
            Segment(2.5, 6.0, "second cue"),
        ],
    )


def _srt_cues(out: str) -> list[tuple[str, str]]:
    """Read SRT back the way a player does: blank-line-separated blocks."""
    cues: list[tuple[str, str]] = []
    for block in out.strip().split("\n\n"):
        lines = block.split("\n")
        assert len(lines) >= 2, f"block without a timing line: {block!r}"
        cues.append((lines[1], "\n".join(lines[2:])))
    return cues


def _vtt_cues(out: str) -> list[tuple[str, str]]:
    """Read VTT back the way a player does: header, then blank-line blocks."""
    blocks = out.split("\n\n")
    assert blocks[0] == "WEBVTT"
    cues: list[tuple[str, str]] = []
    for block in blocks[1:]:
        block = block.strip("\n")
        if not block:
            continue
        lines = block.split("\n")
        assert len(lines) >= 2, f"block without a timing line: {block!r}"
        cues.append((lines[0], "\n".join(lines[1:])))
    return cues


def test_srt_keeps_hostile_payload_inside_one_cue():
    """A11: an interior blank line or "-->"/"&"/"<" must not break the cue."""
    out = render(hostile(), "srt")
    cues = _srt_cues(out)
    assert len(cues) == 2
    assert cues[0][0] == "00:00:00,000 --> 00:00:02,500"
    assert cues[1][0] == "00:00:02,500 --> 00:00:06,000"
    # The only arrows left are the two timing lines' own.
    assert out.count("-->") == 2
    assert cues[0][1] == "A: line one\nline two with -> arrow & <tag>"
    assert cues[1][1] == "second cue"


def test_srt_arrow_run_does_not_survive_as_an_arrow():
    """A longer run such as "---->" must not keep the arrow in the payload."""
    tr = Transcript(
        source="x",
        segments=[Segment(0.0, 2.5, "a ---> b -----> c")],
    )
    out = render(tr, "srt")
    assert out.count("-->") == 1
    assert _srt_cues(out) == [("00:00:00,000 --> 00:00:02,500", "a -> b -> c")]


def test_vtt_escapes_hostile_payload_and_keeps_both_cues():
    """A11: WebVTT cue text is parsed, so reserved characters get references."""
    out = render(hostile(), "vtt")
    cues = _vtt_cues(out)
    assert len(cues) == 2
    assert cues[0][0] == "00:00:00.000 --> 00:00:02.500"
    assert cues[1][0] == "00:00:02.500 --> 00:00:06.000"
    assert out.count("-->") == 2
    assert cues[0][1] == "<v A>line one\nline two with --&gt; arrow &amp; &lt;tag&gt;"
    assert cues[1][1] == "second cue"


def test_vtt_timing_arrow_and_voice_markup_survive_escaping():
    """The fix must not escape the format's own arrow or <v ...> span."""
    out = render(hostile(), "vtt")
    assert "00:00:00.000 --> 00:00:02.500" in out
    assert "<v A>" in out


def test_vtt_speaker_annotation_cannot_close_the_tag_early():
    """A speaker label comes from the model, so it is sanitized too."""
    tr = Transcript(source="x", segments=[Segment(0.0, 2.5, "hi", speaker="A>B")])
    out = render(tr, "vtt")
    assert "<v A&gt;B>hi" in out
    assert _vtt_cues(out)[0][1] == "<v A&gt;B>hi"


def test_vtt_speaker_label_cannot_span_lines():
    tr = Transcript(source="x", segments=[Segment(0.0, 2.5, "hi", speaker="A\nB")])
    assert _vtt_cues(render(tr, "vtt"))[0][1] == "<v A B>hi"


def test_vtt_blank_speaker_label_emits_no_voice_span():
    """A whitespace-only label would make "<v >", an annotation-less tag."""
    tr = Transcript(source="x", segments=[Segment(0.0, 2.5, "hi", speaker="   ")])
    assert _vtt_cues(render(tr, "vtt"))[0][1] == "hi"


def test_srt_speaker_label_cannot_split_the_cue():
    tr = Transcript(source="x", segments=[Segment(0.0, 2.5, "hi", speaker="A\n\nB")])
    cues = _srt_cues(render(tr, "srt"))
    assert len(cues) == 1
    assert cues[0][1] == "A\nB: hi"


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
