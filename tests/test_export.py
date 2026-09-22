"""DOCX and PDF export.

These are built last on purpose: both render the *finished* data model, so these
tests check that speaker labels and translated text actually reach the output -
the reason the formats were not built earlier.

The artifacts are validated, not merely produced: the DOCX is unzipped and its
document XML inspected, and the PDF's text is extracted and matched. A file that
merely exists proves nothing.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from textflowkit.core.model import Segment, Transcript
from textflowkit.render import BINARY_FORMATS, render, render_bytes, write_all

docx = pytest.importorskip("docx", reason="DOCX export needs the 'export' extra")
pytest.importorskip("reportlab", reason="PDF export needs the 'export' extra")


def rich_transcript() -> Transcript:
    """A transcript carrying every field the exporters must render."""
    return Transcript(
        source="https://example.com/talk",
        language="en",
        duration=125.0,
        metadata={
            "diarization": {"speakers": ["SPEAKER_00", "SPEAKER_01"]},
            "translation": {"target": "Spanish", "segments_translated": 2},
        },
        segments=[
            Segment(0.0, 4.0, "Hello there.", speaker="SPEAKER_00",
                    translated_text="Hola."),
            Segment(4.0, 9.0, "General Kenobi.", speaker="SPEAKER_01",
                    translated_text="General Kenobi."),
            Segment(9.0, 12.0, "hidden note", hidden=True),
        ],
    )


# --- DOCX -----------------------------------------------------------------

def _docx_text(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def test_docx_is_a_valid_zip_container():
    blob = render_bytes(rich_transcript(), "docx")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.testzip() is None
        assert "word/document.xml" in zf.namelist()


def test_docx_contains_translated_text_and_original():
    xml = _docx_text(render_bytes(rich_transcript(), "docx"))
    assert "Hola." in xml
    assert "original" in xml          # source kept alongside the translation
    assert "Hello there." in xml


def test_docx_contains_speaker_labels():
    xml = _docx_text(render_bytes(rich_transcript(), "docx"))
    assert "SPEAKER_00" in xml
    assert "SPEAKER_01" in xml


def test_docx_includes_metadata():
    xml = _docx_text(render_bytes(rich_transcript(), "docx"))
    assert "example.com" in xml
    assert "Spanish" in xml


def test_docx_omits_hidden_segments():
    xml = _docx_text(render_bytes(rich_transcript(), "docx"))
    assert "hidden note" not in xml


def test_docx_title_is_used():
    xml = _docx_text(render_bytes(rich_transcript(), "docx", title="My Talk"))
    assert "My Talk" in xml


# --- PDF ------------------------------------------------------------------

def _pdf_text(blob: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_pdf_has_pdf_header_and_pages():
    blob = render_bytes(rich_transcript(), "pdf")
    assert blob.startswith(b"%PDF-")
    from pypdf import PdfReader

    assert len(PdfReader(io.BytesIO(blob)).pages) >= 1


def test_pdf_contains_text():
    text = _pdf_text(render_bytes(rich_transcript(), "pdf"))
    assert "Hello there." in text
    assert "General Kenobi." in text


def test_pdf_contains_translation_and_original():
    text = _pdf_text(render_bytes(rich_transcript(), "pdf"))
    assert "Hola." in text
    assert "original" in text


def test_pdf_contains_speakers():
    text = _pdf_text(render_bytes(rich_transcript(), "pdf"))
    assert "SPEAKER_00" in text
    assert "SPEAKER_01" in text


def test_pdf_omits_hidden_segments():
    text = _pdf_text(render_bytes(rich_transcript(), "pdf"))
    assert "hidden note" not in text


def test_pdf_embeds_fonts_and_preserves_multilingual_text():
    phrase = "你好 Привет مرحبا café"
    tr = Transcript(source="local", segments=[Segment(0, 1, phrase)])
    blob = render_bytes(tr, "pdf")
    extracted = _pdf_text(blob)
    for word in ("你好", "Привет", "café"):
        assert word in extracted
    # PDF text extraction reports RTL runs in visual order on some readers;
    # every Arabic codepoint must survive even when word order is reversed.
    assert set("مرحبا").issubset(set(extracted))
    from pypdf import PdfReader

    fonts = PdfReader(io.BytesIO(blob)).pages[0]["/Resources"]["/Font"].get_object()
    names = [font.get_object().get("/BaseFont", "") for font in fonts.values()]
    assert all(any(font in str(name) for name in names)
               for font in ("NotoSans", "NotoSansArabic", "NotoSansSC"))


# --- integration with the render layer ------------------------------------

def test_render_refuses_binary_with_a_useful_message():
    with pytest.raises(ValueError) as exc:
        render(rich_transcript(), "docx")
    assert "binary" in str(exc.value)
    assert "render_bytes" in str(exc.value)


def test_render_bytes_handles_text_formats_too():
    assert render_bytes(rich_transcript(), "txt").decode("utf-8")
    assert render_bytes(rich_transcript(), "json").decode("utf-8").startswith("{")


def test_render_bytes_rejects_unknown():
    with pytest.raises(ValueError):
        render_bytes(rich_transcript(), "xyzzy")


def test_write_all_writes_binary_correctly(tmp_path, monkeypatch):
    """write_all must not corrupt binary output by treating it as text."""
    from textflowkit.core.paths import ENV_OUTPUT_ROOT

    monkeypatch.setenv(ENV_OUTPUT_ROOT, str(tmp_path))
    paths = write_all(
        rich_transcript(),
        formats=["docx", "pdf", "srt"],
        output_dir=str(tmp_path),
        stem="talk",
    )
    by_name = {p.name: p for p in paths}
    assert set(by_name) == {"talk.docx", "talk.pdf", "talk.srt"}

    with zipfile.ZipFile(by_name["talk.docx"]) as zf:      # valid DOCX
        assert zf.testzip() is None
    assert by_name["talk.pdf"].read_bytes().startswith(b"%PDF-")

    # The subtitle renderer shows the *translated* text when a translation
    # exists (display_text() prefers it), so the SRT carries "Hola." here and
    # not the source line. That is the intended behaviour.
    srt = by_name["talk.srt"].read_text(encoding="utf-8")
    assert "Hola." in srt
    assert "SPEAKER_00" in srt


def test_binary_formats_constant():
    assert BINARY_FORMATS == ("docx", "pdf")


@pytest.mark.parametrize("fmt,header", [("pdf", b"%PDF-"), ("docx", b"PK")])
def test_cli_binary_export_writes_valid_file(tmp_path, fmt, header):
    from textflowkit import cli

    transcript_path = tmp_path / "talk.json"
    rich_transcript().save_json(transcript_path)
    output = tmp_path / f"talk.{fmt}"
    assert cli.main(["export", str(transcript_path), "--format", fmt,
                     "--output", str(output)]) == 0
    assert output.read_bytes().startswith(header)


def test_cli_binary_export_requires_output_path(tmp_path, capsys):
    from textflowkit import cli

    transcript_path = tmp_path / "talk.json"
    rich_transcript().save_json(transcript_path)
    assert cli.main(["export", str(transcript_path), "--format", "pdf"]) == 1
    assert "--output is required" in capsys.readouterr().err
