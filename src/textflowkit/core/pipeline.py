"""The shared pipeline.

Every platform, every caller, every adapter runs this same path:

    resolve source -> acquire media -> extract audio -> transcribe -> render

Platform differences live entirely in the source layer.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from textflowkit.core.engine import get_engine
from textflowkit.core.model import Transcript
from textflowkit.render import SUPPORTED_FORMATS, write_all
from textflowkit.sources.acquire import AcquisitionError, extract_audio, fetch_media, require_tool
from textflowkit.sources.detect import resolve_source


class PipelineError(RuntimeError):
    """Raised when any stage of the pipeline fails."""


@dataclass(slots=True)
class TranscribeResult:
    transcript: Transcript
    outputs: list[Path]


def transcribe(
    source: str,
    *,
    language: str | None = None,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    model: str = "small",
    engine: str = "whisper",
    device: str | None = None,
    cookies_from_browser: str | None = None,
    keep_media: bool = False,
    work_dir: str | Path | None = None,
) -> TranscribeResult:
    """Run the full pipeline for a URL or local file."""
    formats = formats or ["json", "srt", "txt"]
    for fmt in formats:
        if fmt.lower().lstrip(".") not in SUPPORTED_FORMATS:
            raise PipelineError(
                f"unsupported format '{fmt}'; choose from {', '.join(SUPPORTED_FORMATS)}"
            )

    try:
        ref = resolve_source(source)
    except (FileNotFoundError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc

    scratch = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="textflowkit-"))
    scratch.mkdir(parents=True, exist_ok=True)

    # Fail fast on missing external tools.
    require_tool("ffmpeg")

    try:
        media = fetch_media(ref, work_dir=scratch, cookies_from_browser=cookies_from_browser)
        audio = extract_audio(media, work_dir=scratch)
    except AcquisitionError as exc:
        raise PipelineError(str(exc)) from exc

    eng = get_engine(engine, model=model, device=device)
    try:
        transcript = eng.transcribe(audio, language=language)
    except Exception as exc:  # engine failures are user-facing
        raise PipelineError(f"transcription failed: {exc}") from exc

    transcript.source = source
    transcript.platform = ref.platform

    outputs: list[Path] = []
    if output_dir is not None:
        stem = Path(scratch.name if ref.kind == "url" else ref.location).stem
        stem = stem.replace("textflowkit-", "") or "transcript"
        outputs = write_all(transcript, formats=formats, output_dir=output_dir, stem=stem)

    if not keep_media:
        for f in (media, audio):
            try:
                if f.exists() and scratch in f.parents:
                    f.unlink()
            except OSError:
                pass

    return TranscribeResult(transcript=transcript, outputs=outputs)

