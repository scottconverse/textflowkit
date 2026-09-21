"""The shared pipeline.

Every platform, every caller, every adapter runs this same path:

    resolve source -> acquire media -> extract audio -> transcribe -> render

Platform differences live entirely in the source layer.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textflowkit.core.engine import get_engine
from textflowkit.core.model import Transcript
from textflowkit.core.paths import (
    UnsafeInputPathError,
    default_input_root,
    resolve_input_path,
)
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
    check_cancel: Callable[[], None] | None = None,
    input_root: str | Path | None = None,
) -> TranscribeResult:
    """Run the full pipeline for a URL or local file.

    `check_cancel` is an optional callback invoked at stage boundaries. It is
    expected to raise in order to abort the run. Cancellation is therefore
    cooperative: the stage boundaries are the checkpoints, so a job cannot be
    interrupted part-way through a single `fetch_media` or `engine.transcribe`
    call. That limit is deliberate and documented rather than hidden.
    """

    def _checkpoint() -> None:
        if check_cancel is not None:
            check_cancel()

    _checkpoint()

    formats = formats or ["json", "srt", "txt"]
    for fmt in formats:
        if fmt.lower().lstrip(".") not in SUPPORTED_FORMATS:
            raise PipelineError(
                f"unsupported format '{fmt}'; choose from {', '.join(SUPPORTED_FORMATS)}"
            )

    # A local path may be confined; a URL is guarded separately by the SSRF
    # check inside resolve_source. `input_root=None` means "use the configured
    # root if one is set", which keeps the CLI unconfined by default.
    if not source.startswith(("http://", "https://")):
        try:
            resolve_input_path(source, root=input_root if input_root is not None else default_input_root())
        except (FileNotFoundError, ValueError, UnsafeInputPathError) as exc:
            raise PipelineError(str(exc)) from exc

    try:
        ref = resolve_source(source)
    except (FileNotFoundError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc

    _checkpoint()

    scratch = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="textflowkit-"))
    scratch.mkdir(parents=True, exist_ok=True)

    # Fail fast on missing external tools.
    require_tool("ffmpeg")

    try:
        media = fetch_media(
            ref,
            work_dir=scratch,
            cookies_from_browser=cookies_from_browser,
            check_cancel=check_cancel,
        )
        _checkpoint()
        audio = extract_audio(media, work_dir=scratch)
        _checkpoint()
    except AcquisitionError as exc:
        raise PipelineError(str(exc)) from exc

    eng = get_engine(engine, model=model, device=device)
    try:
        transcript = eng.transcribe(audio, language=language)
    except Exception as exc:  # engine failures are user-facing
        raise PipelineError(f"transcription failed: {exc}") from exc

    _checkpoint()

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
