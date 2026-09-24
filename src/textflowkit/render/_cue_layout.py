"""Shared cue layout for the time-coded subtitle renderers.

SRT and WebVTT differ in how a cue's text is escaped, not in where a cue
begins and ends. Both renderers call `layout_cues`, so one transcript cannot
come out with different splits or different cue times in the two formats.

Layout runs on *visible* text - what a viewer reads, before SRT's arrow
stand-in or WebVTT's character references - because the target below is a
readability budget, not a byte count.

RED STEP: this module currently reproduces the pre-C1 behaviour (one cue per
segment, the segment's own times) so the new tests fail against the defect
they were written for instead of against a missing import.
"""

from __future__ import annotations

from dataclasses import dataclass

from textflowkit.core.model import Segment

# Readability targets, not hard guarantees: a single word longer than the line
# target still gets its own line rather than being split mid-word.
MAX_LINE_CHARS = 42
MAX_LINES_PER_CUE = 2


@dataclass(frozen=True, slots=True)
class Cue:
    """One subtitle cue: an interval and the lines shown during it."""

    start: float
    end: float
    lines: tuple[str, ...]


def layout_cues(
    segment: Segment,
    *,
    include_translation: bool = True,
    inline_speaker: bool = False,
) -> list[Cue]:
    """Lay one segment out as cues. Pre-C1 behaviour: exactly one cue."""
    text = segment.display_text() if include_translation else segment.text
    body = text.strip()
    if inline_speaker and segment.speaker:
        body = f"{segment.speaker}: {body}"
    lines = (line.strip() for line in body.splitlines())
    kept = tuple(line for line in lines if line) or ((body,) if body else ())
    return [Cue(start=segment.start, end=segment.end, lines=kept)]
