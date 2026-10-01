"""One submission contract for CLI, MCP, HTTP, and batch callers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from textflowkit.core.checkpoint import (
    find_resumable_checkpoint,
    is_local_source,
    load_checkpoint,
    matches,
    prepare_resume,
    reusable_done_result,
    validate_local_resume,
)
from textflowkit.core.engine import (
    require_engine,
    validate_engine,
    validate_model,
)
from textflowkit.core.executor import QueueFullError, get_default_executor
from textflowkit.core.jobs import (
    RESUMABLE_CLAIM_STATES,
    Job,
    JobState,
    JobStore,
    ObservedJob,
)
from textflowkit.core.paths import (
    default_input_root,
    resolve_input_path,
    resolve_output_dir,
)
from textflowkit.core.runner import run_job
from textflowkit.core.service import reject_browser_cookie_requests
from textflowkit.render import DEFAULT_FORMATS, SUPPORTED_FORMATS, validate_export_requirements


@dataclass(slots=True)
class SubmissionRequest:
    source: str
    language: str | None = None
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_FORMATS))
    output_dir: str | None = None
    model: str = "small"
    engine: str = "whisper"
    device: str | None = None
    cookies_from_browser: str | None = None
    input_root: str | None = None
    work_dir: str | None = None
    diarize: bool = False
    diarizer_backend: str = "pyannote"
    translate_to: str | None = None
    translator_backend: str = "ollama"

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("source is required")
        # Every surface (CLI, MCP, HTTP single/batch, and resume rebuilding a
        # saved request) passes through here, so this is the one place a
        # production refusal covers all of them before any store write or queue.
        reject_browser_cookie_requests(self.cookies_from_browser)
        # The engine *name* is settled here, where it is still free: it is a
        # pure lookup with no import, so a typo is rejected on all four adapters
        # before a job record exists rather than after acquisition. Whether an
        # optional engine's package is actually installed is a separate question
        # and deliberately not asked here - see `_require_engine_ready`.
        validate_engine(self.engine)
        # The model *name* is settled next to the engine name, for the same
        # reason and with the same limits: it is a lookup against a name list the
        # engine publishes, not a load. See `validate_model` for why the engine's
        # own name list is asked rather than a copy kept here, and why a path is
        # refused rather than stat-ed.
        validate_model(self.model, self.engine)
        # Adapter path helpers return Path objects, but a durable request must
        # be JSON-serializable before it is inserted into SQLite.
        if isinstance(self.input_root, Path):
            self.input_root = str(self.input_root)
        if isinstance(self.work_dir, Path):
            self.work_dir = str(self.work_dir)
        # An explicit `output_dir` is resolved here, against the configured
        # output root, but deliberately not created: submission only has to
        # answer "would this be allowed", and the pipeline owns directory
        # creation at the point it writes. Checking it here means a path the
        # operator's root forbids is a request error on every surface rather
        # than a late failure after a job row and a queue slot exist.
        if self.output_dir is not None:
            resolve_output_dir(self.output_dir)
        # A local source must exist before a job is queued for it. `is_local_source`
        # is the same test the pipeline uses to decide fetch-vs-open, so a URL is
        # never stat-ed as a path; the resolver's missing-file error carries both
        # `FileNotFoundError` and `ValueError`, so the surfaces keep the refusal
        # they already have (a CLI message, an MCP `{"error": ...}`, an HTTP 422)
        # rather than surfacing a missing input as an unhandled 500.
        if is_local_source(self.source):
            resolve_input_path(self.source, root=self.input_root)
        self.formats = [str(fmt).lower().lstrip(".") for fmt in self.formats]
        if not self.formats:
            # `pipeline.transcribe` has always read an empty list as its normal
            # default, so an empty request means the default formats and nothing
            # else. Recording that here - before the request is persisted and
            # matched - is what keeps a checkpoint written under the default
            # findable by the request that produced it. Explicit, invalid, and
            # duplicate formats are untouched: only "nothing asked for" resolves.
            self.formats = list(DEFAULT_FORMATS)
        bad = [fmt for fmt in self.formats if fmt not in SUPPORTED_FORMATS]
        if bad:
            raise ValueError(f"unsupported format(s): {', '.join(bad)}")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("duplicate output format")
        if self.output_dir is not None:
            validate_export_requirements(self.formats)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubmissionRequest:
        return cls(**data)

    def options(self) -> dict[str, Any]:
        return {
            "formats": list(self.formats),
            "diarize": self.diarize,
            "diarizer_backend": self.diarizer_backend,
            "translate_to": self.translate_to,
            "translator_backend": self.translator_backend,
        }

    def run_kwargs(self) -> dict[str, Any]:
        return self.to_dict()


def _matching_checkpoint(store: JobStore, request: SubmissionRequest, job_id: str | None):
    """Find the job to resume and snapshot the identity the decision is made on.

    Returns ``(observed, checkpoint)``. ``observed`` is an immutable
    :class:`ObservedJob` taken *under the store's lock*, so its ``attempt``,
    ``state`` and ``checkpoint`` are all from one instant. That matters because a
    decision spans an intervening validation (the request match, the checkpoint
    match, and the local-resume preflight) before the claim; reading fields off a
    live alias after that validation, or reading ``job.attempt`` after loading
    the checkpoint, would let a concurrent writer move the attempt in between and
    hand the decision an identity it never observed.
    """
    if job_id is not None:
        observed = store.observe(job_id)
        if observed is None:
            raise ValueError(f"no job with id '{job_id}'")
        if observed.request is not None:
            saved = dict(observed.request)
            incoming = request.to_dict()
            for key in ("input_root", "work_dir"):
                saved.pop(key, None)
                incoming.pop(key, None)
            if saved != incoming:
                raise ValueError("resume request does not match the saved request")
        checkpoint = _checkpoint_from(observed)
        if checkpoint is None:
            return observed, None
        if not matches(
            checkpoint, source=request.source, model=request.model,
            language=request.language, engine=request.engine, device=request.device,
            options=request.options(),
        ):
            raise ValueError("resume request does not match the saved checkpoint")
        return observed, checkpoint
    found = find_resumable_checkpoint(
        store, source=request.source, model=request.model,
        language=request.language, engine=request.engine, device=request.device,
        options=request.options(),
    )
    if found is None:
        return None
    job, checkpoint = found
    # The selection already read the row, but through an alias whose attempt may
    # have moved since. Re-observe under the lock so the pinned identity is the
    # one the claim will be judged against.
    observed = store.observe(job.id)
    if observed is None:
        return None
    return observed, checkpoint


def _checkpoint_from(observed: ObservedJob):
    """Load the checkpoint a snapshot carries, or None if it has none."""
    if observed.checkpoint is None:
        return None
    return load_checkpoint(_snapshot_as_job(observed))


def _snapshot_as_job(observed: ObservedJob) -> Job:
    """A throwaway `Job` view of an immutable observation, for load helpers.

    `load_checkpoint` accepts a `Job`; the observation carries every field it
    reads. Building a fresh, private `Job` (never the store's live row) keeps the
    load reading a stable value rather than an alias a concurrent claim can
    mutate.
    """
    return Job(
        id=observed.id,
        source=observed.source,
        state=observed.state,
        checkpoint=observed.checkpoint,
        request=observed.request,
        attempt=observed.attempt,
    )


def _resume_conflict(store: JobStore, job_id: str) -> ValueError:
    """The error a caller gets when it loses the ownership claim for a resume.

    The claim is atomic, so a None result means another caller owns the job or
    the job is no longer claimable. Re-reading the row turns that into an
    actionable message: "already active" for the common race (parsed as a 409 by
    the HTTP resume route), a disappearance note for an evicted row. Both are
    ValueError, matching the refusal the submission contract already raises.
    """
    current = store.get(job_id)
    if current is None:
        return ValueError(f"job '{job_id}' disappeared before resume")
    if current.state in {JobState.PENDING, JobState.RUNNING}:
        return ValueError(f"job '{job_id}' is already active")
    return ValueError(
        f"job '{job_id}' failed again since this resume decided to retry it; "
        f"resume again to act on the new failure (state: {current.state.value})"
    )


def _fail_unadmitted(
    store: JobStore, job_id: str, progress: str, error: str, *, claimed_attempt: int
) -> None:
    """Fail a reopened row the executor refused, without clobbering a newer claim.

    The caller's claim left the row PENDING with no worker queued for it (the
    queue was full, or the pool was shutting down). Leaving it PENDING would be a
    permanent orphan that a later resume reads as "already active". But the row
    could also have been re-claimed by another caller in the meantime, so the
    revert is conditional on the identity of *this* refusal's own claim.

    State alone is not enough: a claim acquires an identity at *acquisition*
    (see `JobStore.claim`), and a newer caller that reopened the row after a
    queued cancellation can leave it at PENDING with a *different* acquisition
    identity. The cleanup therefore names ``claimed_attempt`` - the identity its
    claim produced - and rewrites the row only while that exact identity still
    holds. A row that has moved on belongs to someone else's state, so it is left
    alone.
    """
    store.claim(
        job_id,
        allowed_states={JobState.PENDING},
        observed_attempt=claimed_attempt,
        state=JobState.ERROR,
        progress=progress,
        error=error,
    )


def _require_engine_ready(engine: str) -> None:
    """Refuse an engine whose optional package is absent, before any work.

    Called only on the paths that are about to create or queue a job - never at
    request construction. A completed job whose transcript can be reused is
    returned without ever touching an engine, so uninstalling the optional extra
    must not stop that reuse. On a resume it is called before the row is
    un-terminated, so a refusal cannot leave a job PENDING with nothing queued to
    run it. The engine *name* is already settled, more cheaply, in
    ``SubmissionRequest``; this adds the one check that needs an import.
    """
    require_engine(engine)


def submit_request(
    store: JobStore,
    request: SubmissionRequest,
    *,
    background: bool = True,
    resume: bool = False,
    resume_job_id: str | None = None,
) -> Job:
    """Submit or resume via the same durable job lifecycle on every surface."""
    executor = get_default_executor() if background else None
    if executor is not None and executor.store is store:
        # Reap old PENDING/RUNNING records before selecting one to resume.
        executor.start()
    found = _matching_checkpoint(store, request, resume_job_id) if resume or resume_job_id else None
    if found is not None:
        observed, checkpoint = found
        prior = store.get(observed.id)
        if prior is None:
            raise _resume_conflict(store, observed.id)
        # The pre-checks below decide *what to tell the caller* (already active,
        # no reusable checkpoint, DONE reuse). They must read the same instant the
        # claim will be judged against, so they read `observed.state` rather than
        # `prior.state`: on the memory store `prior` is a mutable alias, and a
        # concurrent reopen could move its state between this read and the claim,
        # handing the caller a message for a state it never saw. (`prior` itself
        # is still needed below for the DONE-reuse path, which wants the row's
        # outputs.) None of these reads is the ownership decision - the atomic
        # claim is - so a stale read can only mislead the message, never admit a
        # second owner; reading `observed` removes even that.
        if observed.state in {JobState.PENDING, JobState.RUNNING}:
            raise ValueError(f"job '{prior.id}' is already active")
        if is_local_source(request.source):
            if checkpoint is None:
                if observed.state is JobState.DONE:
                    raise ValueError("local job has no reusable checkpoint; resubmit without resume")
            else:
                validate_local_resume(
                    checkpoint, request.source,
                    input_root=request.input_root if request.input_root is not None
                    else default_input_root(),
                )
        if observed.state is JobState.DONE:
            if request.output_dir is None:
                return prior
            result = reusable_done_result(store, prior, formats=request.formats,
                                          output_dir=request.output_dir,
                                          stem=_output_stem(prior))
            if result is not None:
                _transcript, outputs = result
                updated = store.update(prior.id, outputs=[str(p) for p in outputs])
                return updated or prior
        # Reuse of this *job* has been ruled out above, so the resume is about to
        # create or queue work. The check goes before `prepare_resume` and the
        # reopen below, because both un-terminal the row: refusing afterwards
        # would leave a job PENDING with its error and cancellation flag already
        # cleared and no worker ever queued for it. A checkpoint that already
        # holds a finished transcript can still skip the engine deeper in the
        # pipeline; that is the pipeline's decision, made after the row is
        # reopened, and second-guessing it here would mean duplicating its resume
        # logic in the submission contract.
        _require_engine_ready(request.engine)
        # The reopen is the ownership claim, so both branches take it through the
        # store's conditional transition: `prepare_resume` for a checkpointed
        # job, and the same atomic claim here for a job without one. Reopening
        # with `store.update` after the state was read above would let two
        # concurrent resumes of one job id both pass the terminal check and both
        # run. A caller that loses the race gets None and is refused below rather
        # than handed a second run.
        # Pin the *identity* the caller decided against (`observed.attempt`, taken
        # under the store lock at the matching read). A claim acquires its own
        # identity at acquisition - the counter advances here, not only when a
        # worker starts - so a decision formed against an earlier observation is
        # refused once the row has moved on, even if it never reached RUNNING in
        # between (an admission refusal, or a queued-cancellation-and-reclaim).
        if checkpoint is None:
            reopened = store.claim(
                prior.id, allowed_states=RESUMABLE_CLAIM_STATES,
                observed_attempt=observed.attempt,
                state=JobState.PENDING, progress="resuming from start",
                error=None, cancel_requested=False, advance_attempt=True,
            )
            prepared = (reopened, None) if reopened is not None else None
        else:
            prepared = prepare_resume(store, prior, checkpoint,
                                      observed_attempt=observed.attempt)
        if prepared is None:
            raise _resume_conflict(store, prior.id)
        if prepared is not None:
            job, checkpoint_payload = prepared
            # The identity this refusal's claim acquired, named so the cleanup
            # rewrites only the row this caller owns - never a newer reclaim that
            # shares the PENDING state but not the identity.
            claimed_attempt = job.attempt
            kwargs = request.run_kwargs()
            store.update(job.id, request=request.to_dict())
            if checkpoint_payload is not None:
                kwargs["resume_checkpoint"] = checkpoint_payload
            if executor is not None and executor.store is store:
                try:
                    return executor.enqueue(job, **kwargs)
                except QueueFullError:
                    _fail_unadmitted(store, job.id, "queue full",
                                    "resume not queued; job queue is full",
                                    claimed_attempt=claimed_attempt)
                    raise
                except RuntimeError as exc:
                    # The pool refused for a reason other than a full queue (it is
                    # shutting down, or the id is somehow already owned). The row
                    # was reopened by our claim and has no worker, so it must not
                    # be left PENDING; fail it without touching a newer attempt.
                    _fail_unadmitted(store, job.id, "not queued", str(exc),
                                     claimed_attempt=claimed_attempt)
                    raise
            run_job(job, store, **kwargs)
            return store.get(job.id) or job

    _require_engine_ready(request.engine)
    kwargs = request.run_kwargs()
    if executor is not None and executor.store is store:
        return executor.submit(request=request.to_dict(), **kwargs)
    job = store.create(request.source, request=request.to_dict())
    run_job(job, store, **kwargs)
    return store.get(job.id) or job


def resume_job(
    store: JobStore, job_id: str, *, background: bool = True,
    input_root: str | None = None, work_dir: str | None = None,
) -> Job:
    """Resume an interrupted durable job using its original saved request."""
    job = store.get(job_id)
    if job is None:
        raise ValueError(f"no job with id '{job_id}'")
    if job.request is None:
        raise ValueError("job predates saved requests; resubmit its original options")
    request_data = dict(job.request)
    if input_root is not None:
        request_data["input_root"] = input_root
    if work_dir is not None:
        request_data["work_dir"] = work_dir
    return submit_request(
        store, SubmissionRequest.from_dict(request_data),
        background=background, resume=True, resume_job_id=job_id,
    )


def submit_batch(
    store: JobStore, requests: list[SubmissionRequest], *, resume: bool = False
) -> list[dict[str, Any]]:
    """Accept each source independently; a bad item never hides later items.

    An engine whose optional package is missing is the one exception, and only
    for a fresh batch: it is a property of the request rather than of one
    source, so every item naming it fails identically. Checking it once here,
    before the loop, is what keeps a batch from queueing half its items and
    erroring the rest for the same reason. A *resume* batch skips this because
    reuse is decided per item - a completed transcript needs no engine - and
    `submit_request` covers the items that do run.
    """
    if not resume:
        for engine in dict.fromkeys(request.engine for request in requests):
            require_engine(engine)
    results: list[dict[str, Any]] = []
    for request in requests:
        try:
            job = submit_request(store, request, resume=resume)
            results.append({"source": request.source, "job_id": job.id, "state": job.state.value})
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            results.append({"source": request.source, "error": f"{type(exc).__name__}: {exc}"})
    return results


def _output_stem(job: Job) -> str:
    if job.outputs:
        return Path(job.outputs[0]).stem
    parsed = urlparse(job.source)
    name = Path(parsed.path).stem if parsed.scheme in {"http", "https"} else Path(job.source).stem
    return f"{name or 'transcript'}-{job.id}"
