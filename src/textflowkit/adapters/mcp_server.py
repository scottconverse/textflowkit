"""MCP server adapter.

Exposes the textflowkit core over the Model Context Protocol.

Transport decision (2026-09-21): stdio first, Streamable HTTP second. Both are
served from this one module so the tool definitions cannot drift apart. Every
tool calls straight into `core.runner`; no pipeline logic lives here.

Verified harness support:
  - DSH (@deepseek-ai/dsh-mcp-client) - stdio + streamable-http
  - Claude Code                       - stdio + http (+ sse)
  - Codex CLI                         - stdio (add via `codex mcp add`)
  - OpenCode                          - remote (http) + local (stdio)

Tools are named `transcribe_media`, `get_transcript`, `list_jobs`,
`get_job_status`, and `export_transcript`, and are deliberately job-based: a
long video returns a job id immediately rather than blocking the call.
"""

from __future__ import annotations

from typing import Any

from textflowkit import __version__
from textflowkit.core.executor import get_default_executor
from textflowkit.core.jobs import Job, JobState, get_default_store
from textflowkit.core.paths import UnsafeOutputPathError, ensure_output_dir
from textflowkit.core.runner import submit, transcript_for
from textflowkit.render import SUPPORTED_FORMATS, render
from textflowkit.sources.detect import PLATFORMS

try:  # the MCP SDK is an optional extra
    # mcp 2.x renamed FastMCP to MCPServer (mcp.server.mcpserver).
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "The MCP adapter requires the 'mcp' extra. Install with: pip install 'textflowkit[mcp]'"
    ) from exc

# Tools that only read state.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
# Tools that reach the network or write files.
OPEN_WORLD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
# Tools that change local job state without touching the network or disk.
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


INSTRUCTIONS = """\
Transcribe media from a URL or local file into timestamped text and subtitles.

Typical flow:
  1. transcribe_media(source=...)  -> returns a job id
  2. get_job_status(job_id=...)    -> poll until state is done
  3. get_transcript(job_id=...)    -> read the transcript (or export_transcript for files)

Transcription runs locally. Long videos are processed in the background, so never
block on transcribe_media; poll the job instead.
"""

mcp = MCPServer("textflowkit", instructions=INSTRUCTIONS, version=__version__)


def _job_payload(job: Job) -> dict[str, Any]:
    return job.to_dict()


def _resolve_job(job_id: str) -> tuple[Job | None, str | None]:
    store = get_default_store()
    job = store.get(job_id)
    if job is None:
        return None, f"error: no job with id '{job_id}'. Use list_jobs to see known jobs."
    return job, None


@mcp.tool(annotations=READ_ONLY)
def list_sources() -> dict[str, Any]:
    """List the media platforms and input kinds textflowkit can transcribe.

    Returns recognised platform names plus 'local' (filesystem paths) and
    'direct' (direct media URLs).
    """
    return {
        "platforms": sorted(PLATFORMS),
        "input_kinds": ["local", "direct"],
        "formats": list(SUPPORTED_FORMATS),
    }


@mcp.tool(annotations=OPEN_WORLD)
def transcribe_media(
    source: str,
    language: str | None = None,
    formats: str = "json,srt,txt",
    output_dir: str | None = None,
    model: str = "small",
    device: str | None = None,
    cookies_from_browser: str | None = None,
) -> dict[str, Any]:
    """Start transcribing a media URL or local file. Returns immediately with a job id.

    The work runs in the background; poll get_job_status until state is 'done',
    then read get_transcript. Do not expect a transcript in this response.

    Args:
        source: A media URL (YouTube, TikTok, Facebook, Instagram, Vimeo,
            Twitch, Bilibili, Rumble, Kick, Zoom, Medal, Loom, Dropbox, or a
            direct media link) or a path to a local file.
        language: Optional ISO language code (e.g. 'en'). Auto-detected if omitted.
        formats: Comma-separated outputs to write when output_dir is set.
            Available: txt, srt, vtt, md, json.
        output_dir: Directory to write rendered files into. Omit to keep the
            transcript in memory only.
        model: Whisper model size - tiny, base, small, medium, or large.
            Larger is more accurate and slower. Default small.
        device: Torch device ('cuda' or 'cpu'). Auto-detected when omitted.
        cookies_from_browser: Pass cookies to yt-dlp from a browser, e.g.
            'firefox'. Only for media you are authorised to access.
    """
    fmt_list = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
    bad = [f for f in fmt_list if f not in SUPPORTED_FORMATS]
    if bad:
        return {
            "error": f"unsupported format(s): {', '.join(bad)}",
            "available_formats": list(SUPPORTED_FORMATS),
        }

    store = get_default_store()
    job = submit(
        store,
        source=source,
        language=language,
        formats=fmt_list,
        output_dir=output_dir,
        model=model,
        device=device,
        cookies_from_browser=cookies_from_browser,
        work_dir=None,
    )
    return {
        "job_id": job.id,
        "state": job.state.value,
        "source": job.source,
        "next": f"Poll get_job_status with job_id='{job.id}' until state is 'done'.",
    }


@mcp.tool(annotations=READ_ONLY)
def get_job_status(job_id: str) -> dict[str, Any]:
    """Check the state of a transcription job.

    Args:
        job_id: The id returned by transcribe_media.
    """
    job, err = _resolve_job(job_id)
    if err:
        return {"error": err}
    assert job is not None
    payload = _job_payload(job)
    if job.state is JobState.DONE:
        payload["next"] = f"Read the transcript with get_transcript(job_id='{job.id}')."
    elif job.state is JobState.ERROR:
        payload["next"] = "The job failed; see the 'error' field."
    else:
        payload["next"] = "Still working. Poll again."
    return payload


