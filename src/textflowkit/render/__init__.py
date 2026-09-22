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

import os
import tempfile
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


def _render_requested(
    transcript: Transcript, formats: list[str], title: str | None
) -> list[tuple[str, bytes]]:
    """Validate and render every format before touching any destination file."""
    rendered: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for fmt in formats:
        norm = fmt.lower().lstrip(".")
        if norm not in SUPPORTED_FORMATS:
            raise ValueError(f"unsupported format: {fmt}")
        if norm in seen:
            raise ValueError(f"duplicate output format: {fmt}")
        seen.add(norm)
        rendered.append((norm, render_bytes(transcript, norm, title=title)))
    return rendered


def _atomic_write_new(path: Path, data: bytes) -> None:
    """Publish a complete file without ever replacing an existing destination."""
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link commits the fully-written temp file atomically and fails
        # if the destination already exists, unlike os.replace().
        os.link(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


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


def ensure_outputs(
    transcript: Transcript,
    *,
    formats: list[str],
    output_dir: str | Path | None,
    stem: str,
    existing: list[str | Path] | None = None,
    title: str | None = None,
) -> list[Path]:
    """Write requested formats, reusing already-present outputs when possible.

    Resume must not redo transcription. Rendering missing files is cheap and
    keeps the checkpoint contract true even when the original output directory
    was removed between runs.
    """
    if output_dir is None:
        return []
    from textflowkit.core.paths import ensure_output_dir

    out_dir = ensure_output_dir(str(output_dir))
    rendered = _render_requested(transcript, formats, title)
    by_suffix: dict[str, Path] = {}
    for raw in existing or []:
        path = Path(raw)
        if path.parent.resolve() == out_dir.resolve():
            by_suffix[path.suffix.lower().lstrip(".")] = path
    written: list[Path] = []
    for norm, data in rendered:
        prior = by_suffix.get(norm)
        if prior is not None and prior.exists():
            written.append(prior)
            continue
        path = out_dir / f"{stem}.{norm}"
        _atomic_write_new(path, data)
        written.append(path)
    return written


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
    rendered = _render_requested(transcript, formats, title)
    written: list[Path] = []
    for norm, data in rendered:
        path = out_dir / f"{stem}.{norm}"
        _atomic_write_new(path, data)
        written.append(path)
    return written


__all__ = [
    "BINARY_FORMATS",
    "RENDERERS",
    "SUPPORTED_FORMATS",
    "TEXT_FORMATS",
    "ensure_outputs",
    "render",
    "render_bytes",
    "render_markdown",
    "render_srt",
    "render_txt",
    "render_vtt",
    "write_all",
]
