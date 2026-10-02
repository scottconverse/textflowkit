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
import wave
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
from textflowkit.core.engine import get_engine, require_engine
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
from textflowkit.render import (
    DEFAULT_FORMATS,
    SUPPORTED_FORMATS,
    validate_export_requirements,
    write_all,
)
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


# The per-stage completion markers a run leaves after each *optional*
# postprocessor succeeds, and the coarse marker older records carry instead.
#
# A resume must reuse the optional work a checkpoint already holds, but it must
# only do so when the record actually *says* the stage finished. Presence of
# speaker labels or translated text is not a completion signal: a half-finished
# snapshot can carry either without the stage having been marked done, and
# serving it as complete would skip work that never happened. So the decision is
# keyed on the marker, never on the metadata.
DIARIZE_STAGE = "diarize"
TRANSLATE_STAGE = "translate"
LEGACY_POSTPROCESS_STAGE = "postprocess"


def _stage_completed(
    resumed: CheckpointRecord | None,
    stage: str,
    *,
    translate_to: str | None = None,
    diarizer_backend: str | None = None,
    translator_backend: str | None = None,
) -> bool:
    """Whether a checkpoint records ``stage`` finished for *this* configuration.

    ``postprocess`` is the only postprocess checkpoint records written before
    per-stage markers existed, and it was fired after *every* optional stage
    that record's options requested had completed. It therefore stands in for
    "the optional stages this record's options requested are all finished". It
    is a completion signal precisely because it was only ever written on
    success; a record that lacks any postprocess marker has not finished the
    optional stage, whatever its transcript metadata holds.

    But "finished" is only reusable when it is finished *for the stage being
    asked for now*. A legacy record that ran with ``diarize=True`` and no
    translation must not satisfy a resume that now asks for German: the coarse
    mark covers the stages that record requested, not a stage it never ran. The
    same holds for per-stage markers, which describe one provider configuration.
    So the marker and the option match must both hold.
    """
    if resumed is None:
        return False
    stages = resumed.finished_stages
    if stage not in stages and LEGACY_POSTPROCESS_STAGE not in stages:
        # A per-stage marker for the *other* optional stage must not stand in for
        # this one: only the coarse legacy mark covers both.
        return False
    if stage == DIARIZE_STAGE:
        prior = resumed.options.get("diarizer_backend")
        return bool(resumed.options.get("diarize")) and (
            diarizer_backend is None or prior == diarizer_backend
        )
    if stage == TRANSLATE_STAGE:
        prior_target = resumed.options.get("translate_to")
        prior_backend = resumed.options.get("translator_backend")
        if not prior_target:
            return False
        if translate_to is not None and prior_target != translate_to:
            return False
        return translator_backend is None or prior_backend == translator_backend
    return False


