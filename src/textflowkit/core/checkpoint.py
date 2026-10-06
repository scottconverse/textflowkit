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

Storage contract for a finished job
-----------------------------------

A transcript is stored once per job. While a job is in flight the checkpoint
carries it, because that is the only durable copy of the expensive work: a
resume reads it and skips acquisition and the engine. When the job reaches DONE
the transcript moves to the job's own `transcript` field and the checkpoint
drops its copy, keeping only the metadata a later request is matched against -
source, model, language, engine, device, options, finished stages, recorded
media/audio paths, and the local-source identity. A finished job therefore pays
for its words once, and `load_checkpoint` assembles the full record for a DONE
job from the two halves in memory.

Only DONE is hydrated that way. For an ERROR or CANCELLED job the checkpoint is
still the sole copy, so filling it in from the job field would invent work that
never finished; `metadata_only_checkpoint` is the write half of this contract
and `load_checkpoint` is the read half.

Partial work
------------

A long native run (Whistle cuts a recording into short windows) can also be
resumed *mid-transcription*. That is a different kind of record: version 3 adds
``engine_progress``, a validated snapshot of the engine's own per-block progress
which carries no completed transcript. A partial record therefore can never be
hydrated or promoted as a finished result - it has no transcript to serve - and
it must not list ``transcribe`` as finished. It is matched and source-validated
exactly like a full record; the engine revalidates the progress body against the
real redecoded audio before running a single remaining window. A record that
carries a transcript never also carries ``engine_progress``: the two would be the
same words stored twice.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textflowkit.core.jobs import RESUMABLE_CLAIM_STATES, Job, JobState, JobStore
from textflowkit.core.model import Transcript
from textflowkit.core.paths import opened_file_path, resolve_input_path
from textflowkit.render import ensure_outputs

#: The current record version. v3 adds a *partial* form: a checkpoint that holds
#: per-block engine progress (``engine_progress``) but no completed transcript,
#: so long native-engine work can resume where it stopped. A v3 record is still
#: written in full at every completed stage, exactly as v2 was.
CHECKPOINT_VERSION = 3
#: The version that introduced the local-source fingerprint (``local_identity``).
#: A record at or above this version can be validated against the bytes on disk;
#: anything older has no fingerprint and a local resume must fail closed rather
#: than reuse work whose source may have changed. Kept separate from
#: ``CHECKPOINT_VERSION`` so bumping the current version does not silently strip
#: the fingerprint from the records that already carry one.
IDENTITY_VERSION = 2
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
    #: Per-block engine progress for a *partial* run (Whistle's windowed
    #: transcription). Present only while ``transcribe`` has not finished: it is
    #: validated, unfinished work, never a completed transcript, so it can resume
    #: the engine but can never be served as a finished result. A record with a
    #: ``transcript`` never carries this.
    engine_progress: dict[str, Any] | None = None
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
            "engine_progress": self.engine_progress,
            "media_path": self.media_path,
            "audio_path": self.audio_path,
            "local_identity": self.local_identity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointRecord:
        if not isinstance(data, dict):
            raise CheckpointError("checkpoint is not an object")
        # Records written before versioning was explicit are legacy v1, not v3.
        version = data.get("version", 1)
        if version not in {1, 2, CHECKPOINT_VERSION}:
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
        engine_progress = data.get("engine_progress")
        # The record's engine is read here, before the partial branch: the partial
        # form is Whistle's own per-block body, so a record that claims to carry
        # one must also claim the Whistle engine. A non-Whistle engine with a
        # Whiskey-shaped partial is corrupt rather than a resumable run - no
        # other engine emits that body, so accepting it would let a foreign
        # object masquerade as durable work.
        #
        # The persisted name is canonicalized the same way ``SubmissionRequest``
        # decodes a saved request, because the two are matched against each other
        # on resume. Before Whistle existed, ``default`` and ``openai-whisper``
        # both meant openai-whisper - a record saved under the old default is a
        # *Whisper* record, and decoding it as the new default (Whistle) would
        # break the checkpoint match and re-run completed work. The current
        # canonical form never persists ``default`` at all, so this rewrite only
        # ever touches pre-Whistle records. A name that is already canonical
        # (``whistle``, ``whisper``, ``faster-whisper``) is kept unchanged: in
        # particular an actual ``whistle`` record is never rewritten.
        record_engine = _canonical_persisted_engine(data.get("engine"))
        if transcript is not None:
            # A record carrying a transcript is a finished one: the transcript
            # must validate, transcription must be marked done, and a partial
            # engine body must not coexist with it (that would be the same words
            # stored twice, and would leave a resume able to read either copy).
            if not isinstance(transcript, dict):
                raise CheckpointError("checkpoint transcript is invalid")
            Transcript.from_dict(transcript)
            if "transcribe" not in stages:
                raise CheckpointError("checkpoint has not finished transcription")
            if engine_progress is not None:
                raise CheckpointError(
                    "checkpoint carries both a transcript and partial engine progress"
                )
        elif engine_progress is not None:
            # Only v3 introduces the partial form; an older version has no field
            # for it, so seeing one there is corrupt rather than a partial run.
            if version != CHECKPOINT_VERSION:
                raise CheckpointError(
                    f"checkpoint version {version} cannot carry partial engine progress"
                )
            # Partial progress is Whistle's per-block body and nothing else. Gate
            # on the canonical engine *name* here (cheap, import-free) rather than
            # on the body's shape alone: the engine's own ``validate_progress`` is
            # the authoritative check against the real audio, but a record that
            # does not even name Whistle must not be adopted as partial Whistle
            # work. A pre-Whistle record still naming the old ``default`` alias is
            # a Whisper record - the rewrite above makes it ``whisper`` - so a v3
            # partial body on it fails closed here rather than being reinterpreted
            # as Whistle's own.
            if record_engine != "whistle":
                raise CheckpointError(
                    "partial engine progress is Whistle-only but the checkpoint "
                    f"names engine {record_engine!r}"
                )
            _validate_engine_progress(engine_progress)
            if "transcribe" in stages:
                # Partial progress by definition has not finished transcription;
                # a record that says it did is contradictory and must not be
                # treated as either a finished or a resumable one.
                raise CheckpointError(
                    "partial engine progress lists transcription as finished"
                )
        else:
            # A checkpoint with neither a validated transcript nor partial
            # progress cannot be resumed; treat it as corrupt rather than
            # "start from nothing", exactly as before.
            raise CheckpointError("checkpoint is missing a transcript")
        identity = data.get("local_identity") if version >= IDENTITY_VERSION else None
        if identity is not None and not _valid_local_identity(identity):
            raise CheckpointError("checkpoint local source identity is invalid")
        return cls(
            version=version,
            source=source,
            model=model,
            language=language,
            engine=record_engine,
            device=data.get("device") if isinstance(data.get("device"), str) else None,
            options=dict(options),
            finished_stages=list(stages),
            transcript=transcript,
            engine_progress=engine_progress,
            media_path=_optional_str(data.get("media_path")),
            audio_path=_optional_str(data.get("audio_path")),
            local_identity=identity,
        )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


