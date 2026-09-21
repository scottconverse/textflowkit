"""HTTP adapter.

A small JSON API over the same core and job store the MCP adapter uses. This is
the door for software products and for the eventual website: it is deliberately
job-based so a long video never blocks a request.

Not started by default. There is no authentication here by design - bind it to
localhost, or put it behind your own gateway before exposing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textflowkit import __version__
from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.core.runner import submit, transcript_for
from textflowkit.render import SUPPORTED_FORMATS, render

try:  # optional extra
    from fastapi import FastAPI, HTTPException
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
    speaker_labels: bool = False
    cookies_from_browser: str | None = None


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
    job = submit(
        store,
        source=req.source,
        language=req.language,
        formats=[f.lower().lstrip(".") for f in req.formats],
        output_dir=req.output_dir,
        model=req.model,
        device=req.device,
        speaker_labels=req.speaker_labels,
        cookies_from_browser=req.cookies_from_browser,
    )
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


@app.get("/jobs/{job_id}/transcript")
def get_transcript(job_id: str, format: str = "json"):
    """Transcript for a completed job, rendered in the requested format."""
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

    fmt = format.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=422,
            detail={"error": f"unsupported format '{format}'",
                    "available_formats": list(SUPPORTED_FORMATS)},
        )
    content = render(tr, fmt)
    if fmt == "json":
        return {"job_id": job.id, "transcript": tr.to_dict()}
    if fmt in ("srt", "vtt", "txt"):
        return PlainTextResponse(content)
    return PlainTextResponse(content)


@app.post("/jobs/{job_id}/export")
def export(job_id: str, formats: list[str] | None = None, output_dir: str = ".") -> dict[str, Any]:
    """Write a completed transcript to disk."""
    job = get_default_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job with id '{job_id}'")
    if job.state is not JobState.DONE:
        raise HTTPException(status_code=409, detail={"error": "job not finished"})
    tr = transcript_for(job)
    if tr is None:
        raise HTTPException(status_code=500, detail="job contains no transcript")

    fmt_list = formats or ["srt", "vtt", "txt", "json"]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for f in fmt_list:
        norm = f.lower().lstrip(".")
        if norm not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail=f"unsupported format '{f}'")
        path = out / f"{job.id}.{norm}"
        path.write_text(render(tr, norm, title=job.id), encoding="utf-8")
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
    parser.add_argument("--version", action="version", version=f"textflowkit-http {__version__}")
    args = parser.parse_args(argv)

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
