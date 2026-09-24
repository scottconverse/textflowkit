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
from typing import Annotated, Any

from textflowkit import __version__
from textflowkit.core.bind import (
    ENV_ALLOW_REMOTE,
    UnsafeBindError,
    check_bind_safety,
    developer_request_refusal,
)
from textflowkit.core.executor import QueueFullError, get_default_executor
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
from textflowkit.core.submission import (
    SubmissionRequest,
    submit_batch,
    submit_request,
)
from textflowkit.core.submission import (
    resume_job as core_resume_job,
)
from textflowkit.render import (
    SUPPORTED_FORMATS,
    TEXT_FORMATS,
    atomic_write_bytes,
    render,
    render_bytes,
)

try:  # optional extra
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import JSONResponse, PlainTextResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The HTTP adapter requires the 'http' extra. Install with: pip install 'textflowkit[http]'"
    ) from exc

app = FastAPI(
    title="textflowkit",
    version=__version__,
    description="Cross-platform media transcription API. Job-based: submit, poll, fetch.",
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
    peer = request.client.host if request.client else "unknown"
    refusal = _rate_refusal(peer, time.monotonic(), rate)
    if refusal is not None:
        return JSONResponse({"error": refusal}, status_code=429)
    return await call_next(request)


class TranscribeRequest(BaseModel):
    source: str = Field(..., description="Media URL or local file path")
    language: str | None = Field(None, description="ISO language code; auto-detected if omitted")
    formats: list[str] = Field(default_factory=lambda: ["json", "srt", "txt"])
    output_dir: str | None = Field(None, description="Directory for rendered files; omit for none")
    model: str = Field("small", description="Whisper model size")
    device: str | None = Field(None, description="cuda or cpu; auto-detected if omitted")
    cookies_from_browser: str | None = None
    diarize: bool = False
    translate_to: str | None = None


class BatchRequest(BaseModel):
    jobs: list[TranscribeRequest]
    resume: bool = False


def _submission_request(req: TranscribeRequest) -> SubmissionRequest:
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
    """Queue multiple independent jobs through the same core contract."""
    try:
        requests = [_submission_request(item) for item in req.jobs]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    results = submit_batch(get_default_store(), requests, resume=req.resume)
    return {"count": len(results), "jobs": results}


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
    normalized = [f.lower().lstrip(".") for f in fmt_list]
    bad = [f for f in normalized if f not in SUPPORTED_FORMATS]
    if bad:
        raise HTTPException(status_code=422, detail=f"unsupported format(s): {', '.join(bad)}")
    if len(normalized) != len(set(normalized)):
        raise HTTPException(status_code=422, detail="duplicate output format")
    try:
        rendered = [(f, render_bytes(tr, f, title=job.id)) for f in normalized]
    except (ValueError, ImportError) as exc:
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
    parser.add_argument("--version", action="version", version=f"textflowkit-http {__version__}")
    args = parser.parse_args(argv)

    try:
        validate_production_config()
        check_bind_safety(args.host, allow_remote=args.allow_remote or None)
    except (UnsafeBindError, ServiceConfigurationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

