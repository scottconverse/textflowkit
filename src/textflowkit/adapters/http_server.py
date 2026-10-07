"""HTTP adapter.

A small JSON API over the same core and job store the MCP adapter uses. This is
the door for software products and for the eventual website: it is deliberately
job-based so a long video never blocks a request.

Not started by default. Developer mode is unauthenticated and loopback-only by
default: each request must carry a loopback Host and, if present, a loopback
Origin, and must arrive from a loopback peer unless TEXTFLOWKIT_ALLOW_REMOTE is
set. The opt-in JSON HTTP production profile requires Bearer authentication and
a trusted egress proxy for URL jobs; Streamable-HTTP MCP is separate.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

from textflowkit import __version__
from textflowkit.core.bind import (
    ENV_ALLOW_REMOTE,
    UnsafeBindError,
    check_bind_safety,
    developer_request_refusal,
    resolve_client_identity,
)
from textflowkit.core.engine import DEFAULT_ENGINE
from textflowkit.core.executor import (
    QueueFullError,
    get_default_executor,
    shutdown_default_executor,
)
from textflowkit.core.jobs import JobState, get_default_store, validate_list_limit
from textflowkit.core.model import Transcript
from textflowkit.core.paths import (
    UnsafeOutputPathError,
    ensure_output_dir,
    server_input_root,
)
from textflowkit.core.retrieval import page_segments, search_segments
from textflowkit.core.runner import transcript_for
from textflowkit.core.service import (
    ENV_API_TOKEN,
    ENV_MAX_REQUEST_BYTES,
    ENV_RATE_PER_MINUTE,
    ServiceConfigurationError,
    enforce_output_limit,
    positive_limit,
    production_enabled,
    service_work_root,
    validate_production_config,
)
from textflowkit.core.startup import recover_startup
from textflowkit.core.streaming import cancel_all_sessions
from textflowkit.core.submission import (
    SubmissionRequest,
    item_source,
    submit_batch,
    submit_request,
)
from textflowkit.core.submission import (
    resume_job as core_resume_job,
)
from textflowkit.render import (
    DEFAULT_FORMATS,
    SUPPORTED_FORMATS,
    TEXT_FORMATS,
    atomic_write_bytes,
    render,
    render_requested,
)

try:  # optional extra
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
    from pydantic import BaseModel, Field, ValidationError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The HTTP adapter requires the 'http' extra. Install with: pip install 'textflowkit[http]'"
    ) from exc


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run startup recovery before the app serves any request.

    A durable store can hold jobs a previous process left PENDING/RUNNING. After
    a restart there is no worker for them, so they must be failed *before* the
    first read is served - otherwise ``/health`` and ``/jobs`` report a job that
    will never finish (the audit's QA-001). Recovery is delegated to the shared
    per-store owner, so it runs exactly once even if a worker's lazy start also
    reaches it; a reap that cannot be persisted raises and the app fails to
    start rather than serving a false ready.
    """
    recover_startup()
    try:
        yield
    finally:
        # Stop live streams first. Each owns a native child process, and a stream
        # left running while the job workers drain would hold its child open past
        # the point the server is supposed to be gone.
        cancel_all_sessions()
        # Drain the workers this process owns. Never creates an executor: a
        # server that ran no job has nothing to stop.
        shutdown_default_executor(wait=True)


app = FastAPI(
    title="textflowkit",
    version=__version__,
    description="Cross-platform media transcription API. Job-based: submit, poll, fetch.",
    lifespan=_lifespan,
)

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, tuple[float, int]] = {}
RATE_BUCKET_CAPACITY = 10000
RATE_BUCKET_TTL_SECONDS = 60.0
# Earliest monotonic time at which a tracked bucket can expire. A sweep is only
# eligible once it passes, so a table full of live peers costs one length check
# per request instead of an O(RATE_BUCKET_CAPACITY) walk. Every sweep relearns
# it from the buckets that remain. Guarded by _RATE_LOCK.
_next_expiry = float("inf")


def _sweep_expired_buckets(now: float) -> None:
    """Drop expired buckets and relearn when the next one can expire.

    Caller must hold _RATE_LOCK. A bucket restarted in place is younger than the
    one it replaces, so the relearned bound is exact and never too late.
    """
    global _next_expiry
    cutoff = now - RATE_BUCKET_TTL_SECONDS
    oldest = float("inf")
    for peer in list(_RATE_BUCKETS):
        started, _ = _RATE_BUCKETS[peer]
        if started <= cutoff:
            del _RATE_BUCKETS[peer]
        elif started < oldest:
            oldest = started
    _next_expiry = oldest + RATE_BUCKET_TTL_SECONDS


