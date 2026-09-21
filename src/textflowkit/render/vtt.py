"""WebVTT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import vtt_timestamp


def render_vtt(transcript: Transcript, *, include_translation: bool = True) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for seg in transcript.segments:
        if seg.hidden:
            continue
        body = seg.display_text().strip() if include_translation else seg.text.strip()
        if seg.speaker:
            body = f"<v {seg.speaker}>{body}"
        lines.append(f"{vtt_timestamp(seg.start)} --> {vtt_timestamp(seg.end)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)