#: The engine names a persisted record may carry after canonicalization, and the
#: legacy aliases that map onto them. Kept as literals here rather than importing
#: ``engine`` so reading a job row pulls in neither the engine module nor the
#: asset table. ``default`` and ``openai-whisper`` both named openai-whisper
#: before Whistle was the default; the current canonical form never writes
#: ``default``, so any record still holding it predates that change.
_PERSISTED_ENGINE_ALIASES = {
    "default": "whisper",
    "openai-whisper": "whisper",
}


def _canonical_persisted_engine(value: Any) -> str:
    """Canonical engine name for a persisted record, decoding legacy aliases.

    A missing or non-string engine decodes to ``whisper``: records written before
    the engine field existed were all openai-whisper runs. A legacy alias
    ``default``/``openai-whisper`` decodes to ``whisper`` - what it *meant when
    written* - so a pre-Whistle record is never reinterpreted as the new default.
    An already-canonical name is returned unchanged, with case folded only for
    the alias lookup's benefit.
    """
    if not isinstance(value, str) or not value:
        return "whisper"
    return _PERSISTED_ENGINE_ALIASES.get(value.casefold(), value)


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


#: The Whistle progress schema marker. Duplicated as a literal rather than
#: imported so reading a checkpoint does not pull the engine module (and its
#: asset table) into every process that touches a job row. The engine revalidates
#: the whole body against the real redecoded audio before any core runs, so this
#: is only the "this claims to be a validated Whistle progress" gate - see
#: ``whistle.validate_progress`` for the authoritative check.
_WHISTLE_PROGRESS_SCHEMA = "textflowkit.whistle.progress/1"


