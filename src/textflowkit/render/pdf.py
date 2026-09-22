"""PDF renderer.

Binary output, so it does not go through `render()` - see `render_bytes()`.

Like the DOCX renderer this emits the finished data model: speaker labels and
translated text included.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from xml.sax.saxutils import escape

from textflowkit.core.model import Transcript
from textflowkit.core.timeutil import srt_timestamp

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except ImportError as exc:  # pragma: no cover - optional extra
    raise ImportError(
        "PDF export requires the 'export' extra. Install with: pip install 'textflowkit[export]'"
    ) from exc


def _hms(seconds: float) -> str:
    return srt_timestamp(seconds).split(",")[0]


_FONT_LOCK = threading.Lock()
_FONT_DIR = Path(__file__).parent / "fonts"


def _ensure_fonts() -> None:
    with _FONT_LOCK:
        for name in ("NotoSans", "NotoSansArabic", "NotoSansSC"):
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / f"{name}.ttf")))
            pdfmetrics.registerFontFamily(
                name, normal=name, bold=name, italic=name, boldItalic=name
            )


def _font_for(char: str) -> str:
    code = ord(char)
    if (0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF):
        return "NotoSansArabic"
    if (0x3000 <= code <= 0x303F or 0x3400 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF):
        return "NotoSansSC"
    return "NotoSans"


def _unicode_markup(text: str) -> str:
    """Escape text and select embedded fonts for CJK and Arabic runs."""
    if not text:
        return ""
    chunks: list[str] = []
    current = _font_for(text[0])
    run: list[str] = []
    for char in text:
        font = _font_for(char)
        if font != current:
            chunks.append(f'<font name="{current}">{escape("".join(run))}</font>')
            run = []
            current = font
        run.append(char)
    chunks.append(f'<font name="{current}">{escape("".join(run))}</font>')
    return "".join(chunks)


def render_pdf(transcript: Transcript, *, title: str = "Transcript") -> bytes:
    """Render a transcript as a PDF."""
    _ensure_fonts()
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
    for name in ("Title", "Normal", "Heading3", "BodyText"):
        styles[name].fontName = "NotoSans"
        styles[name].wordWrap = "LTR"
        styles[name].shaping = 1
    story = [Paragraph(_unicode_markup(title), styles["Title"]), Spacer(1, 10)]

    if transcript.source:
        story.append(Paragraph(f"<b>Source:</b> {_unicode_markup(transcript.source)}", styles["Normal"]))
    if transcript.language:
        story.append(Paragraph(f"<b>Language:</b> {_unicode_markup(transcript.language)}", styles["Normal"]))
    if transcript.duration:
        story.append(Paragraph(f"<b>Duration:</b> {_hms(transcript.duration)}", styles["Normal"]))

    diar = (transcript.metadata or {}).get("diarization")
    if diar:
        speakers = ", ".join(diar.get("speakers") or []) or "none"
        story.append(Paragraph(f"<b>Speakers:</b> {_unicode_markup(speakers)}", styles["Normal"]))
    trans = (transcript.metadata or {}).get("translation")
    if trans:
        story.append(
            Paragraph(f"<b>Translated to:</b> {_unicode_markup(str(trans.get('target')))}", styles["Normal"])
        )

    story.append(Spacer(1, 16))

    current_speaker: str | None = None
    for segment in transcript.segments:
        if segment.hidden:
            continue
        if segment.speaker and segment.speaker != current_speaker:
            current_speaker = segment.speaker
            story.append(Paragraph(f"<b>{_unicode_markup(current_speaker)}</b>", styles["Heading3"]))

        stamp = f"[{_hms(segment.start)}] "
        if segment.translated_text:
            body = f"{_unicode_markup(segment.translated_text.strip())}<br/><i>(original: {_unicode_markup(segment.text.strip())})</i>"
        else:
            body = _unicode_markup(segment.text.strip())
        story.append(Paragraph(f"<b>{stamp}</b>{body}", styles["BodyText"]))
        story.append(Spacer(1, 4))

    doc.build(story)
    return buffer.getvalue()
