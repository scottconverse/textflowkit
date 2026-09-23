"""The shared pipeline.

Every platform, every caller, every adapter runs this same path:

    resolve source -> acquire media -> extract audio -> transcribe -> render

Platform differences live entirely in the source layer.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textflowkit.core.checkpoint import (
    CheckpointRecord,
    local_source_identity,
    parse_checkpoint,
    validate_local_resume,
)
from textflowkit.core.diarize import DiarizationError, assign_speakers, get_diarizer
from textflowkit.core.engine import get_engine
from textflowkit.core.model import Transcript
from textflowkit.core.paths import (
    UnsafeInputPathError,
    default_input_root,
    resolve_input_path,
)
from textflowkit.core.service import enforce_media_limits, enforce_predecode_limits
from textflowkit.core.translate import (
    TranslationError,
    get_translator,
    translate_segments,
)
from textflowkit.render import SUPPORTED_FORMATS, write_all
from textflowkit.sources.acquire import (
    AcquisitionError,
    extract_audio,
    fetch_media,
    require_tool,
    stage_confined_local_media,
)
from textflowkit.sources.detect import resolve_source


class PipelineError(RuntimeError):
    """Raised when any stage of the pipeline fails."""


def _cleanup_scratch(path: Path) -> None:
    """Remove a completed attempt, tolerating brief Windows decoder file locks.

    A failed cleanup must not be silently ignored: the scratch tree may contain
    downloaded media, so report a persistent failure to the caller.
    """
    for attempt in range(10):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == 9:
                raise PipelineError(f"scratch cleanup failed: {exc}") from exc
            time.sleep(0.1)


@dataclass(slots=True)
class TranscribeResult:
    transcript: Transcript
    outputs: list[Path]


def _existing_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def _resume_transcript(
    resumed: CheckpointRecord | None,
    *,
    source: str,
    ref,
) -> Transcript | None:
    if resumed is None or resumed.transcript is None:
        return None
    try:
        transcript = Transcript.from_dict(resumed.transcript)
    except (KeyError, TypeError, ValueError):
        return None
    transcript.source = source
    transcript.platform = ref.platform
    return transcript


def _checkpoint_paths_usable(resumed: CheckpointRecord | None) -> bool:
    if resumed is None or resumed.transcript is None:
        return False
    return "transcribe" in resumed.finished_stages


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
    diarize: bool = False,
    diarizer_backend: str = "pyannote",
    translate_to: str | None = None,
    translator_backend: str = "ollama",
    resume_checkpoint: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    output_id: str | None = None,
) -> TranscribeResult:
    """Run the full pipeline for a URL or local file.

    `check_cancel` is an optional callback invoked at stage boundaries and
    during yt-dlp progress and ffmpeg decode. It is expected to raise to abort
    the run. Model inference and optional postprocessors still only stop at
    their next stage boundary.

    `resume_checkpoint` carries a validated snapshot from an earlier run.
    Completed transcript work is reused; acquisition and engine work are skipped.
    `on_checkpoint` receives an atomic snapshot after each completed stage.
    """
    resumed = parse_checkpoint(resume_checkpoint)
    finished_stages = list(resumed.finished_stages) if resumed else []
    media: Path | None = None
    audio: Path | None = None
    transcript: Transcript | None = None
    local_identity: dict[str, Any] | None = resumed.local_identity if resumed else None

    def _checkpoint(stage: str | None = None) -> CheckpointRecord | None:
        if check_cancel is not None:
            check_cancel()
        if stage is None or on_checkpoint is None:
            return None
        if stage not in finished_stages:
            finished_stages.append(stage)
        snapshot = CheckpointRecord(
            source=source,
            model=model,
            language=language,
            engine=engine,
            device=resumed.device if resumed else device,
            options={
                "formats": list(formats or []),
                "diarize": diarize,
                "diarizer_backend": diarizer_backend,
                "translate_to": translate_to,
                "translator_backend": translator_backend,
            },
            finished_stages=list(finished_stages),
            transcript=transcript.to_dict() if isinstance(transcript, Transcript) else None,
            media_path=_recordable(media),
            audio_path=_recordable(audio),
            local_identity=local_identity,
        )
        on_checkpoint(snapshot.to_dict())
        return snapshot

    def _recordable(path: Path | None) -> str | None:
        """A path we can honestly promise to a later run.

        The scratch directory is deleted at the end of a run, so recording a
        path inside it produces a checkpoint that looks valid but can never be
        used - resume finds the file gone and silently redoes the work. Only
        paths outside scratch are worth storing.
        """
        if path is None:
            return None
        try:
            resolved = Path(path).resolve()
        except OSError:
            return None
        if scratch is not None and scratch.resolve() in resolved.parents:
            return None
        return str(path)

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
    root = input_root if input_root is not None else default_input_root()
    resolved_source = source
    if not source.startswith(("http://", "https://")):
        try:
            resolved_source = str(resolve_input_path(source, root=root))
        except (FileNotFoundError, ValueError, UnsafeInputPathError) as exc:
            raise PipelineError(str(exc)) from exc

    try:
        ref = resolve_source(resolved_source)
    except (FileNotFoundError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc

    _checkpoint("source")

    if resumed is not None and ref.kind == "file":
        try:
            validate_local_resume(resumed, resolved_source, input_root=root)
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="textflowkit-", dir=work_dir))

    try:
        transcript = _resume_transcript(resumed, source=source, ref=ref)
        if resumed is not None:
            media = _existing_path(resumed.media_path)
            audio = _existing_path(resumed.audio_path)

        # Resuming means "the transcript already exists, do not transcribe again".
        # The media files are a separate question: they live in scratch and are
        # deleted after every run, so requiring them would make resume impossible.
        # If a later stage (diarization) genuinely needs the audio and it is gone,
        # re-acquire it - that is far cheaper than re-running Whisper.
        can_resume = transcript is not None and _checkpoint_paths_usable(resumed)
        if not can_resume:
            transcript = None

        try:
            if not can_resume:
                require_tool("ffmpeg")
                if ref.kind == "file" and root is not None:
                    media = stage_confined_local_media(
                        ref.location, work_dir=scratch, input_root=root
                    )
                else:
                    media = fetch_media(
                        ref,
                        work_dir=scratch,
                        cookies_from_browser=cookies_from_browser,
                        check_cancel=check_cancel,
                    )
                if ref.kind == "file":
                    local_identity = local_source_identity(
                        resolved_source, input_root=root, content_path=media,
                    )
                _checkpoint("fetch")
                enforce_predecode_limits(media)
                audio = extract_audio(media, work_dir=scratch, check_cancel=check_cancel)
                enforce_media_limits(media, audio)
                _checkpoint("extract")

                eng = get_engine(engine, model=model, device=device)
                try:
                    transcript = eng.transcribe(audio, language=language)
                except Exception as exc:  # engine failures are user-facing
                    raise PipelineError(f"transcription failed: {exc}") from exc
                if ref.kind == "file":
                    try:
                        current = local_source_identity(resolved_source, input_root=root)
                    except (FileNotFoundError, OSError, ValueError) as exc:
                        raise PipelineError("local source changed during transcription") from exc
                    if current != local_identity:
                        raise PipelineError("local source changed during transcription")
                _checkpoint("transcribe")
            elif diarize and audio is None:
                # A finished transcript is the expensive checkpoint. Reacquire
                # only the audio required by pyannote; never rerun Whisper.
                require_tool("ffmpeg")
                if media is None:
                    if ref.kind == "file" and root is not None:
                        media = stage_confined_local_media(
                            ref.location, work_dir=scratch, input_root=root
                        )
                    else:
                        media = fetch_media(
                            ref, work_dir=scratch,
                            cookies_from_browser=cookies_from_browser,
                            check_cancel=check_cancel,
                        )
                    _checkpoint("fetch")
                enforce_predecode_limits(media)
                audio = extract_audio(media, work_dir=scratch, check_cancel=check_cancel)
                enforce_media_limits(media, audio)
                _checkpoint("extract")
        except (AcquisitionError, UnsafeInputPathError) as exc:
            raise PipelineError(str(exc)) from exc

        if diarize:
            # Refuse loudly rather than returning a transcript with empty speakers.
            # A silent no-op here is exactly the defect that was removed from
            # --speaker-labels, and it must not come back through this door.
            if audio is None:
                raise PipelineError("diarization requested but no audio is available for resume")
            try:
                diarizer = get_diarizer(diarizer_backend)
                turns = diarizer.diarize(audio)
            except DiarizationError as exc:
                raise PipelineError(f"diarization requested but unavailable: {exc}") from exc
            except Exception as exc:
                raise PipelineError(f"diarization failed: {exc}") from exc
            labelled = assign_speakers(transcript.segments, turns)
            transcript.metadata["diarization"] = {
                "backend": getattr(diarizer, "name", diarizer_backend),
                "speakers": sorted({t.speaker for t in turns}),
                "turns": len(turns),
                "segments_labelled": labelled,
            }

        if translate_to:
            # Refuse loudly: never present source text as though it were translated.
            try:
                translator = get_translator(translator_backend)
                translated = translate_segments(
                    transcript.segments, translate_to, translator=translator
                )
            except TranslationError as exc:
                raise PipelineError(f"translation requested but unavailable: {exc}") from exc
            except ValueError as exc:
                raise PipelineError(f"translation failed: {exc}") from exc
            transcript.metadata["translation"] = {
                "backend": getattr(translator, "name", translator_backend),
                "route": getattr(translator, "route", "unknown"),
                "target": translate_to,
                "segments_translated": translated,
            }

        _checkpoint("postprocess")

        transcript.source = source
        transcript.platform = ref.platform

        outputs: list[Path] = []
        if output_dir is not None:
            stem = Path(ref.location).stem if ref.kind != "url" else "transcript"
            stem = stem.replace("textflowkit-", "") or "transcript"
            stem = f"{stem}-{output_id or uuid.uuid4().hex[:16]}"
            outputs = write_all(transcript, formats=formats, output_dir=output_dir, stem=stem)
        _checkpoint("render")

        assert transcript is not None
        result = TranscribeResult(transcript=transcript, outputs=outputs)
    except BaseException:
        _cleanup_scratch(scratch)
        raise
    if not keep_media:
        _cleanup_scratch(scratch)
    return result
