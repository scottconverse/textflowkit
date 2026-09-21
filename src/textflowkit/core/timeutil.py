"""Timestamp formatting for subtitle formats."""

from __future__ import annotations


def srt_timestamp(seconds: float) -> str:
    """HH:MM:SS,mmm (SRT uses a comma)."""
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def vtt_timestamp(seconds: float) -> str:
    """HH:MM:SS.mmm (WebVTT uses a period)."""
    return srt_timestamp(seconds).replace(",", ".")
