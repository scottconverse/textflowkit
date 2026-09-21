"""Plain-text renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript


def render_txt(transcript: Transcript, *, timestamps: bool = False, speaker: bool = False) -> str:
    lines: list[str] = []
    for seg in transcript.segments:
        if seg.hidden:
            continue
        prefix = ""
        if timestamps:
            prefix = f"[{seg.start:8.2f}] "
        if speaker and seg.speaker:
            prefix += f"{seg.speaker}: "
        lines.append(prefix + seg.display_text().strip())
    return "\n".join(lines) + ("\n" if lines else "")
