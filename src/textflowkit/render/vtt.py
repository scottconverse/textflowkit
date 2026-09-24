"""WebVTT renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import vtt_timestamp
from textflowkit.render._cue_text import (
    escape_vtt_annotation,
    escape_vtt_text,
    normalize_cue_lines,
)


def render_vtt(transcript: Transcript, *, include_translation: bool = True) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for seg in transcript.segments:
        if seg.hidden:
            continue
        body = seg.display_text().strip() if include_translation else seg.text.strip()
        # Cue text is parsed WebVTT, so the payload is escaped before it goes
        # in; escaping ">" is also what keeps a literal "-->" out of it. The
        # "<v ...>" span below is the format's own markup, added after the
        # fact so it is not escaped, with its annotation sanitized separately.
        body = escape_vtt_text(normalize_cue_lines(body))
        if seg.speaker:
            annotation = escape_vtt_annotation(seg.speaker)
            if annotation:
                body = f"<v {annotation}>{body}"
        lines.append(f"{vtt_timestamp(seg.start)} --> {vtt_timestamp(seg.end)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)
