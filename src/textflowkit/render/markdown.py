"""Markdown renderer - readable transcript with time anchors."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp


def _hms(seconds: float) -> str:
    return srt_timestamp(seconds).split(",")[0]


def render_markdown(transcript: Transcript, *, title: str | None = None) -> str:
    heading = title or "Transcript"
    out: list[str] = [f"# {heading}", ""]
    if transcript.source:
        out.append(f"**Source:** {transcript.source}")
    if transcript.language:
        out.append(f"**Language:** {transcript.language}")
    if transcript.duration:
        out.append(f"**Duration:** {_hms(transcript.duration)}")
    out += ["", "---", ""]

    current_speaker: str | None = None
    for seg in transcript.segments:
        if seg.hidden:
            continue
        if seg.speaker and seg.speaker != current_speaker:
            current_speaker = seg.speaker
            out += [f"**{current_speaker}**", ""]
        out.append(f"`{_hms(seg.start)}` {seg.display_text().strip()}")
        out.append("")
    return "\n".join(out)
