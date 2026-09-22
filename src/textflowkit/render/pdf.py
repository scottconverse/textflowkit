"""PDF renderer.

Binary output, so it does not go through `render()` - see `render_bytes()`.

Like the DOCX renderer this emits the finished data model: speaker labels and
translated text included.
"""

from __future__ import annotations

import io

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except ImportError as exc:  # pragma: no cover - optional extra
    raise ImportError(
        "PDF export requires the 'export' extra. Install with: pip install 'textflowkit[export]'"
    ) from exc


def _hms(seconds: float) -> str:
    return srt_timestamp(seconds).split(",")[0]


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render_pdf(transcript: Transcript, *, title: str = "Transcript") -> bytes:
    """Render a transcript as a PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        title=title,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(_escape(title), styles["Title"]), Spacer(1, 10)]

    if transcript.source:
        story.append(Paragraph(f"<b>Source:</b> {_escape(transcript.source)}", styles["Normal"]))
    if transcript.language:
        story.append(Paragraph(f"<b>Language:</b> {_escape(transcript.language)}", styles["Normal"]))
    if transcript.duration:
        story.append(Paragraph(f"<b>Duration:</b> {_hms(transcript.duration)}", styles["Normal"]))

    diar = (transcript.metadata or {}).get("diarization")
    if diar:
        speakers = ", ".join(diar.get("speakers") or []) or "none"
        story.append(Paragraph(f"<b>Speakers:</b> {_escape(speakers)}", styles["Normal"]))
    trans = (transcript.metadata or {}).get("translation")
    if trans:
        story.append(
            Paragraph(f"<b>Translated to:</b> {_escape(str(trans.get('target')))}", styles["Normal"])
        )

    story.append(Spacer(1, 16))

    current_speaker: str | None = None
    for segment in transcript.segments:
        if segment.hidden:
            continue
        if segment.speaker and segment.speaker != current_speaker:
            current_speaker = segment.speaker
            story.append(Paragraph(f"<b>{_escape(current_speaker)}</b>", styles["Heading3"]))

        stamp = f"[{_hms(segment.start)}] "
        if segment.translated_text:
            body = f"{_escape(segment.translated_text.strip())}<br/><i>(original: {_escape(segment.text.strip())})</i>"
        else:
            body = _escape(segment.text.strip())
        story.append(Paragraph(f"<b>{stamp}</b>{body}", styles["BodyText"]))
        story.append(Spacer(1, 4))

    doc.build(story)
    return buffer.getvalue()