def _sweep_is_eligible(now: float) -> bool:
    """True when a bucket may have expired since the last sweep.

    Caller must hold _RATE_LOCK. _next_expiry is only maintained through
    _rate_refusal. Reading it as infinite while buckets exist means the table was
    written directly, so the bound is unknown and must be relearned rather than
    trusted - otherwise a stale claim of "nothing can have expired" would defer
    reclamation indefinitely.
    """
    return now >= _next_expiry or (_next_expiry == float("inf") and bool(_RATE_BUCKETS))


def _rate_refusal(peer: str, now: float, rate: int) -> str | None:
    """Account one request from `peer` at monotonic time `now`.

    Returns None when the request may proceed, else the refusal reason. Buckets
    are per-peer for RATE_BUCKET_TTL_SECONDS. At capacity an untracked peer is
    refused rather than evicting counters that are still counting down. Expired
    buckets are the only thing reclaimed, and only once the earliest known one
    has actually expired, so neither a live table nor a single expiry costs a
    scan per request.
    """
    global _next_expiry
    with _RATE_LOCK:
        known = peer in _RATE_BUCKETS
        started, count = _RATE_BUCKETS.get(peer, (now, 0))
        if now - started >= RATE_BUCKET_TTL_SECONDS:
            started, count = now, 0
        if count >= rate:
            return "rate limit exceeded"
        if not known and len(_RATE_BUCKETS) >= RATE_BUCKET_CAPACITY:
            if _sweep_is_eligible(now):
                _sweep_expired_buckets(now)
            if len(_RATE_BUCKETS) >= RATE_BUCKET_CAPACITY:
                return "rate capacity reached"
        if not known and not _RATE_BUCKETS:
            # Only bucket in an empty table: it is also the next to expire.
            _next_expiry = started + RATE_BUCKET_TTL_SECONDS
        _RATE_BUCKETS[peer] = started, count + 1
        return None


@app.middleware("http")
async def production_guard(request: Request, call_next):
    try:
        if not production_enabled():
            # Developer mode: loopback-only by Host, Origin, and peer address.
            # This also covers an app started directly through an ASGI server,
            # where main()'s bind check never runs.
            refusal = developer_request_refusal(
                host=request.headers.get("host"),
                origin=request.headers.get("origin"),
                peer=request.client.host if request.client else None,
            )
            if refusal is not None:
                return JSONResponse({"error": refusal}, status_code=403)
            return await call_next(request)
        validate_production_config()
    except ServiceConfigurationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {os.environ[ENV_API_TOKEN]}"
    if not hmac.compare_digest(supplied, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    max_bytes = positive_limit(ENV_MAX_REQUEST_BYTES, 64 * 1024)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > max_bytes):
        return JSONResponse({"error": "request body too large"}, status_code=413)
    # Do not call request.body() first: absent Content-Length, it buffers an
    # arbitrarily large chunked body before the check can run. Cache only after
    # incrementally enforcing the limit so call_next can replay it to FastAPI.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        body.extend(chunk)
    request._body = bytes(body)

    rate = positive_limit(ENV_RATE_PER_MINUTE, 60)
    # Behind a proxy every request shares the proxy's address. The identity is
    # the peer unless that peer is a trusted proxy this operator named, and even
    # then only a real address from the forwarded chain is used.
    # Every X-Forwarded-For line is joined, not just the first: a proxy may append
    # its own header rather than extend the caller's value, and reading only the
    # first would let a caller-supplied line stand in for the real client.
    peer = resolve_client_identity(
        request.client.host if request.client else None,
        ", ".join(request.headers.getlist("x-forwarded-for")),
    )
    refusal = _rate_refusal(peer, time.monotonic(), rate)
    if refusal is not None:
        return JSONResponse({"error": refusal}, status_code=429)
    return await call_next(request)