def _validate_engine_progress(progress: Any) -> None:
    """Reject a partial engine-progress body whose *shape* is not trustworthy.

    This is a structural gate, not the full validation: a partial checkpoint is
    data from another process, so it must at minimum be an object carrying the
    Whistle progress schema, content/model/binary identities, a finite positive
    duration, a scalar language, a non-negative completed-core index, and a
    *present* segment list. The engine re-decides everything that needs the real
    audio (window policy, hashes, language, word ownership) before executing a
    single remaining core; nothing here is sanitized, so a malformed body is
    refused rather than trimmed into something that parses.
    """
    if not isinstance(progress, dict):
        raise CheckpointError("checkpoint partial engine progress is not an object")
    if progress.get("schema") != _WHISTLE_PROGRESS_SCHEMA:
        raise CheckpointError("checkpoint partial engine progress has an incompatible schema")
    if not isinstance(progress.get("policy"), str) or not progress["policy"]:
        raise CheckpointError("checkpoint partial engine progress has no window policy")
    if not _valid_sha256(progress.get("wav_identity")):
        raise CheckpointError("checkpoint partial engine progress has no audio identity")
    if not _valid_sha256(progress.get("model_sha256")):
        raise CheckpointError("checkpoint partial engine progress has no model identity")
    if not _valid_sha256(progress.get("binary_sha256")):
        raise CheckpointError("checkpoint partial engine progress has no runtime identity")
    # The duration is the resume match key against the real audio (`validate_progress`
    # compares it back), so a body without a finite positive one cannot be checked
    # and must not be trusted as *this* recording's partial work.
    try:
        stored_duration = float(progress.get("duration"))
    except (TypeError, ValueError) as exc:
        raise CheckpointError(
            "checkpoint partial engine progress has no usable duration"
        ) from exc
    if not math.isfinite(stored_duration) or stored_duration <= 0:
        raise CheckpointError(
            "checkpoint partial engine progress has a non-positive duration"
        )
    # Language is the requested language, or None for auto-detect. Anything else
    # (a list, an object, a number) is not a value the engine could have emitted
    # and would never compare equal to the current run's language choice.
    language = progress.get("language")
    if language is not None and not isinstance(language, str):
        raise CheckpointError("checkpoint partial engine progress has an invalid language")
    completed = progress.get("completed_core_index")
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 0:
        raise CheckpointError("checkpoint partial engine progress has no completed core index")
    # The segment list is required, not optional: it is the accumulated words the
    # resume adopts, so an absent list is a body with no work in it rather than a
    # partial run. ``validate_progress`` re-checks each entry against the audio.
    segments = progress.get("segments")
    if not isinstance(segments, list):
        raise CheckpointError("checkpoint partial engine progress has a corrupt segment list")


