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

import sys
from typing import Any

from textflowkit import __version__
from textflowkit.core.bind import ENV_ALLOW_REMOTE, UnsafeBindError, check_bind_safety
from textflowkit.core.executor import QueueFullError, get_default_executor
from textflowkit.core.jobs import Job, JobState, get_default_store, validate_list_limit
from textflowkit.core.model import Transcript
from textflowkit.core.paths import (
    UnsafeOutputPathError,
    ensure_output_dir,
    server_input_root,
)
from textflowkit.core.retrieval import page_segments, search_segments
from textflowkit.core.runner import transcript_for
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
    diarize: bool = False,
    translate_to: str | None = None,
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
        diarize: Label speakers. Requires the optional diarize extra and a gated
            Hugging Face model; the job fails with a clear error if unavailable
            rather than returning empty speakers.
        translate_to: Target language code (e.g. 'es'). Translates the transcript
            with the configured backend; fails loudly if it is unreachable.
    """
    fmt_list = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
    bad = [f for f in fmt_list if f not in SUPPORTED_FORMATS]
    if bad:
        return {
            "error": f"unsupported format(s): {', '.join(bad)}",
            "available_formats": list(SUPPORTED_FORMATS),
        }

    try:
        request = SubmissionRequest(
            source=source,
            language=language,
            formats=fmt_list,
            output_dir=output_dir,
            model=model,
            device=device,
            cookies_from_browser=cookies_from_browser,
            input_root=server_input_root(),
            diarize=diarize,
            translate_to=translate_to,
        )
        job = submit_request(get_default_store(), request)
    except ValueError as exc:
        return {"error": str(exc)}
    except QueueFullError as exc:
        return {"error": str(exc), "retryable": True}
    return {
        "job_id": job.id,
        "state": job.state.value,
        "source": job.source,
        "next": f"Poll get_job_status with job_id='{job.id}' until state is 'done'.",
    }


@mcp.tool(annotations=OPEN_WORLD)
def submit_batch_media(
    sources: list[str],
    language: str | None = None,
    formats: str = "json,srt,txt",
    output_dir: str | None = None,
    model: str = "small",
    device: str | None = None,
    diarize: bool = False,
    translate_to: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Queue multiple independent media jobs and return each job handle."""
    fmt_list = [f.strip().lower().lstrip(".") for f in formats.split(",") if f.strip()]
    try:
        requests = [SubmissionRequest(
            source=source, language=language, formats=fmt_list,
            output_dir=output_dir, model=model, device=device,
            diarize=diarize, translate_to=translate_to,
            input_root=server_input_root(),
        ) for source in sources]
    except ValueError as exc:
        return {"error": str(exc)}
    results = submit_batch(get_default_store(), requests, resume=resume)
    return {"count": len(results), "jobs": results}


@mcp.tool(annotations=MUTATING)
def resume_job(job_id: str) -> dict[str, Any]:
    """Resume an interrupted job using its saved request and transcript checkpoint."""
    try:
        job = core_resume_job(get_default_store(), job_id)
    except QueueFullError as exc:
        return {"error": str(exc), "retryable": True}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"job_id": job.id, "state": job.state.value}


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
    offset: int = 0,
    limit: int | None = None,
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    """Read the transcript for a completed job, optionally a slice of it.

    For long transcripts do not request everything: page with offset/limit, or
    ask for a time range with start/end (seconds). The response reports
    total_segments and has_more so you know whether to continue.

    Args:
        job_id: The id returned by transcribe_media.
        fmt: How to render the text - txt, srt, vtt, md, or json.
        offset: Skip this many segments within the selected range.
        limit: Return at most this many segments.
        start: Only segments ending at or after this time (seconds).
        end: Only segments starting at or before this time (seconds).
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
    if norm not in TEXT_FORMATS:
        # docx/pdf are export-only: they are binary and cannot be returned as
        # inline text. Say so rather than failing deeper down.
        return {
            "error": f"'{fmt}' cannot be returned inline",
            "available_formats": list(TEXT_FORMATS),
            "hint": "binary formats (docx, pdf) are written to disk with export_transcript",
        }

    try:
        page = page_segments(tr, offset=offset, limit=limit, start=start, end=end)
    except ValueError as exc:
        return {"error": str(exc)}

    sliced = Transcript(
        source=tr.source,
        language=tr.language,
        platform=tr.platform,
        duration=tr.duration,
        engine=tr.engine,
        metadata=tr.metadata,
        segments=page.segments,
    )
    payload = {
        "job_id": job.id,
        "format": norm,
        "language": tr.language,
        "platform": tr.platform,
        **page.as_dict(),
        "content": render(sliced, norm),
    }
    if page.has_more:
        payload["next"] = (
            f"More segments remain. Request offset={page.offset + page.returned} "
            f"for the next page."
        )
    return payload


@mcp.tool(annotations=READ_ONLY)
def search_transcript(
    job_id: str,
    query: str,
    limit: int = 20,
    context: int = 1,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Search a completed transcript for a phrase.

    Returns matching segments with their timestamps, newest-first order
    preserved from the transcript. Use this instead of paging through a long
    transcript looking for a topic.

    Args:
        job_id: The id returned by transcribe_media.
        query: Substring to find (not fuzzy).
        limit: Maximum number of matches to return.
        context: How many neighbouring segments to include either side.
        case_sensitive: Match case-sensitively (default false).
    """
    job, err = _resolve_job(job_id)
    if err:
        return {"error": err}
    assert job is not None
    if job.state is not JobState.DONE:
        return {
            "error": f"job is not finished (state: {job.state.value})",
            "state": job.state.value,
        }
    tr = transcript_for(job)
    if tr is None:
        return {"error": "job completed but contains no transcript"}

    try:
        matches = search_segments(
            tr, query, limit=limit, context=context, case_sensitive=case_sensitive
        )
    except ValueError as exc:
        return {"error": str(exc)}

    return {
        "job_id": job.id,
        "query": query,
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
    if len(fmt_list) != len(set(fmt_list)):
        return {"error": "duplicate output format"}
    try:
        rendered = [(f, render_bytes(tr, f, title=job.id)) for f in fmt_list]
    except (ValueError, ImportError) as exc:
        return {"error": str(exc)}

    try:
        out = ensure_output_dir(output_dir)
    except UnsafeOutputPathError as exc:
        return {"error": str(exc)}

    written = []
    for f, content in rendered:
        path = out / f"{job.id}.{f}"
        atomic_write_bytes(path, content, replace=True)
        written.append(str(path))
    return {"job_id": job.id, "written": written}


@mcp.tool(annotations=READ_ONLY)
def list_jobs(limit: int = 20, state: str | None = None) -> dict[str, Any]:
    """List recent transcription jobs, newest first.

    Args:
        limit: Maximum number of jobs to return.
        state: Optional filter - pending, running, done, error, or cancelled.
    """
    try:
        validate_list_limit(limit)
    except ValueError as exc:
        return {"error": str(exc)}
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
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "permit binding HTTP beyond loopback. This surface has no "
            f"authentication. ({ENV_ALLOW_REMOTE}=1 also works)"
        ),
    )
    parser.add_argument("--version", action="version", version=f"textflowkit-mcp {__version__}")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        run_stdio()
    else:
        try:
            check_bind_safety(args.host, allow_remote=args.allow_remote or None)
        except UnsafeBindError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        run_http(host=args.host, port=args.port, path=args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




