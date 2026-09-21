"""Renderer layer: canonical transcript -> output formats."""

from __future__ import annotations

from pathlib import Path

from textflowkit.core.model import Transcript
from textflowkit.core.paths import ensure_output_dir
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

SUPPORTED_FORMATS = tuple(RENDERERS) + ("json",)


def render(transcript: Transcript, fmt: str, *, title: str | None = None) -> str:
    fmt = fmt.lower().lstrip(".")
    if fmt == "json":
        return transcript.to_json()
    if fmt not in RENDERERS:
        raise ValueError(f"unsupported format: {fmt} (choose from {', '.join(SUPPORTED_FORMATS)})")
    if fmt == "md":
        return render_markdown(transcript, title=title)
    return RENDERERS[fmt](transcript)


def write_all(
    transcript: Transcript,
    *,
    formats: list[str],
    output_dir: str | Path,
    stem: str,
    title: str | None = None,
) -> list[Path]:
    out_dir = ensure_output_dir(str(output_dir))
    written: list[Path] = []
    for fmt in formats:
        content = render(transcript, fmt, title=title)
        path = out_dir / f"{stem}.{fmt}"
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


__all__ = [
    "RENDERERS",
    "SUPPORTED_FORMATS",
    "render",
    "render_markdown",
    "render_srt",
    "render_txt",
    "render_vtt",
    "write_all",
]

