"""SRT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp


def render_srt(transcript: Transcript, *, include_translation: bool = True) -> str:
    blocks: list[str] = []
    index = 0
    for seg in transcript.segments:
        if seg.hidden:
            continue
        index += 1
        body = seg.display_text().strip() if include_translation else seg.text.strip()
        if seg.speaker:
            body = f"{seg.speaker}: {body}"
        blocks.append(
            f"{index}\n{srt_timestamp(seg.start)} --> {srt_timestamp(seg.end)}\n{body}\n"
        )
    return "\n".join(blocks)
