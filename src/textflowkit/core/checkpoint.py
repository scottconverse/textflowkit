"""Resumable transcription checkpoints.

A checkpoint is stored on the existing `Job` record, so there is no second
persistence mechanism to keep consistent. The store owns durability; this module
owns the shape of the record, safe reading, and matching a request against a
candidate.

Two rules matter for correctness:

- A malformed checkpoint is treated as absent. A corrupt record must never make
  resume crash; it means "start clean".
- A checkpoint is only reusable when source, model, and language match. Anything
  less can silently mix transcripts from different runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textflowkit.core.jobs import Job, JobState, JobStore
from textflowkit.core.model import Transcript
from textflowkit.core.paths import opened_file_path, resolve_input_path
from textflowkit.render import ensure_outputs

CHECKPOINT_VERSION = 2
RESUMABLE_STATES = frozenset(
    {JobState.PENDING, JobState.RUNNING, JobState.ERROR, JobState.CANCELLED, JobState.DONE}
)


class CheckpointError(ValueError):
    """Raised when explicitly creating a malformed checkpoint."""


@dataclass(slots=True)
class CheckpointRecord:
    """A durable snapshot of completed pipeline work for one source."""

    source: str
    model: str
    language: str | None = None
    engine: str = "whisper"
    device: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    finished_stages: list[str] = field(default_factory=list)
    transcript: dict[str, Any] | None = None
    media_path: str | None = None
    audio_path: str | None = None
    local_identity: dict[str, Any] | None = None
    version: int = CHECKPOINT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "model": self.model,
            "language": self.language,
            "engine": self.engine,
            "device": self.device,
            "options": dict(self.options),
            "finished_stages": list(self.finished_stages),
            "transcript": self.transcript,
            "media_path": self.media_path,
            "audio_path": self.audio_path,
            "local_identity": self.local_identity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointRecord:
        if not isinstance(data, dict):
            raise CheckpointError("checkpoint is not an object")
        # Records written before versioning was explicit are legacy v1, not v2.
        version = data.get("version", 1)
        if version not in {1, CHECKPOINT_VERSION}:
            raise CheckpointError(f"unsupported checkpoint version: {version!r}")
        source = data.get("source")
        model = data.get("model")
        if not isinstance(source, str) or not source:
            raise CheckpointError("checkpoint is missing source")
        if not isinstance(model, str) or not model:
            raise CheckpointError("checkpoint is missing model")
        language = data.get("language")
        if language is not None and not isinstance(language, str):
            raise CheckpointError("checkpoint language is invalid")
        options = data.get("options") or {}
        if not isinstance(options, dict):
            raise CheckpointError("checkpoint options are invalid")
        stages = data.get("finished_stages") or []
        if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
            raise CheckpointError("checkpoint finished_stages are invalid")
        transcript = data.get("transcript")
        if not isinstance(transcript, dict):
            # A checkpoint without a validated transcript cannot be resumed;
            # treat it as corrupt rather than "start from nothing".
            raise CheckpointError("checkpoint is missing a transcript")
        # Validate nested transcript structure now; callers should never
        # discover corruption halfway through a resumed pipeline.
        Transcript.from_dict(transcript)
        if "transcribe" not in stages:
            raise CheckpointError("checkpoint has not finished transcription")
        identity = data.get("local_identity") if version == CHECKPOINT_VERSION else None
        if identity is not None and not _valid_local_identity(identity):
            raise CheckpointError("checkpoint local source identity is invalid")
        return cls(
            version=version,
            source=source,
            model=model,
            language=language,
            engine=str(data.get("engine") or "whisper"),
            device=data.get("device") if isinstance(data.get("device"), str) else None,
            options=dict(options),
            finished_stages=list(stages),
            transcript=transcript,
            media_path=_optional_str(data.get("media_path")),
            audio_path=_optional_str(data.get("audio_path")),
            local_identity=identity,
        )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _valid_local_identity(identity: Any) -> bool:
    return (
        isinstance(identity, dict)
        and isinstance(identity.get("path"), str)
        and bool(identity["path"])
        and isinstance(identity.get("size"), int)
        and identity["size"] >= 0
        and isinstance(identity.get("sha256"), str)
        and len(identity["sha256"]) == 64
        and all(c in "0123456789abcdef" for c in identity["sha256"])
    )


def is_local_source(source: str) -> bool:
    return not source.startswith(("http://", "https://"))


def local_source_identity(
    source: str,
    *,
    input_root: str | Path | None = None,
    content_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fingerprint bytes and a normalized source path, with a stable open handle.

    ``content_path`` is the handle-verified staged copy when input confinement is
    active; the logical identity still names the original source. A changed
    source is rechecked after transcription before a checkpoint is published.
    """
    logical = resolve_input_path(source, root=input_root)
    path = Path(content_path) if content_path is not None else logical
    digest = hashlib.sha256()
    with path.open("rb") as opened:
        actual = opened_file_path(opened.fileno(), path)
        if content_path is None and actual != logical:
            raise ValueError("local source changed while it was opened")
        before = os.fstat(opened.fileno())
        while chunk := opened.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(opened.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("local source changed while it was fingerprinted")
    return {"path": str(logical), "size": after.st_size, "sha256": digest.hexdigest()}


def validate_local_resume(
    record: CheckpointRecord,
    source: str,
    *,
    input_root: str | Path | None = None,
) -> None:
    """Fail closed for missing, changed, or pre-v2 local-source checkpoints."""
    if not is_local_source(source):
        return  # URL bytes can change; URL resume is a separate explicit policy.
    if record.version < CHECKPOINT_VERSION or record.local_identity is None:
        raise ValueError("legacy local checkpoint has no fingerprint; resubmit without resume")
    try:
        current = local_source_identity(source, input_root=input_root)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("local source is missing or unreadable; resubmit without resume") from exc
    if current != record.local_identity:
        raise ValueError("local source changed since checkpoint; resubmit without resume")


def load_checkpoint(job: Job | None) -> CheckpointRecord | None:
    """Return a valid checkpoint from a job, or None on absent/corrupt data."""
    if job is None or job.checkpoint is None:
        return None
    try:
        return CheckpointRecord.from_dict(job.checkpoint)
    except (CheckpointError, TypeError, ValueError, KeyError):
        return None


def parse_checkpoint(raw: Any) -> CheckpointRecord | None:
    """Parse an untrusted checkpoint value, returning None instead of raising."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    try:
        return CheckpointRecord.from_dict(raw)
    except (CheckpointError, TypeError, ValueError, KeyError):
        return None


def checkpoint_for_request(
    *,
    source: str,
    model: str,
    language: str | None = None,
    engine: str = "whisper",
    device: str | None = None,
    options: dict[str, Any] | None = None,
) -> CheckpointRecord:
    """Build a checkpoint keyed by the options that must match on resume."""
    return CheckpointRecord(
        source=source,
        model=model,
        language=language,
        engine=engine,
        device=device,
        options=dict(options or {}),
    )


def matches(
    checkpoint: CheckpointRecord,
    *,
    source: str,
    model: str,
    language: str | None = None,
    engine: str = "whisper",
    device: str | None = None,
    options: dict[str, Any] | None = None,
) -> bool:
    """Whether a checkpoint can be safely reused for this request."""
    if checkpoint.source != source:
        return False
    if checkpoint.model != model:
        return False
    if checkpoint.language != language:
        return False
    if checkpoint.engine != engine:
        return False
    if checkpoint.device != device:
        return False
    return options is None or checkpoint.options == dict(options)


def find_resumable_checkpoint(
    store: JobStore,
    *,
    source: str,
    model: str,
    language: str | None = None,
    engine: str = "whisper",
    device: str | None = None,
    options: dict[str, Any] | None = None,
    limit: int = 10_000,
) -> tuple[Job, CheckpointRecord] | None:
    """Find the newest job with a matching, valid checkpoint.

    Interrupted jobs are marked ERROR by startup reaping, so terminal jobs are
    included deliberately. A DONE job may be reused too; that turns a repeated
    invocation into a cheap no-op rather than recomputing identical work.
    """
    for job in store.list(limit=limit):
        if job.state not in RESUMABLE_STATES:
            continue
        record = load_checkpoint(job)
        if record is None:
            continue
        if matches(
            record,
            source=source,
            model=model,
            language=language,
            engine=engine,
            device=device,
            options=options,
        ):
            return job, record
    return None


def prepare_resume(
    store: JobStore,
    job: Job,
    checkpoint: CheckpointRecord | dict[str, Any],
) -> tuple[Job, dict[str, Any]] | None:
    """Reopen a resumed job for another run, or return None if it is DONE.

    A checkpoint from an ERROR/CANCELLED job is durable work, but the job row is
    terminal, so `run_job` would refuse to start. Explicit resume is the one
    place allowed to un-terminal that row: reset it to PENDING (clearing the
    stale error/cancellation flag) while leaving the checkpoint byte-for-byte
    intact. A DONE job is a different case: the transcript and outputs are the
    real deliverable, so resuming it is a no-op and callers should render from
    the stored transcript instead.
    """
    current = store.get(job.id)
    if current is None:
        return None
    if current.state is JobState.DONE:
        return None
    payload = checkpoint.to_dict() if isinstance(checkpoint, CheckpointRecord) else dict(checkpoint)
    updated = store.update(
        job.id,
        state=JobState.PENDING,
        progress="resuming",
        error=None,
        cancel_requested=False,
        checkpoint=payload,
    )
    if updated is None:
        return None
    return updated, payload


def write_checkpoint(
    store: JobStore,
    job_id: str,
    checkpoint: CheckpointRecord | dict[str, Any],
) -> Job | None:
    """Persist a checkpoint through the existing store update path."""
    payload = checkpoint.to_dict() if isinstance(checkpoint, CheckpointRecord) else dict(checkpoint)
    return store.update(job_id, checkpoint=payload)


def transcript_for_job(job: Job | None) -> Transcript | None:
    """Return the stored transcript for a terminal job, or None if absent/corrupt."""
    if job is None or job.transcript is None:
        return None
    try:
        return Transcript.from_dict(job.transcript)
    except (KeyError, TypeError, ValueError):
        return None


def reusable_done_result(
    store: JobStore,
    job: Job,
    *,
    transcript: Transcript | None = None,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    stem: str | None = None,
) -> tuple[Transcript, list[Path]] | None:
    """Return stored transcript/outputs for a DONE job when it can be reused.

    This is the no-op half of explicit resume: a job that already reached DONE
    must not be reopened or re-run. Callers use this before falling back to the
    normal pipeline, and render any missing requested formats from the stored
    transcript without touching acquisition or the engine.

    Returns None when the job is not DONE or its transcript is unreadable, which
    makes the caller start clean rather than presenting corrupt output as a
    successful resume.
    """
    current = store.get(job.id)
    if current is None or current.state is not JobState.DONE:
        return None
    parsed = transcript or transcript_for_job(current)
    if parsed is None:
        return None
    if formats:
        if not stem:
            raise CheckpointError("stem is required when rendering reused outputs")
        outputs = ensure_outputs(
            parsed,
            formats=formats,
            output_dir=output_dir,
            stem=stem,
            existing=current.outputs,
        )
        return parsed, outputs
    return parsed, [Path(path) for path in current.outputs]