class TranscribeRequest(BaseModel):
    source: str = Field(..., description="Media URL or local file path")
    language: str | None = Field(None, description="ISO language code; auto-detected if omitted")
    formats: list[str] = Field(default_factory=lambda: list(DEFAULT_FORMATS))
    output_dir: str | None = Field(None, description="Directory for rendered files; omit for none")
    model: str | None = Field(None, description="Model name; defaults to the engine's own (whistle, or small for whisper)")
    device: str | None = Field(None, description="cuda or cpu; auto-detected if omitted (Whistle is CPU-only)")
    cookies_from_browser: str | None = None
    diarize: bool = False
    translate_to: str | None = None
    engine: str = Field(
        DEFAULT_ENGINE,
        description=(
            "Speech engine: 'whistle' (default, CPU-only, no torch), 'whisper' "
            "(openai-whisper on the torch stack; needs the whisper extra), or "
            "'faster-whisper' (CPU/Mac; needs the faster-whisper extra)"
        ),
    )


class BatchRequest(BaseModel):
    # `jobs` is deliberately `list[Any]`, not `list[TranscribeRequest]`: the
    # envelope contract is "a list" - a top-level `jobs` that is not a list at
    # all is refused by this model as a whole-request 422 - while each *entry* is
    # validated against `TranscribeRequest` inside the per-item boundary in
    # `create_batch` (via `_submission_request`). So a non-object entry, or one
    # with a malformed, missing, or wrongly-typed field, is that entry's item
    # error rather than a whole-request 422; only the envelope shape is
    # enforced here, before the handler runs.
    jobs: list[Any]
    resume: bool = False


def _submission_request(item: Any) -> SubmissionRequest:
    """Build one submission request from one decoded batch/job entry.

    The entry is validated as a `TranscribeRequest` here, inside the caller's
    per-item boundary, so a shape or value error on one batch entry surfaces as
    that entry's item error rather than aborting the whole list. Pydantic's
    `ValidationError` is normalised to `ValueError` so every door speaks the one
    refusal type the submission contract and the HTTP handler already map.
    """
    if isinstance(item, TranscribeRequest):
        req = item
    else:
        try:
            req = TranscribeRequest.model_validate(item)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
    return SubmissionRequest(
        **req.model_dump(), input_root=server_input_root(), work_dir=service_work_root()
    )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@app.get("/sources")
def sources() -> dict[str, Any]:
    """Capabilities: platforms, input kinds, output formats."""
    from textflowkit.sources.detect import PLATFORMS

    return {
        "platforms": sorted(PLATFORMS),
        "input_kinds": ["local", "direct"],
        "formats": list(SUPPORTED_FORMATS),
    }


@app.post("/jobs", status_code=202)
def create_job(req: TranscribeRequest) -> dict[str, Any]:
    """Submit a transcription job. Returns 202 with a job id immediately."""
    store = get_default_store()
    try:
        job = submit_request(store, _submission_request(req))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return job.to_dict()


@app.post("/jobs/batch", status_code=202)
def create_batch(req: BatchRequest) -> dict[str, Any]:
    """Queue multiple independent jobs through the same core contract.

    Each entry of `jobs` is admitted independently: an entry the submission
    contract refuses (an unsupported format, an empty source, a missing optional
    engine extra) is reported as its own item error, identified by its zero-based
    `index`, and the entries after it are still attempted. Only a malformed
    *envelope* - a body whose `jobs` is not a list (non-objects inside the list
    are per-entry errors, not envelope errors) - is a 422; the
    envelope shape itself is enforced by `BatchRequest` before this handler runs.
    """
    store = get_default_store()
    requests: list[SubmissionRequest] = []
    # One slot per submitted entry, in submission order: a rejected entry holds
    # its error outcome, an accepted entry a placeholder filled from the core
    # batch result below, so `jobs` mirrors the request list positionally.
    results: list[dict[str, Any] | None] = []
    for index, item in enumerate(req.jobs):
        try:
            request = _submission_request(item)
        except (TypeError, ValueError) as exc:
            results.append({
                "index": index,
                "source": item_source(item),
                "error": f"{type(exc).__name__}: {exc}",
            })
        else:
            requests.append(request)
            results.append(None)  # placeholder, replaced below in order
    accepted = submit_batch(store, requests, resume=req.resume)
    # `submit_batch` enumerates only the *accepted* requests, so an accepted
    # item's own `index` is its position in the compacted list, not the position
    # it was submitted at. Re-stamp every recombined result with its original
    # slot index so `index` always names the caller's input position, even when
    # rejected items sit before it.
    accepted_iter = iter(accepted)
    jobs = []
    for index, entry in enumerate(results):
        outcome = next(accepted_iter) if entry is None else entry
        jobs.append({**outcome, "index": index})
    return {"count": len(jobs), "jobs": jobs}


