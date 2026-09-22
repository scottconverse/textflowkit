"""Renderer layer: canonical transcript -> output formats.

Two paths, because two kinds of output:

- `render()` returns **text** (txt, srt, vtt, md, json) and is what the MCP
  `get_transcript` tool returns inline.
- `render_bytes()` returns **bytes** and additionally handles the binary formats
  (docx, pdf), which exist for file export rather than for reading in a model's
  context.

`SUPPORTED_FORMATS` is the union and is what callers should validate against.
`TEXT_FORMATS` is what can be returned as a string.
"""

from __future__ import annotations

from pathlib import Path

from textflowkit.core.model import Transcript
from textflowkit.render.markdown import render_markdown
from textflowkit.render.srt import render_srt
from textflowkit.render.txt import render_txt
from textflowkit.render.vtt import render_vtt

RENDERERS = {
    "txt": render_txt,
    "srt": render_srt,
    "vtt": render_vtt,
    "md": render_markdown,
}

TEXT_FORMATS = tuple(RENDERERS) + ("json",)
BINARY_FORMATS = ("docx", "pdf")
SUPPORTED_FORMATS = TEXT_FORMATS + BINARY_FORMATS


def render(transcript: Transcript, fmt: str, *, title: str | None = None) -> str:
    """Render to text. Raises for a binary format - use `render_bytes`."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "json":
        return transcript.to_json()
    if fmt in BINARY_FORMATS:
        raise ValueError(
            f"'{fmt}' is a binary format; use render_bytes() (and export to a file)"
        )
    if fmt not in RENDERERS:
        raise ValueError(f"unsupported format: {fmt} (choose from {', '.join(SUPPORTED_FORMATS)})")
    if fmt == "md":
        return render_markdown(transcript, title=title)
    return RENDERERS[fmt](transcript)


def render_bytes(transcript: Transcript, fmt: str, *, title: str | None = None) -> bytes:
    """Render to bytes, for any supported format including binary ones."""
    fmt = fmt.lower().lstrip(".")

    if fmt == "docx":
        from textflowkit.render.docx import render_docx

        return render_docx(transcript, title=title or "Transcript")
    if fmt == "pdf":
        from textflowkit.render.pdf import render_pdf

        return render_pdf(transcript, title=title or "Transcript")
    if fmt not in TEXT_FORMATS:
        raise ValueError(f"unsupported format: {fmt} (choose from {', '.join(SUPPORTED_FORMATS)})")
    return render(transcript, fmt, title=title).encode("utf-8")


def write_all(
    transcript: Transcript,
    *,
    formats: list[str],
    output_dir: str | Path,
    stem: str,
    title: str | None = None,
) -> list[Path]:
    """Write each requested format to `output_dir`."""
    from textflowkit.core.paths import ensure_output_dir

    out_dir = ensure_output_dir(str(output_dir))
    written: list[Path] = []
    for fmt in formats:
        norm = fmt.lower().lstrip(".")
        path = out_dir / f"{stem}.{norm}"
        path.write_bytes(render_bytes(transcript, norm, title=title))
        written.append(path)
    return written


__all__ = [
    "BINARY_FORMATS",
    "RENDERERS",
    "SUPPORTED_FORMATS",
    "TEXT_FORMATS",
    "render",
    "render_bytes",
    "render_markdown",
    "render_srt",
    "render_txt",
    "render_vtt",
    "write_all",
]
