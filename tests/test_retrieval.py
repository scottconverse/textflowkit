"""Transcript retrieval: paging, time range, and search.

A long transcript returned whole is a context problem, so these are the
guarantees that make slicing safe: offset/limit are relative to the selected
range, boundaries are inclusive, and has_more tells the caller whether to
continue rather than silently truncating.
"""

from __future__ import annotations

import pytest

from textflowkit.core.model import Segment, Transcript
from textflowkit.core.retrieval import page_segments, search_segments


def make(n: int = 10) -> Transcript:
    return Transcript(
        source="src",
        language="en",
        segments=[
            Segment(start=float(i), end=float(i) + 0.5, text=f"word{i} filler")
            for i in range(n)
        ],
    )


# --- paging ---------------------------------------------------------------

def test_page_no_args_returns_everything():
    page = page_segments(make(5))
    assert page.total == 5
    assert page.returned == 5
    assert page.has_more is False
    assert page.offset == 0


def test_page_limit():
    page = page_segments(make(10), limit=3)
    assert page.returned == 3
    assert page.total == 10
    assert page.has_more is True
    assert page.segments[0].text.startswith("word0")


def test_page_offset():
    page = page_segments(make(10), offset=7, limit=5)
    assert page.returned == 3               # only 3 left
    assert page.has_more is False
    assert page.segments[0].text.startswith("word7")


def test_page_offset_beyond_end_is_empty():
    page = page_segments(make(5), offset=99)
    assert page.returned == 0
    assert page.has_more is False


def test_page_exact_boundary_has_no_more():
    page = page_segments(make(6), limit=6)
    assert page.returned == 6
    assert page.has_more is False


def test_page_reports_time_span_of_window():
    page = page_segments(make(10), offset=2, limit=3)
    assert page.start == 2.0
    assert page.end == 4.5


def test_page_negative_offset_rejected():
    with pytest.raises(ValueError):
        page_segments(make(3), offset=-1)


def test_page_negative_limit_rejected():
    with pytest.raises(ValueError):
        page_segments(make(3), limit=-1)


def test_page_zero_limit_returns_nothing_but_reports_total():
    page = page_segments(make(4), limit=0)
    assert page.returned == 0
    assert page.total == 4
    assert page.has_more is True


# --- time range -----------------------------------------------------------

def test_time_range_filters():
    page = page_segments(make(10), start=3.0, end=5.0)
    texts = [s.text.split()[0] for s in page.segments]
    assert texts == ["word3", "word4", "word5"]


def test_time_range_offset_is_relative_to_the_range():
    """offset counts from the start of the window, not the transcript."""
    page = page_segments(make(10), start=3.0, offset=1, limit=1)
    assert page.returned == 1
    assert page.segments[0].text.startswith("word4")
    assert page.total == 7      # segments 3..9


def test_time_range_boundaries_are_inclusive():
    page = page_segments(make(10), start=4.0, end=4.0)
    assert [s.text.split()[0] for s in page.segments] == ["word4"]


def test_hidden_segments_are_not_paged():
    tr = make(4)
    tr.segments[1].hidden = True
    page = page_segments(tr)
    assert page.total == 3
    assert all(not s.hidden for s in page.segments)


def test_page_as_dict_shape():
    d = page_segments(make(3), limit=2).as_dict()
    for key in ("total_segments", "offset", "returned", "has_more", "start", "end"):
        assert key in d


# --- search ---------------------------------------------------------------

def test_search_finds_substring():
    matches = search_segments(make(10), "word3")
    assert len(matches) == 1
    assert matches[0].segment.text.startswith("word3")
    assert matches[0].index == 3


def test_search_is_case_insensitive_by_default():
    tr = Transcript(source="s", segments=[Segment(0, 1, "Hello WORLD")])
    assert len(search_segments(tr, "hello world")) == 1


def test_search_case_sensitive_option():
    tr = Transcript(source="s", segments=[Segment(0, 1, "Hello World")])
    assert search_segments(tr, "hello", case_sensitive=True) == []
    assert len(search_segments(tr, "Hello", case_sensitive=True)) == 1


def test_search_respects_limit():
    tr = Transcript(source="s", segments=[Segment(i, i + 1, "repeat me") for i in range(9)])
    assert len(search_segments(tr, "repeat", limit=4)) == 4


def test_search_zero_limit_returns_no_matches():
    assert search_segments(make(3), "word", limit=0) == []


def test_search_returns_context():
    matches = search_segments(make(10), "word5", context=2)
    m = matches[0]
    assert len(m.context_before) == 2
    assert len(m.context_after) == 2
    assert m.context_before[-1].text.startswith("word4")
    assert m.context_after[0].text.startswith("word6")


def test_search_context_clamped_at_edges():
    m = search_segments(make(10), "word0", context=3)[0]
    assert m.context_before == []
    assert len(m.context_after) == 3


def test_search_matches_translated_text():
    tr = Transcript(
        source="s",
        segments=[Segment(0, 1, "hola", translated_text="hello there")],
    )
    assert len(search_segments(tr, "hello")) == 1


def test_search_skips_hidden_segments():
    tr = make(4)
    tr.segments[1].hidden = True
    assert search_segments(tr, "word1") == []


def test_search_empty_query_rejected():
    with pytest.raises(ValueError):
        search_segments(make(3), "")


def test_search_negative_limit_rejected():
    with pytest.raises(ValueError):
        search_segments(make(3), "word", limit=-1)


def test_search_negative_context_rejected():
    with pytest.raises(ValueError):
        search_segments(make(3), "word", context=-1)


def test_search_no_matches():
    assert search_segments(make(3), "zzz") == []
