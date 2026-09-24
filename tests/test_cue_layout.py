"""C1: a long segment becomes readable, word-timed subtitle cues.

The contract under test:

* no subtitle line is longer than the readability target (42 code points),
* no cue has more than two lines,
* a cue's interval comes from the first and last source word it shows, when
  those word timings exist, are ordered, sit inside the segment, and name the
  displayed words,
* otherwise the interval is a segment-time estimate, which is a different
  (weaker) claim and must never be presented as word precision,
* text safety, speaker labels and short existing cues are unchanged.
"""

from __future__ import annotations

import pytest

from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.render import render
from textflowkit.render._cue_layout import (
    MAX_LINE_CHARS,
    MAX_LINES_PER_CUE,
    layout_cues,
)

# The long segment the coordinator measured on a real clip: 86 characters and
# nine seconds on one screen under the old one-segment-one-cue renderer.
LONG = "Cool thing for these guys is that they have really really long prompts and that's cool."
SEG_START = 5.24
SEG_END = 14.24
STEP = 0.5


def _worded(
    *,
    text: str = LONG,
    start: float = SEG_START,
    end: float = SEG_END,
    step: float = STEP,
    speaker: str | None = None,
) -> Segment:
    """A segment whose word timings name exactly the displayed words."""
    words = text.split()
    return Segment(
        start,
        end,
        text,
        speaker=speaker,
        words=[WordTiming(start + i * step, start + (i + 1) * step, w) for i, w in enumerate(words)],
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


def _rendered_lines(cues) -> list[str]:
    return [line for cue in cues for line in cue.lines]


# --- readable geometry -------------------------------------------------------


def test_long_word_timed_segment_splits_into_readable_cues():
    """The 86-character, nine-second segment must not stay on one screen."""
    cues = layout_cues(_worded())
    assert len(cues) >= 2
    for cue in cues:
        assert 1 <= len(cue.lines) <= MAX_LINES_PER_CUE
        for line in cue.lines:
            assert len(line) <= MAX_LINE_CHARS, f"overlong line: {line!r}"


def test_no_word_is_lost_or_reordered_by_wrapping():
    cues = layout_cues(_worded())
    assert " ".join(_rendered_lines(cues)).split() == LONG.split()


def test_cue_boundaries_are_the_first_and_last_word_of_the_cue():
    seg = _worded()
    words = seg.words
    starts = {round(w.start, 6) for w in words}
    ends = {round(w.end, 6) for w in words}
    for cue in layout_cues(seg):
        assert round(cue.start, 6) in starts
        assert round(cue.end, 6) in ends


def test_cue_intervals_are_ordered_and_do_not_overlap():
    cues = layout_cues(_worded())
    for earlier, later in zip(cues, cues[1:]):
        assert later.start >= earlier.end
    for cue in cues:
        assert cue.end > cue.start


def test_short_line_keeps_its_exact_spacing():
    """A line that already fits is emitted as written, not re-tokenized."""
    seg = Segment(0.0, 2.0, "a  b   c")
    assert layout_cues(seg)[0].lines == ("a  b   c",)


def test_short_segment_stays_one_line_and_one_cue():
    tr = Transcript(source="x", segments=[Segment(0.0, 2.0, "Short and sweet.")])
    assert _srt_cues(render(tr, "srt")) == [
        ("00:00:00,000 --> 00:00:02,000", "Short and sweet.")
    ]


# --- one layout shared by both formats ---------------------------------------


def test_srt_and_vtt_split_and_time_identically():
    """The two renderers must not disagree about where a cue breaks."""
    tr = Transcript(source="x", segments=[_worded()])
    srt = _srt_cues(render(tr, "srt"))
    vtt = _vtt_cues(render(tr, "vtt"))
    assert len(srt) == len(vtt) >= 2
    assert [t.replace(",", ".") for t, _ in srt] == [t for t, _ in vtt]
    assert [b for _, b in srt] == [b for _, b in vtt]
    assert " ".join(b for _, b in srt).split() == LONG.split()


# --- honest timing for text the words do not describe ------------------------


def _skewed_words(text: str, segment: Segment) -> None:
    """Word timings packed into the first second of a ten-second segment."""
    words = text.split()
    segment.words = [WordTiming(i * 0.05, (i + 1) * 0.05, w) for i, w in enumerate(words)]


def test_translated_text_is_laid_out_on_segment_time_not_source_word_time():
    """Translated words are not the timed words, so the timing must be an estimate."""
    seg = Segment(0.0, 10.0, LONG)
    _skewed_words(LONG, seg)
    seg.translated_text = (
        "Lo bueno para estos chicos es que tienen indicaciones realmente "
        "muy largas y eso esta genial."
    )
    cues = layout_cues(seg)
    assert len(cues) >= 2
    for cue in cues:
        assert len(cue.lines) <= MAX_LINES_PER_CUE
        for line in cue.lines:
            assert len(line) <= MAX_LINE_CHARS
    # The estimate spans the whole segment; the source words stop at 0.8s.
    assert cues[0].start == pytest.approx(0.0)
    assert cues[-1].end == pytest.approx(10.0)
    assert max(cue.end for cue in cues) > 5.0


def test_wordless_legacy_segment_is_readable_on_a_segment_time_estimate():
    seg = Segment(1.0, 9.0, LONG)
    cues = layout_cues(seg)
    assert len(cues) >= 2
    assert cues[0].start == pytest.approx(1.0)
    assert cues[-1].end == pytest.approx(9.0)
    for earlier, later in zip(cues, cues[1:]):
        assert later.start >= earlier.end


def test_words_that_do_not_name_the_displayed_text_are_not_trusted():
    """Alignment is a precondition, not an assumption."""
    seg = Segment(0.0, 10.0, LONG)
    words = LONG.split()
    seg.words = [WordTiming(i * 0.5, (i + 1) * 0.5, "different") for i in range(len(words))]
    cues = layout_cues(seg)
    assert cues[-1].end == pytest.approx(10.0)


def test_words_outside_the_segment_are_not_trusted():
    seg = _worded(start=5.24, end=14.24)
    seg.words = [WordTiming(w.start + 100.0, w.end + 100.0, w.text) for w in seg.words]
    cues = layout_cues(seg)
    assert cues[0].start == pytest.approx(5.24)
    assert cues[-1].end == pytest.approx(14.24)


def test_unordered_word_timings_fall_back_instead_of_overlapping():
    seg = _worded()
    seg.words = list(reversed(seg.words))
    cues = layout_cues(seg)
    for earlier, later in zip(cues, cues[1:]):
        assert later.start >= earlier.end
    for cue in cues:
        assert cue.end > cue.start


def test_degenerate_segment_still_gets_positive_ordered_cues():
    """A zero-length interval cannot be split honestly; it must not go backwards."""
    seg = Segment(3.0, 3.0, LONG)
    cues = layout_cues(seg)
    assert len(cues) >= 1
    for earlier, later in zip(cues, cues[1:]):
        assert later.start >= earlier.end
    for cue in cues:
        assert cue.end > cue.start


# --- text safety survives the split ------------------------------------------


HOSTILE_LONG = (
    "first line with --> arrow and <tag> & more words here\n"
    "\n"
    "second line that is also long enough that it has to wrap somewhere"
)


def test_hostile_payload_within_a_split_segment_stays_inside_its_cues():
    tr = Transcript(source="x", segments=[Segment(0.0, 8.0, HOSTILE_LONG)])
    out = render(tr, "srt")
    cues = _srt_cues(out)
    assert len(cues) >= 2
    # The only arrows a player may read are the cues' own timing lines.
    assert out.count("-->") == len(cues)
    for _, body in cues:
        assert "-->" not in body
        assert body.strip() != ""


def test_vtt_escapes_every_line_of_a_split_segment():
    tr = Transcript(source="x", segments=[Segment(0.0, 8.0, HOSTILE_LONG)])
    out = render(tr, "vtt")
    cues = _vtt_cues(out)
    assert len(cues) >= 2
    assert out.count("-->") == len(cues)
    bodies = "\n".join(body for _, body in cues)
    assert "&lt;tag&gt;" in bodies
    assert "--&gt;" in bodies
    for _, body in cues:
        assert "-->" not in body
        assert "<tag>" not in body


def test_speaker_label_is_not_split_by_wrapping():
    tr = Transcript(source="x", segments=[_worded(speaker="A\n\nB")])
    for _, body in _srt_cues(render(tr, "srt")):
        assert body.split("\n")[0] == "A"
        assert body.split("\n")[1].startswith("B: ")


def test_srt_speaker_label_counts_toward_line_width_and_repeats_per_cue():
    tr = Transcript(source="x", segments=[_worded(speaker="SPEAKER ONE")])
    cues = _srt_cues(render(tr, "srt"))
    assert len(cues) >= 2
    for _, body in cues:
        for line in body.split("\n"):
            assert len(line) <= MAX_LINE_CHARS
        assert body.split("\n")[0].startswith("SPEAKER ONE: ")


def test_vtt_voice_span_repeats_per_cue_and_stays_markup():
    tr = Transcript(source="x", segments=[_worded(speaker="A>B")])
    cues = _vtt_cues(render(tr, "vtt"))
    assert len(cues) >= 2
    for _, body in cues:
        assert body.startswith("<v A&gt;B>")
        assert "<v A>B" not in body


# --- edge cases --------------------------------------------------------------


def test_single_overlong_token_gets_its_own_line_intact():
    word = "x" * 100
    seg = Segment(0.0, 3.0, f"short start {word} short end")
    cues = layout_cues(seg)
    for cue in cues:
        assert len(cue.lines) <= MAX_LINES_PER_CUE
    lines = _rendered_lines(cues)
    assert word in lines, "an over-long token must not be split or dropped"
    assert " ".join(lines).split() == seg.text.split()
    assert [line for line in lines if len(line) > MAX_LINE_CHARS] == [word]


def test_unicode_text_wraps_by_code_point_without_losing_characters():
    text = "日本語のテキストです " * 8
    seg = Segment(0.0, 4.0, text.strip())
    cues = layout_cues(seg)
    assert len(cues) >= 1
    for cue in cues:
        assert len(cue.lines) <= MAX_LINES_PER_CUE
        for line in cue.lines:
            assert len(line) <= MAX_LINE_CHARS
    assert " ".join(_rendered_lines(cues)).split() == seg.text.split()


def test_combining_marks_are_never_split_apart():
    # "e" + U+0301 COMBINING ACUTE ACCENT: two code points, one visible letter.
    text ="café " * 20
    seg = Segment(0.0, 4.0, text.strip())
    cues = layout_cues(seg)
    assert " ".join(_rendered_lines(cues)).split() == seg.text.split()


def test_canonical_transcript_is_not_mutated_by_rendering():
    seg = _worded()
    seg.translated_text = "texto"
    tr = Transcript(source="x", segments=[seg])
    before = tr.to_dict()
    render(tr, "srt")
    render(tr, "vtt")
    assert tr.to_dict() == before