@app.post("/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str) -> dict[str, Any]:
    """Resume a durable interrupted job by its saved request and checkpoint."""
    try:
        job = core_resume_job(
            get_default_store(), job_id,
            input_root=str(server_input_root()) if server_input_root() else None,
            work_dir=service_work_root(),
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@app.get("/jobs")
def list_jobs(limit: int = 20, state: str | None = None) -> dict[str, Any]:
    """List recent jobs, newest first."""
    try:
        validate_list_limit(limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = get_default_store()
    filter_state = None
    if state:
        try:
            filter_state = JobState(state.lower())
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"error": f"unknown state '{state}'",
                        "available_states": [s.value for s in JobState]},
            ) from None
    jobs = store.list(limit=limit, state=filter_state)
    return {"count": len(jobs), "jobs": [j.to_dict() for j in jobs]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    """Lean job status; use the paged transcript endpoint for content."""
    job = get_default_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    return job.to_dict()


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation. 202-style: accepted, then poll for state.

    A queued job is cancelled immediately. A running job stops at its next stage
    boundary and reports state 'cancelled' once it does.
    """
    store = get_default_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    if job.is_terminal:
        return {
            "job_id": job_id,
            "cancelled": False,
            "state": job.state.value,
            "reason": f"job is already {job.state.value}",
        }

    accepted = get_default_executor().cancel(job_id)
    latest = store.get(job_id)
    return {
        "job_id": job_id,
        "cancelled": accepted,
        "state": latest.state.value if latest is not None else job.state.value,
    }


def _finished_transcript(job_id: str):
    """Shared guard: 404/409/500 and return (job, transcript)."""
    job = get_default_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    if job.state is not JobState.DONE:
        raise HTTPException(
            status_code=409,
            detail={"error": f"job not finished (state: {job.state.value})", "state": job.state.value},
        )
    tr = transcript_for(job)
    if tr is None:
        raise HTTPException(status_code=500, detail="job contains no transcript")
    return job, tr


@app.get("/jobs/{job_id}/transcript")
def get_transcript(
    job_id: str,
    format: str = "json",
    offset: int = 0,
    limit: int | None = None,
    start: float | None = None,
    end: float | None = None,
    include_words: bool = False,
):
    """Transcript for a completed job, optionally a slice.

    `offset`/`limit` page through segments; `start`/`end` select a time range in
    seconds. The JSON form reports total_segments and has_more. Word timings
    are omitted unless include_words is true; translated segments retain
    source-language word timings.
    """
    job, tr = _finished_transcript(job_id)
    if production_enabled():
        limit = 100 if limit is None else limit
        if limit > 500:
            raise HTTPException(status_code=422, detail="transcript page limit must be <= 500")

    fmt = format.lower().lstrip(".")
    if fmt not in TEXT_FORMATS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"'{format}' cannot be returned inline",
                "available_formats": list(TEXT_FORMATS),
                "hint": "binary formats (docx, pdf) are written to disk via /export",
            },
        )

    try:
        page = page_segments(tr, offset=offset, limit=limit, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sliced = Transcript(
        source=tr.source,
        language=tr.language,
        platform=tr.platform,
        duration=tr.duration,
        engine=tr.engine,
        metadata=tr.metadata,
        segments=page.segments,
    )

    if fmt == "json":
        response = {
            "job_id": job.id,
            **page.as_dict(),
            "transcript": sliced.to_dict(include_words=include_words),
        }
        try:
            enforce_output_limit(len(json.dumps(response, ensure_ascii=False).encode("utf-8")))
        except ServiceConfigurationError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return response
    content = render(sliced, fmt)
    try:
        enforce_output_limit(len(content.encode("utf-8")))
    except ServiceConfigurationError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return PlainTextResponse(content)


@app.get("/jobs/{job_id}/search")
def search(job_id: str, q: str, limit: int = 20, context: int = 1,
           case_sensitive: bool = False) -> dict[str, Any]:
    """Search a completed transcript for a phrase."""
    job, tr = _finished_transcript(job_id)
    if production_enabled() and (limit > 500 or context > 20):
        raise HTTPException(status_code=422, detail="search limit/context exceeds production cap")
    try:
        matches = search_segments(
            tr, q, limit=limit, context=context, case_sensitive=case_sensitive
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = {
        "job_id": job.id,
        "query": q,
        "match_count": len(matches),
        "matches": [
            {
                "index": m.index,
                "start": m.segment.start,
                "end": m.segment.end,
                "text": m.segment.display_text(),
                "context_before": [c.display_text() for c in m.context_before],
                "context_after": [c.display_text() for c in m.context_after],
            }
            for m in matches
        ],
    }
    try:
        enforce_output_limit(len(json.dumps(response, ensure_ascii=False).encode("utf-8")))
    except ServiceConfigurationError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return response


@app.post("/jobs/{job_id}/export")
def export(
    job_id: str,
    formats: Annotated[
        list[str] | None,
        Query(description="Repeat for each format, e.g. ?formats=docx&formats=pdf"),
    ] = None,
    output_dir: str = ".",
) -> dict[str, Any]:
    """Write a completed transcript to disk.

    `formats` is declared as an explicit query parameter: a bare `list[str]` on a
    POST is treated by FastAPI as a request *body* field, which meant the argument
    was silently ignored and the defaults were always used.
    """
    job = get_default_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    if job.state is not JobState.DONE:
        raise HTTPException(status_code=409, detail={"error": "job not finished"})
    tr = transcript_for(job)
    if tr is None:
        raise HTTPException(status_code=500, detail="job contains no transcript")

    fmt_list = formats or ["srt", "vtt", "txt", "json"]
    try:
        # `render_requested` is the shared preflight: it normalizes and
        # validates the ENTIRE format list first (so a later unsupported or
        # duplicate entry is refused before any renderer runs), then checks
        # every requested binary dependency, then renders all formats and
        # bounds the whole batch before anything is published. Only after it
        # returns the full batch does the publication loop below run, so a
        # refusal leaves no file created and no existing file replaced.
        rendered = render_requested(tr, fmt_list, title=job.id)
    except (ValueError, ImportError, ServiceConfigurationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        out = ensure_output_dir(output_dir)
    except UnsafeOutputPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    written = []
    for norm, content in rendered:
        path = out / f"{job.id}.{norm}"
        atomic_write_bytes(path, content, replace=True)
        written.append(str(path))
    return {"job_id": job.id, "written": written}


# --- live streaming (opt-in) ----------------------------------------------
#
# Live microphone streaming is added to this app only when opted in, and only
# as a wrapper the launcher serves: the WebSocket handshake is not seen by the
# ``production_guard`` HTTP middleware above (that runs for ``http`` scopes
# only), so the stream is guarded by its own pure-ASGI middleware in
# ``adapters.streaming_ws``. The rules it applies are the same ones this app's
# HTTP guard applies and more: loopback Host and a provably-loopback peer,
# always, and then either the configured API token (a non-browser client) or the
# UI capability (a browser the UI served the page to). None of it is enabled
# unless the operator asks for it.

from textflowkit.adapters.streaming_ws import (
    ENV_STREAMING as _STREAMING_ENV,
)
from textflowkit.adapters.streaming_ws import (
    StreamingConfig,
    StreamingRoute,
    WebSocketScopeGuard,
    streaming_enabled,
)

#: Just above the one-second audio frame the wire protocol allows (32000 bytes),
#: so an oversize frame is refused at the transport. See ``main``.
_WS_MAX_SIZE = 32768
#: A peer may have at most this many frames queued while the handler is busy.
_WS_MAX_QUEUE = 5


def streaming_origins_env() -> tuple[str, ...]:
    """Extra exact cross-origin allowances for streams, validated."""
    from textflowkit.adapters.streaming_ws import streaming_origins

    return streaming_origins()


def streaming_config(*, path: str = "/stream") -> StreamingConfig:
    """The streaming policy for this app.

    The standalone developer app is accessed by programmatic clients and - when a
    page is served from it - by a same-origin browser page, but it has no UI
    capability of its own. So a stream there is authenticated by the API Bearer
    token. In **developer mode** the token is the loopback developer sentinel
    (:data:`textflowkit.core.service.ENV_API_TOKEN` is not required in dev for
    other routes, but streaming always requires one, so the operator running a
    stream is explicit about who may open it): a stream is only ever served with a
    token configured, even in developer mode. In **production** the same
    ``validate_production_config`` rules already enforced on HTTP apply, and the
    stream refuses without a valid token exactly as HTTP does - there is no bypass.
    """
    token = os.environ.get(ENV_API_TOKEN)
    return StreamingConfig(
        own_origin=None,
        allowed_origins=streaming_origins_env(),
        require_api_token=True,
        api_token=token,
    )


def streaming_dependency_problem() -> str | None:
    """A human reason the streaming extra is missing, or ``None`` if it is present.

    Streaming rides on the WebSocket support of the ASGI server. This app ships
    uvicorn without a WebSocket implementation unless the ``streaming`` extra is
    installed, so a server started with streaming opted in but the extra absent
    would advertise an endpoint it cannot complete a handshake on. The launcher
    checks here and fails clearly rather than serving a broken route.
    """
    try:
        import websockets  # noqa: F401
    except ImportError:
        return (
            "live streaming requires the 'streaming' extra (websockets). "
            "Install with: pip install 'textflowkit[streaming]'"
        )
    return None


@app.get("/streaming-example")
def streaming_example() -> Response:
    """The runnable browser example, served only when streaming is on.

    A self-hosted page and its inline script; no CDN, no analytics, no external
    fetch. It is a GET asset the browser loads before any script runs, so it
    carries no capability header; the ``production_guard`` above still requires a
    loopback Host/Origin/peer. With streaming off this route is not registered at
    all, so it 404s like any unknown path.
    """
    if not streaming_enabled():
        return PlainTextResponse("not found", status_code=404)
    from textflowkit.ui.app import render_streaming_example

    # Standalone: no UI capability. The page falls back to the API-token field,
    # which the operator fills in with the same token the server was started with.
    html = render_streaming_example(capability="", origin="")
    if html is None:  # pragma: no cover - packaged asset must exist
        return PlainTextResponse("not found", status_code=404)
    return HTMLResponse(html)


@app.get("/assets/streaming-capture-worklet.js")
def streaming_capture_worklet() -> Response:
    """The packaged AudioWorklet the example loads, served same-origin.

    The page fetches this module with ``audioWorklet.addModule``. It is the
    packaged file itself (the same one the project tests with Node), served with
    a JavaScript content type and only when streaming is on, so the example
    needs no CDN and no synthesised blob module.
    """
    if not streaming_enabled():
        return PlainTextResponse("not found", status_code=404)
    from textflowkit.ui.app import read_static_asset

    asset = read_static_asset("streaming-capture-worklet.js")
    if asset is None:  # pragma: no cover - packaged asset must exist
        return PlainTextResponse("not found", status_code=404)
    return Response(asset, media_type="text/javascript; charset=utf-8")


class _StreamingWebSocketRoute:
    """A Starlette-shaped route serving one guarded stream path.

    Registered on the app's own router (not as a wrapper around the app) so that
    **any** ASGI server that serves the documented import target
    ``uvicorn textflowkit.adapters.http_server:app`` - not just the packaged
    ``textflowkit-http`` launcher - reaches the stream. ``matches`` claims only a
    websocket scope on this exact path and nothing else, so every HTTP route, its
    middleware, and its guards are untouched. ``handle`` runs the pure-ASGI guard,
    which validates loopback peer/Host and origin *before* the route allocates a
    session.
    """

    def __init__(self, *, path: str, config: StreamingConfig):
        self.path = path
        self._guarded = WebSocketScopeGuard(StreamingRoute(config), path=path, config=config)

    def matches(self, scope: dict) -> tuple[Any, dict]:
        from starlette.routing import Match

        if scope.get("type") == "websocket" and scope.get("path") == self.path:
            return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope: dict, receive: Any, send: Any) -> None:
        await self._guarded(scope, receive, send)


def install_streaming_route(
    host_app: Any = None, *, path: str = "/stream", config: StreamingConfig | None = None
) -> bool:
    """Add the guarded stream route to ``host_app`` (default: this module's app).

    Returns whether a route was installed. A second call for the *same* path on the
    same app replaces the earlier one, so the most recent config wins: this module's
    ``app`` is a process-global, and a host that builds two streaming front ends in
    one process (two UI servers, or a test suite) must not leave a stale route whose
    config carries a previous session's capability serving the newer path. Called at
    import time when streaming is opted into - so a directly-served
    ``uvicorn textflowkit.adapters.http_server:app`` carries the stream - and by the
    UI and the packaged launcher to add or refresh their own path.
    """
    target = app if host_app is None else host_app
    router = getattr(target, "router", None)
    if router is None:  # pragma: no cover - a non-Starlette host cannot take the route
        return False
    if config is None:
        config = streaming_config(path=path)
    replacement = _StreamingWebSocketRoute(path=path, config=config)
    router.routes[:] = [
        existing
        for existing in router.routes
        if not (isinstance(existing, _StreamingWebSocketRoute) and existing.path == path)
    ]
    router.routes.append(replacement)
    return True


def remove_streaming_route(host_app: Any = None, *, path: str | None = None) -> int:
    """Drop stream route(s) from ``host_app`` (default: this module's app).

    Returns how many were removed. Needed because ``app`` is a process-global: a
    host that opts streaming *off* after it was on must not leave a live,
    guard-passing route behind for the next server built in the same process. With
    ``path`` ``None`` every stream route is removed.
    """
    target = app if host_app is None else host_app
    router = getattr(target, "router", None)
    if router is None:  # pragma: no cover - a non-Starlette host cannot hold the route
        return 0
    before = len(router.routes)
    router.routes[:] = [
        existing
        for existing in router.routes
        if not (
            isinstance(existing, _StreamingWebSocketRoute)
            and (path is None or existing.path == path)
        )
    ]
    return before - len(router.routes)


# Opt in at import time: an operator who serves the documented target
# `uvicorn textflowkit.adapters.http_server:app` (or any ASGI server pointed at
# `textflowkit.adapters.http_server:app`) with TEXTFLOWKIT_STREAMING=1 gets the
# stream route without the packaged launcher. The packaged launchers call the
# installer explicitly as well, and it is idempotent.
if streaming_enabled():
    install_streaming_route()


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="textflowkit-http", description="textflowkit HTTP API"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8767, help="Bind port (default 8767)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "permit binding to a non-loopback address. Developer mode has no "
            "authentication; use a gateway or the production profile. "
            f"({ENV_ALLOW_REMOTE}=1 also works)"
        ),
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help=(
            "also serve the live-microphone /stream WebSocket and the "
            "/streaming-example browser page (opt-in; also TEXTFLOWKIT_STREAMING=1). "
            "Streaming is loopback-only and never widened by --allow-remote. "
            "Requires the 'streaming' extra (websockets)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"textflowkit-http {__version__}")
    args = parser.parse_args(argv)

    try:
        validate_production_config()
        check_bind_safety(args.host, allow_remote=args.allow_remote or None)
    except (UnsafeBindError, ServiceConfigurationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.streaming and not os.environ.get(_STREAMING_ENV):
        # Honor the CLI flag by setting the switch the app and launcher read, so a
        # server started this way is identical to one started with the env var.
        os.environ[_STREAMING_ENV] = "1"

    if streaming_enabled():
        problem = streaming_dependency_problem()
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        # Idempotent: the route was likely already added at import time. Calling it
        # again after the flag is set makes `--streaming` add the route even though
        # the module was imported before the switch was flipped.
        install_streaming_route(path="/stream", config=streaming_config())

    # uvicorn's own proxy-header middleware applies a *second* trust set of its
    # own - 127.0.0.1/::1 by default, or FORWARDED_ALLOW_IPS - and can rewrite the
    # peer before this app sees it. It is off so the app gets the raw peer and
    # TEXTFLOWKIT_TRUSTED_PROXY_IPS is the only trust configuration in play. The
    # reason is that duplication, not a weaker rule: its normal path also walks
    # the chain from the right. It reaches for the leftmost entry only when
    # configured to trust everything (--forwarded-allow-ips=*), or when every hop
    # in the chain is already trusted. Start the ASGI app directly and you own
    # that choice; see docs/adapters.md.
    #
    # The WebSocket bounds below apply whether or not streaming is on: they also
    # cap any future ws use and cost nothing when idle. ``ws_max_size`` is just
    # above the one-second audio frame the protocol allows, so an oversize frame
    # is refused at the transport before it reaches the handler; ``ws_max_queue``
    # is small so a peer cannot queue unbounded frames while backpressured. No
    # permessage compression: the payload is already-compact PCM and compressing
    # it would only spend CPU and open a decompression-bomb surface.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        ws_max_size=_WS_MAX_SIZE,
        ws_max_queue=_WS_MAX_QUEUE,
        ws_per_message_deflate=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

