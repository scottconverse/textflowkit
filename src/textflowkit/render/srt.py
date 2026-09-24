"""SRT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp
from textflowkit.render._cue_layout import layout_cues
from textflowkit.render._cue_text import neutralize_arrow


def render_srt(transcript: Transcript, *, include_translation: bool = True) -> str:
    blocks: list[str] = []
    index = 0
    for seg in transcript.segments:
        if seg.hidden:
            continue
        # A long segment becomes several cues at word boundaries; the label
        # rides along as visible text because SRT has no markup for a speaker.
        for cue in layout_cues(
            seg, include_translation=include_translation, inline_speaker=True
        ):
            index += 1
            # SRT has no escape mechanism, so the payload keeps its characters
            # and only the two things a parser reads as structure are taken
            # out: a blank line would end the cue, and "-->" would look like a
            # timing line. The layout never emits a blank line.
            body = "\n".join(neutralize_arrow(line) for line in cue.lines)
            blocks.append(
                f"{index}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{body}\n"
            )
    return "\n".join(blocks)