@mcp.tool(annotations=READ_ONLY)
def get_transcript(
    job_id: str,
    fmt: str = "txt",
) -> dict[str, Any]:
    """Read the transcript for a completed job.

    Args:
        job_id: The id returned by transcribe_media.
        fmt: How to render the text - txt, srt, vtt, md, or json.
    """
    job, err = _resolve_job(job_id)
    if err:
        return {"error": err}
    assert job is not None
    if job.state is not JobState.DONE:
        return {
            "error": f"job is not finished (state: {job.state.value})",
            "state": job.state.value,
            "next": "Poll get_job_status until state is 'done'.",
        }
    tr = transcript_for(job)
    if tr is None:
        return {"error": "job completed but contains no transcript"}

    norm = fmt.lower().lstrip(".")
    if norm not in SUPPORTED_FORMATS:
        return {"error": f"unsupported format '{fmt}'", "available_formats": list(SUPPORTED_FORMATS)}
    content = render(tr, norm)
    return {
        "job_id": job.id,
        "format": norm,
        "language": tr.language,
        "platform": tr.platform,
        "segments": len(tr.segments),
        "content": content,
    }


@mcp.tool(annotations=OPEN_WORLD)
def export_transcript(
    job_id: str,
    output_dir: str,
    formats: str = "srt,vtt,txt,json",
) -> dict[str, Any]:
    """Write a completed transcript to files on disk.

    Args:
        job_id: The id returned by transcribe_media.
        output_dir: Directory to write into. Created if missing.
        formats: Comma-separated formats to write - txt, srt, vtt, md, json.
    """
    job, err = _resolve_job(job_id)
    if err:
        return {"error": err}
    assert job is not None
    if job.state is not JobState.DONE:
        return {"error": f"job is not finished (state: {job.state.value})"}

    tr = transcript_for(job)
    if tr is None:
        return {"error": "job contains no transcript"}

    fmt_list = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
    bad = [f for f in fmt_list if f not in SUPPORTED_FORMATS]
    if bad:
        return {"error": f"unsupported format(s): {', '.join(bad)}"}

    try:
        out = ensure_output_dir(output_dir)
    except UnsafeOutputPathError as exc:
        return {"error": str(exc)}

    written = []
    for f in fmt_list:
        path = out / f"{job.id}.{f}"
        path.write_text(render(tr, f, title=job.id), encoding="utf-8")
        written.append(str(path))
    return {"job_id": job.id, "written": written}


@mcp.tool(annotations=READ_ONLY)
def list_jobs(limit: int = 20, state: str | None = None) -> dict[str, Any]:
    """List recent transcription jobs, newest first.

    Args:
        limit: Maximum number of jobs to return.
        state: Optional filter - pending, running, done, error, or cancelled.
    """
    store = get_default_store()
    filter_state = None
    if state:
        try:
            filter_state = JobState(state.lower())
        except ValueError:
            return {
                "error": f"unknown state '{state}'",
                "available_states": [s.value for s in JobState],
            }
    jobs = store.list(limit=limit, state=filter_state)
    return {"count": len(jobs), "jobs": [j.to_dict() for j in jobs]}


@mcp.tool(annotations=MUTATING)
def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation of a pending or running job.

    A queued job is cancelled immediately and never starts. A running job stops
    at its next stage boundary, so it stays 'running' with cancel_requested set
    until the boundary is reached. Poll get_job_status until state is
    'cancelled'.

    Args:
        job_id: The id returned by transcribe_media.
    """
    job, err = _resolve_job(job_id)
    if err:
        return {"error": err}
    assert job is not None

    if job.is_terminal:
        return {
            "job_id": job_id,
            "cancelled": False,
            "state": job.state.value,
            "reason": f"job is already {job.state.value}",
        }

    executor = get_default_executor()
    accepted = executor.cancel(job_id)
    latest = executor.store.get(job_id)
    state = latest.state.value if latest is not None else job.state.value

    next_step = (
        "Job cancelled."
        if state == "cancelled"
        else "Cancellation requested; poll get_job_status until state is 'cancelled'."
    )
    return {
        "job_id": job_id,
        "cancelled": accepted,
        "state": state,
        "next": next_step,
    }


def run_stdio() -> None:
    """Run the server over stdio (the default for local harnesses)."""
    mcp.run(transport="stdio")


def run_http(host: str = "127.0.0.1", port: int = 8766, path: str = "/mcp") -> None:
    """Run the server over Streamable HTTP (for remote/multi-client use).

    Binds to localhost by default. Exposing this beyond localhost requires your
    own auth layer; the server ships none.
    """
    mcp.run(transport="streamable-http", host=host, port=port, streamable_http_path=path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="textflowkit-mcp",
        description="textflowkit MCP server (stdio by default).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="Transport to serve on (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for http (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8766, help="Bind port for http (default 8766)")
    parser.add_argument("--path", default="/mcp", help="URL path for http (default /mcp)")
    parser.add_argument("--version", action="version", version=f"textflowkit-mcp {__version__}")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        run_stdio()
    else:
        run_http(host=args.host, port=args.port, path=args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




