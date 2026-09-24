"""Shared cue layout for the time-coded subtitle renderers.

SRT and WebVTT differ in how a cue's text is escaped, not in where a cue
begins and ends. Both renderers call `layout_cues`, so one transcript cannot
come out with different splits or different cue times in the two formats.

Layout runs on *visible* text - what a viewer reads, before SRT's arrow
stand-in or WebVTT's character references - because the targets in this module
are a readability budget, not a byte count. Two targets apply: no line longer
than `MAX_LINE_CHARS` and no cue with more than `MAX_LINES_PER_CUE` lines.
They are targets rather than guarantees, because the alternative to an
over-long line is breaking a word, and a broken word is worse for a viewer
than a long one.

Timing has two modes, and they make different claims:

*Word-timed* - used only when the displayed text *is* the source text, the
segment carries word timings, those timings are finite, strictly ordered,
inside the segment, and name exactly the displayed words in order. A cue then
starts at its first word and ends at its last word.

*Estimated* - everything else: translated text, an older transcript with no
word timings, or word timings that fail the checks above. Cue times are then
proportional to how far a cue's characters reach into the segment. This is a
weaker claim and must not be presented as word precision - a segment's saved
word timings always describe the *source* speech, so they cannot time a
translation, and an imported transcript may have none at all.

A character that cannot be placed inside either mode is still shown: nothing
here drops text, and a segment whose own interval is unusable still produces
ordered, positive-length cues rather than a cue a player would skip.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from textflowkit.core.model import Segment
from textflowkit.render._cue_text import normalize_cue_lines

# Readability targets. See the module docstring: a single token longer than
# `MAX_LINE_CHARS` keeps its characters and overflows on its own line.
MAX_LINE_CHARS = 42
MAX_LINES_PER_CUE = 2

# The subtitle timestamp resolution is one millisecond, so this is the
# smallest duration a cue can express that a player will not read as zero.
MIN_CUE_SECONDS = 0.001

# Word timings are allowed to reach this far outside their segment before they
# are distrusted: Whisper's word boundaries and its segment boundaries are
# produced by different passes and do not always agree to the millisecond.
WORD_BOUNDS_TOLERANCE = 0.25

# A cue payload is a run of non-whitespace tokens. Whitespace is deliberately
# not a cue character: it is re-emitted between tokens when a line is rebuilt.
_TOKEN_RE = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class Cue:
    """One subtitle cue: an interval and the visible lines shown in it."""

    start: float
    end: float
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Token:
    """One whitespace-delimited token and where it sits in the payload."""

    text: str
    c0: int
    c1: int


@dataclass(frozen=True, slots=True)
class _Line:
    """One output line, plus the tokens it shows (for that line's timing)."""

    text: str
    tokens: tuple[int, ...]


def layout_cues(
    segment: Segment,
    *,
    include_translation: bool = True,
    inline_speaker: bool = False,
) -> list[Cue]:
    """Lay one transcript segment out as readable cues.

    `inline_speaker` folds the segment's speaker label into the visible text,
    which is what SRT does (it has no markup for a speaker). WebVTT keeps the
    label out of the text - its ``<v ...>`` span is markup, not something a
    viewer reads - so it leaves this off and adds the span itself.

    The canonical segment is only read, never written.
    """
    source_text = not (include_translation and segment.translated_text)
    payload = normalize_cue_lines(
        segment.display_text() if include_translation else segment.text
    )
    label_lines = _label_lines(segment) if inline_speaker else []

    payload_lines = payload.split("\n") if payload else []
    tokens, line_tokens, total_chars = _tokenize(payload_lines)

    # A cue repeats the label, because each cue is a separate screen: a
    # viewer of the third cue needs to know who is still speaking. Only the
    # label's last line shares a line with payload text; any earlier label
    # lines are lines of their own, so they come out of the cue's line budget.
    leading_label_lines = len(label_lines) - 1 if label_lines else 0
    payload_lines_per_cue = max(1, MAX_LINES_PER_CUE - leading_label_lines)
    # The label is visible text on the cue's first line, so it is spent from
    # that line's budget. A label at least as long as the target leaves the
    # budget at one character rather than none, so a token can still be placed
    # on the line instead of the line coming out empty.
    first_budget = MAX_LINE_CHARS - len(label_lines[-1]) if label_lines else MAX_LINE_CHARS
    first_budget = max(first_budget, 1)

    wrapped: list[_Line] = []
    for index, (line, indices) in enumerate(zip(payload_lines, line_tokens)):
        budget = first_budget if index == 0 else MAX_LINE_CHARS
        wrapped.extend(_wrap_line(line, indices, tokens, budget))

    groups = [
        wrapped[at : at + payload_lines_per_cue]
        for at in range(0, len(wrapped), payload_lines_per_cue)
    ]
    if not groups:
        groups = [[]]

    times = _cue_times(segment, tokens, groups, source_text)
    if times is None:
        times = _estimated_times(segment, tokens, groups, total_chars)

    cues: list[Cue] = []
    for group, (start, end) in zip(groups, times):
        if label_lines:
            first = _joined(label_lines[-1], group[0].text if group else "")
            lines = [*label_lines[:-1], first, *(line.text for line in group[1:])]
        else:
            lines = [line.text for line in group]
        cues.append(Cue(start=start, end=end, lines=tuple(lines)))
    return cues


# --- text and labels ---------------------------------------------------------


def _label_lines(segment: Segment) -> list[str]:
    """The SRT-visible speaker prefix, as lines.

    Rebuilt the way the previous renderer built it - the label is the front of
    the segment's text, ``"<speaker>: "`` - so an existing short cue keeps its
    exact bytes. The trailing space is what joins the label to the first
    payload line; ``normalize_cue_lines`` strips it, so it is put back.
    """
    if not segment.speaker:
        return []
    label = normalize_cue_lines(f"{segment.speaker}: ")
    if not label:
        return []
    parts = label.split("\n")
    parts[-1] = f"{parts[-1]} "
    return parts


def _joined(label_tail: str, first_payload_line: str) -> str:
    """Join the label's last line to the payload's first line.

    An empty payload means there is nothing for the label's separating space
    to separate, and a cue line does not keep trailing whitespace.
    """
    if not first_payload_line:
        return label_tail.rstrip()
    return f"{label_tail}{first_payload_line}"


def _tokenize(payload_lines: list[str]) -> tuple[list[_Token], list[list[int]], int]:
    """Split the payload into tokens, one entry per line, plus its length.

    One pass over the text, so a pathological segment costs time proportional
    to its characters rather than to characters times tokens.
    """
    tokens: list[_Token] = []
    line_tokens: list[list[int]] = []
    offset = 0
    for line in payload_lines:
        indices: list[int] = []
        for match in _TOKEN_RE.finditer(line):
            indices.append(len(tokens))
            tokens.append(
                _Token(match.group(), offset + match.start(), offset + match.end())
            )
        line_tokens.append(indices)
        offset += len(line) + 1
    total_chars = max(offset - 1, 1)
    return tokens, line_tokens, total_chars


def _wrap_line(line: str, indices: list[int], tokens: list[_Token], budget: int) -> list[_Line]:
    """Break one payload line into output lines within `budget` characters.

    A line that already fits is emitted exactly as it was written. Rebuilding
    it from tokens would collapse a deliberate double space, and a short cue's
    bytes should not change just because it was measured.
    """
    if not indices:
        return []
    if len(line) <= budget:
        return [_Line(line, tuple(indices))]

    out: list[_Line] = []
    current: list[int] = []
    length = 0
    limit = budget
    for index in indices:
        text = tokens[index].text
        if not current:
            current, length = [index], len(text)
        elif length + 1 + len(text) <= limit:
            current.append(index)
            length += 1 + len(text)
        else:
            out.append(_line_from(current, tokens))
            current, length, limit = [index], len(text), MAX_LINE_CHARS
    if current:
        out.append(_line_from(current, tokens))
    return out


def _line_from(indices: list[int], tokens: list[_Token]) -> _Line:
    return _Line(" ".join(tokens[i].text for i in indices), tuple(indices))


# --- timing ------------------------------------------------------------------


def _cue_times(
    segment: Segment,
    tokens: list[_Token],
    groups: list[list[_Line]],
    source_text: bool,
) -> list[tuple[float, float]] | None:
    """Word-timed cue intervals, or None when they cannot be trusted.

    None is the honest answer for translated text (its word timings describe
    the source speech), for a transcript with no word timings, and for timings
    that are unordered, out of bounds, or do not name the displayed words.
    """
    if not source_text:
        return None
    spans = _word_spans(segment, tokens)
    if spans is None:
        return None
    intervals: list[tuple[float, float]] = []
    for group in groups:
        # A cue holding no text (a pathological multi-line label) has no word
        # to take a boundary from.
        if not group or not group[0].tokens:
            return None
        first = group[0].tokens[0]
        last = group[-1].tokens[-1]
        intervals.append((spans[first][0], spans[last][1]))
    previous = segment.start
    for start, end in intervals:
        if end <= start or start < previous:
            return None
        previous = end
    return intervals


def _word_spans(segment: Segment, tokens: list[_Token]) -> list[tuple[float, float]] | None:
    """Per-token word interval, or None if the timings fail any check."""
    words = segment.words
    if not words or len(words) != len(tokens):
        return None
    spans: list[tuple[float, float]] = []
    previous_end = segment.start - WORD_BOUNDS_TOLERANCE
    for token, word in zip(tokens, words):
        if not (math.isfinite(word.start) and math.isfinite(word.end)):
            return None
        if word.end <= word.start or word.start < previous_end:
            return None
        if word.start < segment.start - WORD_BOUNDS_TOLERANCE:
            return None
        if word.end > segment.end + WORD_BOUNDS_TOLERANCE:
            return None
        if token.text != " ".join(word.text.split()):
            return None
        # Clamped so a cue never claims time outside the segment it came from.
        spans.append((max(word.start, segment.start), min(word.end, segment.end)))
        previous_end = word.end
    return spans


def _estimated_times(
    segment: Segment,
    tokens: list[_Token],
    groups: list[list[_Line]],
    total_chars: int,
) -> list[tuple[float, float]]:
    """Segment-time cue intervals, proportional to each cue's character span.

    An estimate, not a measurement: it assumes speech fills the segment at a
    constant rate, which is why it is only used when no trustworthy word
    timing exists. The final pass keeps the intervals ordered, non-overlapping
    and longer than zero even for a segment whose own interval is unusable.
    """
    span = max(segment.end - segment.start, 0.0)

    def at(char_offset: int) -> float:
        return segment.start + span * (char_offset / total_chars)

    raw: list[tuple[float, float]] = []
    for group in groups:
        shown = [index for line in group for index in line.tokens]
        if shown:
            raw.append((at(tokens[shown[0]].c0), at(tokens[shown[-1]].c1)))
        else:
            raw.append((segment.start, segment.end))

    out: list[tuple[float, float]] = []
    previous = segment.start
    for start, end in raw:
        start = max(start, previous)
        end = max(end, start + MIN_CUE_SECONDS)
        out.append((start, end))
        previous = end
    return out
