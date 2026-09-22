"""HTTP adapter.

A small JSON API over the same core and job store the MCP adapter uses. This is
the door for software products and for the eventual website: it is deliberately
job-based so a long video never blocks a request.

Not started by default. There is no authentication here by design - bind it to
localhost, or put it behind your own gateway before exposing it.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from textflowkit import __version__
from textflowkit.core.bind import ENV_ALLOW_REMOTE, UnsafeBindError, check_bind_safety
from textflowkit.core.executor import QueueFullError, get_default_executor
from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.core.model import Transcript
from textflowkit.core.paths import (
    UnsafeOutputPathError,
    ensure_output_dir,
    server_input_root,
)
from textflowkit.core.retrieval import page_segments, search_segments
from textflowkit.core.runner import submit, transcript_for
from textflowkit.render import SUPPORTED_FORMATS, TEXT_FORMATS, render, render_bytes

try:  # optional extra
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import PlainTextResponse
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
    bad = [f for f in req.formats if f.lower().lstrip(".") not in SUPPORTED_FORMATS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail={"error": f"unsupported format(s): {', '.join(bad)}",
                    "available_formats": list(SUPPORTED_FORMATS)},
        )

    store = get_default_store()
    try:
        job = submit(
            store,
            source=req.source,
            language=req.language,
            formats=[f.lower().lstrip(".") for f in req.formats],
            output_dir=req.output_dir,
            model=req.model,
            device=req.device,
            cookies_from_browser=req.cookies_from_browser,
            input_root=server_input_root(),
            diarize=req.diarize,
            translate_to=req.translate_to,
        )
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return job.to_dict()


@app.get("/jobs")
def list_jobs(limit: int = 20, state: str | None = None) -> dict[str, Any]:
    """List recent jobs, newest first."""
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
    """Job status. Includes the transcript only once the job is done."""
    job = get_default_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    return job.to_dict(include_transcript=job.state is JobState.DONE)


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
):
    """Transcript for a completed job, optionally a slice.

    `offset`/`limit` page through segments; `start`/`end` select a time range in
    seconds. The JSON form reports total_segments and has_more.
    """
    job, tr = _finished_transcript(job_id)

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
        return {
            "job_id": job.id,
            **page.as_dict(),
            "transcript": sliced.to_dict(),
        }
    return PlainTextResponse(render(sliced, fmt))


@app.get("/jobs/{job_id}/search")
def search(job_id: str, q: str, limit: int = 20, context: int = 1,
           case_sensitive: bool = False) -> dict[str, Any]:
    """Search a completed transcript for a phrase."""
    job, tr = _finished_transcript(job_id)
    try:
        matches = search_segments(
            tr, q, limit=limit, context=context, case_sensitive=case_sensitive
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
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
        out = ensure_output_dir(output_dir)
    except UnsafeOutputPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    written = []
    for f in fmt_list:
        norm = f.lower().lstrip(".")
        if norm not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail=f"unsupported format '{f}'")
        path = out / f"{job.id}.{norm}"
        path.write_bytes(render_bytes(tr, norm, title=job.id))
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
            "permit binding to a non-loopback address. This surface has no "
            f"authentication; only do this behind your own gateway. ({ENV_ALLOW_REMOTE}=1 also works)"
        ),
    )
    parser.add_argument("--version", action="version", version=f"textflowkit-http {__version__}")
    args = parser.parse_args(argv)

    try:
        check_bind_safety(args.host, allow_remote=args.allow_remote or None)
    except UnsafeBindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



