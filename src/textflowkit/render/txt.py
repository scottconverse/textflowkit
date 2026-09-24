"""Plain-text renderer."""

from __future__ import annotations

from textflowkit.core.model import Transcript


def render_txt(transcript: Transcript, *, timestamps: bool = False, speaker: bool = True) -> str:
    """Plain text, one line per visible segment.

    Speaker labels are included by default: a diarized transcript exports its
    speakers here the same way srt, vtt, md, docx and pdf already do, so the
    plain-text export does not silently lose information the other readable
    formats keep. Only segments that actually carry a speaker are labelled, so
    an unlabelled transcript renders exactly as it did before. Callers that
    want the labels gone pass `speaker=False`.
    """
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
