"""Transcript retrieval: paging, time ranges, and search.

A long transcript returned whole is a context problem. A 19-minute video is
already ~51 KB of JSON (~214 segments); a 3-hour podcast is several hundred KB
dropped into a model's context in a single tool result, most of it irrelevant to
the question being asked.

This module does the slicing in one place so the MCP and HTTP adapters cannot
disagree about what `offset` or `start` mean - the same reasoning that put the
pipeline in core rather than in each adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textflowkit.core.model import Segment, Transcript


@dataclass(slots=True)
class Page:
    """A window onto a transcript's segments."""

    segments: list[Segment]
    total: int
    offset: int
    returned: int
    has_more: bool
    start: float
    end: float

    def as_dict(self) -> dict:
        return {
            "total_segments": self.total,
            "offset": self.offset,
            "returned": self.returned,
            "has_more": self.has_more,
            "start": self.start,
            "end": self.end,
        }


@dataclass(slots=True)
class Match:
    """A search hit, plus optional surrounding context."""

    index: int
    segment: Segment
    context_before: list[Segment] = field(default_factory=list)
    context_after: list[Segment] = field(default_factory=list)


def page_segments(
    transcript: Transcript,
    *,
    offset: int = 0,
    limit: int | None = None,
    start: float | None = None,
    end: float | None = None,
) -> Page:
    """Slice a transcript by time range and/or page offset.

    Filtering order is time first, then offset/limit within the filtered set, so
    `offset` counts from the start of the requested window rather than from the
    beginning of the transcript.

    Raises `ValueError` for a negative offset or limit.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    segments = [s for s in transcript.segments if not s.hidden]

    if start is not None:
        segments = [s for s in segments if s.end >= start]
    if end is not None:
        segments = [s for s in segments if s.start <= end]

    total = len(segments)
    window = segments[offset:] if offset else segments
    if limit is not None:
        window = window[:limit]

    first = window[0].start if window else 0.0
    last = window[-1].end if window else 0.0
    consumed = offset + len(window)

    return Page(
        segments=window,
        total=total,
        offset=offset,
        returned=len(window),
        has_more=consumed < total,
        start=first,
        end=last,
    )


def search_segments(
    transcript: Transcript,
    query: str,
    *,
    limit: int = 20,
    context: int = 0,
    case_sensitive: bool = False,
) -> list[Match]:
    """Find segments whose text contains `query`.

    `context` includes that many neighbouring segments either side, which is
    usually what makes a hit readable. Matching is substring, not fuzzy - the
    caller can ask again with a different phrase.

    Raises `ValueError` for an empty query or negative limit/context.
    """
    if not query:
        raise ValueError("query must not be empty")
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if context < 0:
        raise ValueError("context must be >= 0")

    needle = query if case_sensitive else query.lower()
    visible = [s for s in transcript.segments if not s.hidden]

    matches: list[Match] = []
    for idx, segment in enumerate(visible):
        hay = segment.text if case_sensitive else segment.text.lower()
        translated = segment.translated_text
        if translated:
            hay_translated = translated if case_sensitive else translated.lower()
        else:
            hay_translated = ""
        if needle in hay or (hay_translated and needle in hay_translated):
            matches.append(
                Match(
                    index=idx,
                    segment=segment,
                    context_before=visible[max(0, idx - context): idx] if context else [],
                    context_after=visible[idx + 1: idx + 1 + context] if context else [],
                )
            )
            if len(matches) >= limit:
                break
    return matches
