"""SRT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp
from textflowkit.render._cue_text import neutralize_arrow, normalize_cue_lines


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
        # SRT has no escape mechanism, so the payload keeps its characters and
        # only the two things a parser reads as structure are taken out: a
        # blank line would end the cue, and "-->" would look like a timing line.
        body = neutralize_arrow(normalize_cue_lines(body))
        blocks.append(
            f"{index}\n{srt_timestamp(seg.start)} --> {srt_timestamp(seg.end)}\n{body}\n"
        )
    return "\n".join(blocks)
