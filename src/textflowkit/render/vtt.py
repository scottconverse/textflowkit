"""WebVTT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import vtt_timestamp
from textflowkit.render._cue_layout import layout_cues
from textflowkit.render._cue_text import escape_vtt_annotation, escape_vtt_text


def render_vtt(transcript: Transcript, *, include_translation: bool = True) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for seg in transcript.segments:
        if seg.hidden:
            continue
        # The same layout SRT uses, so the two formats cannot split one
        # segment differently. Cue text is escaped after layout, where the
        # line width was measured, because WebVTT cue text is parsed: "&" and
        # "<" are markup syntax and escaping ">" is also what keeps a literal
        # "-->" out of the payload.
        #
        # The "<v ...>" span below is the format's own markup, added after the
        # fact so it is not escaped, with its annotation sanitized separately.
        # It is not visible text, so it is not part of the width budget; it is
        # repeated on every cue because each cue is a separate screen.
        annotation = escape_vtt_annotation(seg.speaker) if seg.speaker else ""
        for cue in layout_cues(seg, include_translation=include_translation):
            body = [escape_vtt_text(line) for line in cue.lines] or [""]
            if annotation:
                body[0] = f"<v {annotation}>{body[0]}"
            lines.append(f"{vtt_timestamp(cue.start)} --> {vtt_timestamp(cue.end)}")
            lines.append("\n".join(body))
            lines.append("")
    return "\n".join(lines)
