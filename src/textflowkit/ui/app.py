"""The local browser interface: a thin browser-session layer over the HTTP app.

This does **not** reimplement the API. It mounts the existing developer HTTP app
(:mod:`textflowkit.adapters.http_server`) under ``/api`` and adds a session layer
around it. Every job route - submit, poll, cancel, resume, transcript, search,
export - is the *same* code the HTTP and MCP doors already use, so the UI cannot
drift from them: there is exactly one submission contract and one job store, and
this module adds transport only.

What this layer adds, and nothing else:

- **The HTML shell.** ``GET /`` serves the packaged single-page workspace with a
  fresh per-process capability token embedded in a ``<meta>`` element. The token
  never appears in a URL, a cookie, or a log line.
- **Packaged static assets.** ``GET /assets/...`` serves CSS/JS from the wheel.
  No CDN, no external script, no telemetry - every asset is served from this
  process.
- **A stricter request gate.** Loopback host, provably-loopback peer, and an
  ``Origin`` that, when present, must be exactly this UI's own origin. The UI
  refuses any non-loopback bind regardless of ``TEXTFLOWKIT_ALLOW_REMOTE``.
- **A session capability header on mutators** (see :mod:`.security`).
- **A streamed raw-body upload** and a capability endpoint, both of which live
  here rather than in the mounted app so the mounted app is byte-for-byte the
  developer surface it always was.

The mounted app is created once and its routes are never modified, so the
existing HTTP contract and its tests are unaffected.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from textflowkit import __version__
from textflowkit.adapters import http_server
from textflowkit.core.executor import shutdown_default_executor
from textflowkit.core.jobs import JobState, get_default_store, validate_list_limit
from textflowkit.core.runner import transcript_for
from textflowkit.core.startup import recover_startup
from textflowkit.core.submission import SubmissionRequest, item_source, submit_batch, submit_request
from textflowkit.render import SUPPORTED_FORMATS, render_bytes
from textflowkit.ui import capabilities as caps
from textflowkit.ui import paths as ui_paths
from textflowkit.ui import uploads
from textflowkit.ui.security import (
    CAPABILITY_HEADER,
    new_session_token,
    request_refusal,
    ui_origin,
)

#: The formats the download dropdown offers, in display order. Deliberately an
#: explicit allowlist: a download can only ever produce one of these, so no
#: caller-supplied format reaches a renderer or a filesystem path.
DOWNLOAD_FORMATS = ("txt", "md", "srt", "vtt", "json", "docx", "pdf")

#: Content type per format, for the download response.
_CONTENT_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def _static_root():
    """The packaged static directory, resolved through importlib.resources.

    Using the resource API rather than a path relative to ``__file__`` keeps the
    assets readable from an installed wheel (where the package may be zipped)
    and from a source checkout alike.
    """
    return files("textflowkit.ui").joinpath("static")


def _read_static(relative: str) -> bytes | None:
    """Read one packaged asset, or None if it is not packaged.

    The name is joined onto the resource root and must resolve to a *file* under
    it; ``..`` segments and absolute names cannot escape because
    ``importlib.resources`` traverses children only.
    """
    resource = _static_root()
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            return None
        resource = resource.joinpath(part)
    if not resource.is_file():
        return None
    return resource.read_bytes()


def _session_html(token: str, origin: str) -> str:
    """Render the workspace shell with its capability token embedded.

    The token is inserted into a ``<meta>`` attribute. It is *not* interpolated
    into a URL, and the shell contains no inline script reading it from the
    query string, so it never lands in browser history or a server access log.
    ``json.dumps`` is used to quote the value as a JS/HTML-safe literal, and the
    three bytes that could break out of an HTML attribute are escaped, so a
    token (which is URL-safe anyway) cannot inject markup.
    """
    shell = _read_static("index.html")
    if shell is None:  # pragma: no cover - packaged asset must exist
        return "<!doctype html><title>textflowkit</title><p>UI assets are missing.</p>"
    text = shell.decode("utf-8")
    safe_token = (
        json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    text = text.replace("__TFK_CAPABILITY__", safe_token[1:-1])  # strip json quotes
    text = text.replace("__TFK_ORIGIN__", origin)
    text = text.replace("__TFK_VERSION__", __version__)
    return text


def _capability_ok(supplied: str | None, token: str) -> bool:
    import hmac

    return bool(supplied) and hmac.compare_digest(supplied, token)


def _create_shutdown_controller(app: FastAPI):
    """A shutdown request owned by the server, never a signal to a raw pid.

    The workspace offers a "Stop server" action for an operator who launched the
    UI from a shortcut with no terminal to press Ctrl+C in. The implementation here
    is deliberately *inside* the ASGI process: the endpoint sets a flag that a
    watchdog coroutine watches, and on seeing it the coroutine asks the **running
    uvicorn server** to exit through its own supported mechanism
    (``server.should_exit``). The server then runs its normal shutdown - the
    lifespan teardown drains the worker pool - so a queued job's work is not
    killed and a durable checkpoint survives.

    This is the whole reason the launcher uses ``uvicorn.Server.run`` rather than a
    blocking ``uvicorn.run`` call: it is the only way to hold the server object and
    ask it to stop. Nothing here signals, terminates, or kills a pid, and in
    particular it never touches a process the launcher did not start. If no
    controller is installed (a test harness, or an embedded ASGI server), the
    endpoint refuses rather than pretending it can stop something.
    """
    state: dict[str, Any] = {"server": None, "requested": False, "task": None}

    def _watch() -> None:
        # The flag is set by the endpoint on the request loop; this runs on a
        # short-lived daemon thread and only *sets an attribute on the server
        # object*. Setting ``should_exit`` is thread-safe in uvicorn: the server's
        # own loop reads it between iterations and performs the graceful,
        # draining shutdown. No signal reaches any process.
        deadline = time.monotonic() + 30.0
        while not state["requested"] and time.monotonic() < deadline:
            time.sleep(0.1)
        server = state["server"]
        if server is not None and state["requested"]:
            server.should_exit = True
            server.force_exit = False

    def attach_server(server: Any) -> None:
        """Record the uvicorn server this app serves on (called by the launcher)."""
        state["server"] = server

    def request_stop() -> bool:
        """Ask this process's own server to exit. Returns False if not owned."""
        if state["server"] is None:
            # No server object was attached, so there is nothing this process
            # *owns* to stop: refuse rather than claim a stop it cannot make.
            return False
        state["requested"] = True
        if state["task"] is None:
            thread = threading.Thread(
                target=_watch, name="textflowkit-ui-shutdown", daemon=True
            )
            state["task"] = thread
            thread.start()
        return True

    return state, request_stop, attach_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Recover the durable store, then drain workers on shutdown.

    Mirrors the mounted app's own lifecycle, but this app is the one a launcher
    starts, so it owns recovery for the process: an interrupted job from a
    previous UI session is failed before the first read is served, making it
    resumable rather than a row that reads ``running`` forever.
    """
    recover_startup()
    try:
        yield
    finally:
        shutdown_default_executor(wait=True)


def create_app(*, host: str = "127.0.0.1", port: int = 8756) -> FastAPI:
    """Build the UI app bound to a known loopback origin.

    ``host``/``port`` are the *actual* bind address the launcher will use; they
    define the exact origin the request gate accepts. They are never read from a
    request, so a caller cannot widen the accepted origin by sending a header.
    """
    token = new_session_token()
    expected_origin = ui_origin(host, port)

    app = FastAPI(
        title="textflowkit local UI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    _shutdown_state, request_stop, attach_server = _create_shutdown_controller(app)

    @app.middleware("http")
    async def session_guard(request: Request, call_next):
        path = request.url.path
        # The shell and its assets are GETs the browser issues before any script
        # runs; they carry no capability header by design but are still gated on
        # loopback host/peer/origin below.
        is_asset = path == "/" or path.startswith("/assets/")
        refusal = request_refusal(
            host=request.headers.get("host"),
            origin=request.headers.get("origin"),
            peer=request.client.host if request.client else None,
            expected_origin=expected_origin,
            method=request.method,
            supplied_capability=request.headers.get(CAPABILITY_HEADER),
            session_token=token,
            is_asset_request=is_asset,
        )
        if refusal is not None:
            return JSONResponse({"error": refusal}, status_code=403)
        return await call_next(request)

    # --- shell and assets -------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_session_html(token, expected_origin))

    @app.get("/assets/{asset_path:path}")
    def asset(asset_path: str):
        data = _read_static(asset_path)
        if data is None:
            return PlainTextResponse("not found", status_code=404)
        return Response(content=data, media_type=_content_type_for(asset_path))

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    # --- capability and metadata ------------------------------------------

    @app.get("/ui/capabilities")
    def ui_capabilities() -> dict[str, Any]:
        """What this install can do - no model loaded, nothing downloaded."""
        info = caps.capabilities()
        info["version"] = __version__
        info["download_formats"] = list(DOWNLOAD_FORMATS)
        info["supported_formats"] = list(SUPPORTED_FORMATS)
        info["max_upload_bytes"] = ui_paths.max_upload_bytes()
        return info

    @app.post("/ui/preflight")
    async def ui_preflight(request: Request) -> dict[str, Any]:
        """Advisory dependency/size check for a submission the operator is about
        to make. Never queues anything and never writes a job row."""
        payload = await _json_body(request)
        source = str(payload.get("source") or "")
        formats = payload.get("formats") or []
        if not isinstance(formats, list):
            formats = []
        input_root = ui_paths.Path(_input_root()) if _input_root() else None
        return caps.preflight(source, [str(f) for f in formats], input_root=input_root)

    # --- streamed upload --------------------------------------------------

    @app.post("/ui/uploads")
    async def ui_upload(request: Request):
        """Stream a raw file body to a durable, owned scratch path.

        The original filename arrives as a *header*, not in the body, because the
        body is the file itself. HTTP header values are Latin-1, so a non-Latin-1
        name is percent-encoded by the UI; it is decoded here
        (:func:`uploads.decode_filename_header`) before use. The name is used only
        to pick a safe suffix for the staged file and as a display label; the
        destination path is generated here and is never taken from the request.
        """
        filename = uploads.decode_filename_header(
            request.headers.get("x-textflowkit-filename")
        )
        try:
            path, size = await uploads.stream_to_owned_path(request, filename)
        except uploads.UploadTooLarge as exc:
            return JSONResponse({"error": str(exc), "code": "too_large"}, status_code=413)
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "code": "empty"}, status_code=400)
        return {"upload_id": path.name, "path": str(path), "bytes": size}

    # --- submission (thin proxy to the shared contract) -------------------

    @app.post("/ui/jobs", status_code=202)
    async def ui_create_job(request: Request) -> Any:
        """Submit one job through the shared submission contract.

        The request body is the *same* shape the mounted HTTP ``POST /jobs``
        takes, so the UI speaks the existing contract rather than a parallel one.
        A ``mode=upload`` body names an ``upload_id`` that this endpoint turns
        into the staged path for the source; everything else is passed through.
        """
        payload = await _json_body(request)
        try:
            submission, _mode = _materialize(payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        store = get_default_store()
        try:
            job = submit_request(store, submission)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except Exception as exc:
            from textflowkit.core.executor import QueueFullError, StoreUnavailableError

            if isinstance(exc, QueueFullError):
                return JSONResponse({"error": str(exc)}, status_code=429)
            if isinstance(exc, StoreUnavailableError):
                return JSONResponse({"error": str(exc)}, status_code=503)
            raise
        return job.to_dict()

    @app.post("/ui/jobs/batch", status_code=202)
    async def ui_create_batch(request: Request) -> Any:
        """Submit several sources through the shared batch contract."""
        payload = await _json_body(request)
        items = payload.get("jobs")
        if not isinstance(items, list):
            return JSONResponse({"error": "jobs must be a list"}, status_code=422)
        store = get_default_store()
        requests: list[SubmissionRequest] = []
        results: list[dict[str, Any] | None] = []
        for index, item in enumerate(items):
            try:
                submission, _mode = _materialize(item)
            except (TypeError, ValueError) as exc:
                results.append({
                    "index": index,
                    "source": item_source(item),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            else:
                requests.append(submission)
                results.append(None)
        accepted = submit_batch(store, requests, resume=bool(payload.get("resume")))
        accepted_iter = iter(accepted)
        jobs = []
        for index, entry in enumerate(results):
            outcome = next(accepted_iter) if entry is None else entry
            jobs.append({**outcome, "index": index})
        return {"count": len(jobs), "jobs": jobs}

    # --- recent jobs (shared list) ----------------------------------------

    @app.get("/ui/jobs")
    def ui_list_jobs(limit: int = 20, state: str | None = None) -> Any:
        try:
            validate_list_limit(limit)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        filter_state = None
        if state:
            try:
                filter_state = JobState(state.lower())
            except ValueError:
                return JSONResponse(
                    {"error": f"unknown state '{state}'"}, status_code=422
                )
        rows = get_default_store().list(limit=limit, state=filter_state)
        return {"count": len(rows), "jobs": [_job_for_ui(j) for j in rows]}

    # --- fixed-format download --------------------------------------------

    @app.get("/ui/jobs/{job_id}/download")
    def ui_download(job_id: str, format: str = "txt"):
        """Download a finished transcript in one of a fixed set of formats.

        The format is validated against :data:`DOWNLOAD_FORMATS` - a fixed
        allowlist - so no arbitrary format, and therefore no arbitrary renderer
        or path, can be reached. The filename in ``Content-Disposition`` is built
        from the job id (a short hex token) and the chosen extension, never from
        caller input or from a path on disk, so the header cannot carry a
        traversal or a header-injection payload.
        """
        fmt = (format or "txt").lower().lstrip(".")
        if fmt not in DOWNLOAD_FORMATS:
            return JSONResponse(
                {
                    "error": f"format '{format}' is not downloadable",
                    "available_formats": list(DOWNLOAD_FORMATS),
                },
                status_code=422,
            )
        job = get_default_store().get(job_id)
        if job is None:
            return JSONResponse({"error": f"no job with id '{job_id}'"}, status_code=404)
        if job.state is not JobState.DONE:
            return JSONResponse(
                {"error": f"job not finished (state: {job.state.value})"}, status_code=409
            )
        tr = transcript_for(job)
        if tr is None:
            return JSONResponse({"error": "job contains no transcript"}, status_code=500)
        try:
            data = render_bytes(tr, fmt, title=job.id)
        except (ValueError, ImportError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        filename = f"{job.id}.{fmt}"
        return Response(
            content=data,
            media_type=_CONTENT_TYPES[fmt],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- controlled shutdown ----------------------------------------------

    @app.post("/ui/shutdown")
    def ui_shutdown() -> Any:
        """Ask this process's own server to stop, after draining.

        The capability header (enforced by the gate for every mutator) plus the
        browser's confirmation means only an operator acting in this UI's own page
        can stop the server. The stop is routed through the uvicorn server object
        this process owns - never a signal to a pid - so a queued job is drained
        rather than killed. If the app is not running under the launcher's owned
        server, this refuses (409) rather than claiming a stop it cannot perform.
        """
        if not request_stop():
            return JSONResponse(
                {"error": "this server does not own a controllable shutdown; "
                          "stop it the way you started it (Ctrl+C)."},
                status_code=409,
            )
        return {"stopping": True, "detail": "draining workers, then exiting"}

    # --- mount the existing developer HTTP app ----------------------------

    # Mounted *last* and under /api, so no UI route can be shadowed by it, and
    # so the existing app's own routes, middleware, and guards are untouched.
    app.mount("/api", http_server.app)

    # Let the launcher hand the running server to the shutdown controller.
    app.ui_attach_owned_server = attach_server  # type: ignore[attr-defined]
    return app


def _job_for_ui(job) -> dict[str, Any]:
    """A job row plus a UI-friendly ``display_source`` label.

    The shared ``to_dict`` is left exactly as the other doors publish it. This
    adds one **additive** field the workspace uses for its recent-jobs label: when
    a job's source is a staged upload, its original filename (kept in the UI's
    upload sidecar); otherwise the bare basename of a local path, or the source
    unchanged for a URL. The opaque random stage name is never shown.
    """
    data = job.to_dict()
    source = data.get("source")
    label = None
    if isinstance(source, str) and source:
        label = uploads.original_name_for_source(source)
        if label is None:
            if source.startswith(("http://", "https://", "data:")):
                label = source
            else:
                # A local path: show its basename, not the whole staged path. Fall
                # back to the job id if even the basename is empty.
                base = source.replace("\\", "/").rsplit("/", 1)[-1]
                label = base or data.get("id")
    data["display_source"] = label or data.get("id")
    return data


def _content_type_for(asset_path: str) -> str:
    suffix = asset_path.rsplit(".", 1)[-1].lower() if "." in asset_path else ""
    return {
        "css": "text/css; charset=utf-8",
        "js": "text/javascript; charset=utf-8",
        "html": "text/html; charset=utf-8",
        "svg": "image/svg+xml",
        "ico": "image/x-icon",
        "png": "image/png",
        "woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")


def _input_root():
    import os

    raw = os.environ.get("TEXTFLOWKIT_INPUT_ROOT")
    return raw


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001 - a broken body is a client error
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _materialize(payload: dict[str, Any]) -> tuple[SubmissionRequest, str]:
    """Turn a UI submission body into a shared ``SubmissionRequest``.

    Handles the two source modes the UI offers:

    - ``mode == "upload"``: the body names an ``upload_id`` that was returned by
      ``POST /ui/uploads``. It is resolved against the uploads directory, and
      the resolved path must live *inside* that directory, so the id cannot be
      used to reach any other file on the machine.
    - anything else: a URL, a ``data:`` URL, or - if the caller opted into the
      advanced field - a local path, passed straight through to the shared
      contract, which applies the configured input root.

    Returns ``(request, mode)``. Raises ``ValueError`` for a body the shared
    contract will refuse, so the caller can answer 422 without writing a job.
    """
    data = dict(payload)
    mode = str(data.pop("mode", "source") or "source")
    source = data.get("source")

    if mode == "upload":
        upload_id = data.pop("upload_id", None)
        if not upload_id or not isinstance(upload_id, str):
            raise ValueError("upload mode requires an upload_id")
        staged = ui_paths.default_uploads_dir() / upload_id
        resolved = staged.resolve()
        uploads_root = ui_paths.default_uploads_dir().resolve()
        if resolved != uploads_root and uploads_root not in resolved.parents:
            raise ValueError("upload_id does not name a staged upload")
        if not resolved.is_file():
            raise ValueError(f"no staged upload with id '{upload_id}'")
        source = str(resolved)
        data["source"] = source

    if not isinstance(source, str) or not source:
        raise ValueError("source is required")

    allowed = {
        "source", "language", "formats", "output_dir", "model", "engine",
        "device", "diarize", "translate_to",
    }
    clean = {k: v for k, v in data.items() if k in allowed}
    clean["source"] = source
    # The UI never reads browser cookies; refuse the field loudly rather than
    # accepting and ignoring it.
    if "cookies_from_browser" in data:
        raise ValueError("cookies_from_browser is not available in the local UI")
    return SubmissionRequest(**clean), mode