def _valid_local_identity(identity: Any) -> bool:
    return (
        isinstance(identity, dict)
        and isinstance(identity.get("path"), str)
        and bool(identity["path"])
        and isinstance(identity.get("size"), int)
        and identity["size"] >= 0
        and _valid_sha256(identity.get("sha256"))
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
    """Fail closed for missing, changed, or fingerprintless local checkpoints.

    The threshold is ``IDENTITY_VERSION``, not the current record version: the
    fingerprint arrived with v2 and a v2 record still carries one, so bumping the
    current version to v3 must not retroactively refuse a v2 resume whose source
    is provably unchanged. Only a record *older than the fingerprint itself* has
    nothing to compare and is refused.
    """
    if not is_local_source(source):
        return  # URL bytes can change; URL resume is a separate explicit policy.
    if record.version < IDENTITY_VERSION or record.local_identity is None:
        raise ValueError("legacy local checkpoint has no fingerprint; resubmit without resume")
    try:
        current = local_source_identity(source, input_root=input_root)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("local source is missing or unreadable; resubmit without resume") from exc
    if current != record.local_identity:
        raise ValueError("local source changed since checkpoint; resubmit without resume")


def load_checkpoint(job: Job | None) -> CheckpointRecord | None:
    """Return a valid checkpoint from a job, or None on absent/corrupt data.

    A DONE job holds its transcript in the job field and its resume metadata in
    the checkpoint, so the record returned here is assembled from both halves.
    The assembly happens in memory: a read never rewrites the stored row. Rows
    written before this rule - which carry the transcript in both places - are
    read from the checkpoint exactly as they always were, and a corrupt half in
    either direction is treated as absent rather than served as a resume.

    Only a DONE job is hydrated. See the module docstring for why an ERROR or
    CANCELLED checkpoint must speak for itself.
    """
    if job is None or not isinstance(job.checkpoint, dict):
        return None
    raw = job.checkpoint
    if raw.get("transcript") is None and job.state is JobState.DONE:
        if not isinstance(job.transcript, dict):
            return None
        raw = {**raw, "transcript": job.transcript}
    try:
        return CheckpointRecord.from_dict(raw)
    except (CheckpointError, TypeError, ValueError, KeyError):
        return None


def metadata_only_checkpoint(
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a finished job's checkpoint with its duplicated bodies removed.

    A DONE job stores its words once, in the job's own ``transcript`` field, so
    the checkpoint drops both the transcript *and* any partial engine progress:
    either is a second copy of the same words (the partial's cumulative segments
    are a prefix of the final transcript), and leaving one behind would let a
    later reader find the words twice - in the row and in the checkpoint. None
    means there was no body to remove - the checkpoint is absent, already
    metadata only, or not an object at all - so a caller leaves the stored value
    exactly as it found it instead of writing a value it did not read. The
    metadata that matching and local-source validation depend on is kept
    untouched; only the duplicated bodies go.
    """
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("transcript") is None and checkpoint.get("engine_progress") is None:
        return None
    payload = dict(checkpoint)
    payload.pop("transcript", None)
    payload.pop("engine_progress", None)
    return payload


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
    *,
    observed_attempt: int | None = None,
) -> tuple[Job, dict[str, Any]] | None:
    """Reopen a resumed job for another run, or return None if it is not claimable.

    A checkpoint from an ERROR/CANCELLED job is durable work, but the job row is
    terminal, so `run_job` would refuse to start. Explicit resume is the one
    place allowed to un-terminal that row: reset it to PENDING (clearing the
    stale error/cancellation flag) while leaving the checkpoint byte-for-byte
    intact. A DONE job is a different case: the transcript and outputs are the
    real deliverable, so resuming it is a no-op and callers should render from
    the stored transcript instead.

    The reopen is a *claim*: it happens only from an allowed terminal state, in
    one atomic store operation. Two callers racing the same job id therefore
    cannot both reopen it - one transitions the row and runs, the other gets
    None. That is why the check is the store's conditional transition rather than
    a get-then-update here: the read and the write must be one step for the
    ownership decision to mean anything to a concurrent caller.
    """
    payload = checkpoint.to_dict() if isinstance(checkpoint, CheckpointRecord) else dict(checkpoint)
    updated = store.claim(
        job.id,
        allowed_states=RESUMABLE_CLAIM_STATES,
        observed_attempt=observed_attempt,
        advance_attempt=True,
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
    *,
    observed_attempt: int | None = None,
) -> Job | None:
    """Persist a checkpoint through the existing store update path.

    ``observed_attempt`` is the generation a *run* owns. When given, the write
    goes through the store's guarded run-owned path, so a checkpoint produced by
    an old attempt cannot overwrite the checkpoint of a newer generation that has
    taken the row over, nor contaminate a row that has since reached a terminal
    state. It deliberately does *not* refuse an accepted cancellation: a run that
    has been asked to stop still publishes the work it has completed, because
    that checkpoint is the resume material a later attempt reads - discarding it
    would throw away the reason the cancellation is cooperative at all.

    Omitting ``observed_attempt`` keeps the original three-argument contract for
    callers that are not a run owning a generation (tests, adapters, embedding):
    those write unconditionally, exactly as before.
    """
    payload = checkpoint.to_dict() if isinstance(checkpoint, CheckpointRecord) else dict(checkpoint)
    if observed_attempt is None:
        return store.update(job_id, checkpoint=payload)
    return store.update_owned(
        job_id,
        observed_attempt=observed_attempt,
        refuse_if_cancelled=False,
        checkpoint=payload,
    )


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
        try:
            outputs = ensure_outputs(
                parsed,
                formats=formats,
                output_dir=output_dir,
                stem=stem,
                existing=current.outputs,
            )
        except FileExistsError as exc:
            # The render layer refused to publish over a file that is not this
            # job's own rendering (see `ensure_outputs`): a completed job's
            # output was changed on disk after it finished. The resume cannot be
            # honoured without overwriting those bytes, so it is refused in the
            # same voice as the other resume refusals, and the job record and
            # the file are both left exactly as they were.
            raise CheckpointError(
                f"recorded output for job '{current.id}' no longer matches the "
                "stored transcript; refusing to overwrite it (move it aside and "
                "resubmit without resume)"
            ) from exc
        return parsed, outputs
    return parsed, [Path(path) for path in current.outputs]