def _optional_stage_plan(
    resumed: CheckpointRecord | None,
    *,
    diarize: bool,
    translate_to: str | None,
    diarizer_backend: str,
    translator_backend: str,
) -> tuple[bool, bool, bool]:
    """Decide which optional stages still need to run, and whether audio is needed.

    Returns ``(run_diarize, run_translate, need_audio)``. A stage runs only when
    the caller requested it *and* the checkpoint does not already record it
    finished *for the requested configuration* - a finished stage under a
    different backend or translation target is not this stage's work and must be
    redone rather than served. ``need_audio`` is the reason to re-acquire:
    diarization is the one optional stage that consumes the audio, so if it is
    already done the media must not be fetched and decoded again merely to reach
    a provider that will not be consulted.
    """
    run_diarize = bool(diarize) and not _stage_completed(
        resumed, DIARIZE_STAGE, diarizer_backend=diarizer_backend
    )
    run_translate = bool(translate_to) and not _stage_completed(
        resumed, TRANSLATE_STAGE,
        translate_to=translate_to,
        translator_backend=translator_backend,
    )
    return run_diarize, run_translate, run_diarize


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
    on_stage: Callable[[str], None] | None = None,
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

    `on_stage` receives the name of the stage about to run. Checkpoints fire on
    completion, so they cannot answer "what is happening now": by the time one
    arrives, the next stage - usually the long one - has already started. The
    two callbacks are deliberately separate, and only `on_stage` is reported
    while work is in flight. It is a fire-and-forget notice for a *display*: the
    store's own `progress` value is written separately by the runner's sink, so a
    resumed stage that is reused rather than run is never announced here.
    """
    resumed = parse_checkpoint(resume_checkpoint)
    finished_stages = list(resumed.finished_stages) if resumed else []
    media: Path | None = None
    audio: Path | None = None
    transcript: Transcript | None = None
    scratch: Path | None = None
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

    def _stage(name: str) -> None:
        """Announce the stage now starting, or stop if cancellation arrived.

        Pairing the announcement with the cancellation check keeps the two
        honest: a stage is only announced if we are about to run it.
        """
        if check_cancel is not None:
            check_cancel()
        if on_stage is not None:
            on_stage(name)

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

    formats = formats or list(DEFAULT_FORMATS)
    for fmt in formats:
        if fmt.lower().lstrip(".") not in SUPPORTED_FORMATS:
            raise PipelineError(
                f"unsupported format '{fmt}'; choose from {', '.join(SUPPORTED_FORMATS)}"
            )
    if output_dir is not None:
        validate_export_requirements(formats)

    # The engine name, and whether an optional engine's package is importable at
    # all, are both knowable from the arguments alone. Checking them here means a
    # typo or a missing extra costs neither a download, a decode, nor a model
    # load. The adapters preflight through `SubmissionRequest` and
    # `submit_request`; this is the same refusal for a caller who reaches the
    # pipeline directly from Python.
    try:
        require_engine(engine)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

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

    # Validate and hydrate reusable work *before* publishing anything. Every
    # snapshot this run hands to `on_checkpoint` replaces the durable one, so a
    # source checkpoint published before validation/hydration would overwrite a
    # completed transcript with the local `transcript` - still ``None`` here -
    # leaving a record that still lists ``transcribe`` as finished but holds
    # nothing. Publishing the source checkpoint only after reuse is read from the
    # previous snapshot is what keeps a changed local source refused (below) and
    # a setup failure from destroying usable work: nothing is written until the
    # prior coherent snapshot has been validated and its reusable parts adopted.
    if resumed is not None and ref.kind == "file":
        try:
            validate_local_resume(resumed, resolved_source, input_root=root)
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc

    # Resuming means "the transcript already exists, do not transcribe again".
    # The media files are a separate question: they live in scratch and are
    # deleted after every run, so requiring them would make resume impossible.
    # If a later stage (diarization) genuinely needs the audio and it is gone,
    # re-acquire it - that is far cheaper than re-running Whisper.
    transcript = _resume_transcript(resumed, source=source, ref=ref)
    can_resume = transcript is not None and _checkpoint_paths_usable(resumed)
    if not can_resume:
        # Only adopt reusable paths when the transcript actually resumes; a
        # half-usable snapshot must not carry stale media paths into a new one.
        transcript = None
        media = None
        audio = None
    else:
        media = _existing_path(resumed.media_path)
        audio = _existing_path(resumed.audio_path)

    # Which optional stages still need to run is decided *before* any
    # acquisition, because it is also the answer to "is the audio needed at
    # all". A resume whose optional work is already durable must not fetch or
    # decode media just to reach a provider it is not going to consult.
    run_diarize, run_translate, needs_audio_for_resume = _optional_stage_plan(
        resumed if can_resume else None,
        diarize=diarize,
        translate_to=translate_to,
        diarizer_backend=diarizer_backend,
        translator_backend=translator_backend,
    )

    # The source is now resolved, validated, and its reusable work hydrated, so
    # the replacement snapshot is coherent: it carries the reused transcript and
    # markers rather than an empty local value.
    _checkpoint("source")

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="textflowkit-", dir=work_dir))

    try:
        # The decoder boundary follows the *source*, not the branch that
        # acquired the media. A resumed run can carry a still-existing media path
        # from its checkpoint and skip re-staging entirely, so deriving this
        # inside that branch would leave that path decoded with no restriction -
        # under an input root, which is exactly when the boundary must hold.
        confined = ref.kind == "file" and root is not None

        try:
            if not can_resume:
                require_tool("ffmpeg")
                _stage("fetching")
                if confined:
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
                enforce_predecode_limits(media, confined=confined)
                _stage("extracting")
                audio = extract_audio(
                    media, work_dir=scratch, check_cancel=check_cancel, confined=confined
                )
                enforce_media_limits(media, audio)
                _checkpoint("extract")

                eng = get_engine(engine, model=model, device=device)
                _stage("transcribing")
                try:
                    transcript = eng.transcribe(audio, language=language)
                except Exception as exc:  # engine failures are user-facing
                    raise PipelineError(f"transcription failed: {exc}") from exc
                # extract_audio always writes PCM WAV. Its frame count includes
                # trailing silence and is more precise than the last speech cue.
                try:
                    with wave.open(str(audio), "rb") as wav:
                        if wav.getframerate() > 0:
                            transcript.duration = wav.getnframes() / wav.getframerate()
                except (OSError, EOFError, wave.Error):
                    # Preserve an engine-supplied duration for test/custom engines.
                    pass
                if ref.kind == "file":
                    try:
                        current = local_source_identity(resolved_source, input_root=root)
                    except (FileNotFoundError, OSError, ValueError) as exc:
                        raise PipelineError("local source changed during transcription") from exc
                    if current != local_identity:
                        raise PipelineError("local source changed during transcription")
                _checkpoint("transcribe")
            elif needs_audio_for_resume and audio is None:
                # A finished transcript is the expensive checkpoint, and the
                # diarization it may still need is the only remaining consumer of
                # the audio. Reacquire only what pyannote needs; never rerun
                # Whisper. When diarization is already recorded complete this
                # branch is not taken at all, so the media is neither fetched nor
                # decoded - the durable labels are reused as they stand.
                require_tool("ffmpeg")
                if media is None:
                    _stage("fetching")
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
                enforce_predecode_limits(media, confined=confined)
                _stage("extracting")
                audio = extract_audio(
                    media, work_dir=scratch, check_cancel=check_cancel, confined=confined
                )
                enforce_media_limits(media, audio)
                _checkpoint("extract")
        except (AcquisitionError, UnsafeInputPathError) as exc:
            raise PipelineError(str(exc)) from exc

        if run_diarize or run_translate:
            # Announced once, before the first of the optional postprocessors.
            # Neither running means no postprocess stage happens at all, and
            # saying otherwise would report work that does not exist. A stage the
            # checkpoint already finished is not running, so it is not announced.
            _stage("postprocessing")

        if run_diarize:
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
            # Durable the instant the labels exist, so a failure in *translation*
            # resumes with the diarization already done instead of re-running the
            # diarizer (and, before this, re-acquiring the audio to do it).
            _checkpoint(DIARIZE_STAGE)

        if run_translate:
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
            _checkpoint(TRANSLATE_STAGE)

        # The coarse mark is still written, and only once every requested optional
        # stage has run or been reused, so a record read by an older build (and by
        # the legacy rule this fix keeps) still says "postprocess finished". It is
        # deliberately last: a partial attempt that finished diarization but not
        # translation never reaches it, which is what keeps a half-done snapshot
        # from being mistaken for a complete one.
        _checkpoint(LEGACY_POSTPROCESS_STAGE)

        transcript.source = source
        transcript.platform = ref.platform

        outputs: list[Path] = []
        if output_dir is not None:
            # Only announced when there is something to write: with no
            # `output_dir` the run renders nothing and must not say it does.
            _stage("rendering")
            stem = Path(ref.location).stem if ref.kind != "url" else "transcript"
            stem = stem.replace("textflowkit-", "") or "transcript"
            stem = f"{stem}-{output_id or uuid.uuid4().hex[:16]}"
            # A resume of the same job keeps the job id, so this stem names the
            # files that job published before it failed. Only then may an
            # existing artifact be adopted - and only when its bytes are exactly
            # what this run renders. A fresh run has no checkpoint and keeps the
            # strict no-clobber rule. The pairing of `resume_checkpoint` with
            # this `output_id` is set by the single submission path, which always
            # resumes a job with that job's own checkpoint.
            outputs = write_all(
                transcript,
                formats=formats,
                output_dir=output_dir,
                stem=stem,
                reuse_published=resumed is not None and output_id is not None,
            )
        _checkpoint("render")

        assert transcript is not None
        result = TranscribeResult(transcript=transcript, outputs=outputs)
    except BaseException:
        _cleanup_scratch(scratch)
        raise
    if not keep_media:
        _cleanup_scratch(scratch)
    return result
