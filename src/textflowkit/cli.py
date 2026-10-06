"""textflowkit command-line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from textflowkit import __version__
from textflowkit.core.batch import run_batch
from textflowkit.core.checkpoint import metadata_only_checkpoint
from textflowkit.core.engine import (
    DEFAULT_ENGINE,
    ENGINE_CHOICES,
    ensure_engine_available,
    get_engine,
)
from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.core.model import Transcript
from textflowkit.core.paths import default_input_root, output_root
from textflowkit.core.pipeline import TranscribeResult
from textflowkit.core.runner import transcript_for
from textflowkit.core.startup import StartupRecoveryError, recover_startup
from textflowkit.core.submission import SubmissionRequest, submit_request
from textflowkit.render import (
    BINARY_FORMATS,
    SUPPORTED_FORMATS,
    TEXT_FORMATS,
    atomic_write_bytes,
    render,
    render_bytes,
)
from textflowkit.sources.acquire import AcquisitionError
from textflowkit.sources.detect import PLATFORMS


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="textflowkit",
        description="Transcribe media from a URL or local file into timestamped text and subtitles.",
    )
    p.add_argument("--version", action="version", version=f"textflowkit {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("transcribe", help="transcribe a URL or local media file")
    t.add_argument("source", help="media URL or path to a local file")
    t.add_argument("--formats", default="json,srt,txt",
                   help=f"comma-separated outputs (default: json,srt,txt; available: {', '.join(SUPPORTED_FORMATS)})")
    t.add_argument("--output-dir", "-o", default=None, help="directory for written outputs")
    t.add_argument("--language", default=None, help="source language code (e.g. en); default auto-detect")
    t.add_argument("--model", default=None,
                   help="model name; default is the engine's own (whistle, or a "
                        "whisper size such as small/tiny for the whisper engines)")
    t.add_argument("--device", default=None,
                   help="torch device (cuda/cpu) for the whisper engines; default auto (whistle is CPU-only)")
    t.add_argument(
        "--engine",
        default=DEFAULT_ENGINE,
        help=(
            f"speech engine, one of {', '.join(ENGINE_CHOICES)} (default whistle: a "
            "CPU-only native engine needing no torch). whisper is openai-whisper on "
            "the torch/ROCm stack and needs the whisper extra; faster-whisper is an "
            "opt-in CPU/Mac engine and needs its own extra"
        ),
    )
    t.add_argument(
        "--diarize",
        action="store_true",
        help=(
            "label speakers. Requires the optional 'diarize' extra and a Hugging "
            "Face token with access to the gated pyannote model; fails loudly if either is missing"
        ),
    )
    t.add_argument(
        "--translate-to",
        default=None,
        metavar="LANG",
        help=(
            "translate the transcript into LANG (e.g. es) using the configured "
            "backend; the job fails loudly if the backend is unreachable"
        ),
    )
    t.add_argument("--cookies-from-browser", default=None,
                   help="pass cookies to yt-dlp from a browser (e.g. firefox) for access-controlled content")
    t.add_argument("--stdout", action="store_true", help="print transcript to stdout instead of writing files")
    t.add_argument("--stdout-format", default="txt",
                   help=f"format for --stdout (default txt; text formats only: {', '.join(TEXT_FORMATS)})")
    t.add_argument(
        "--resume",
        action="store_true",
        help="reuse a completed checkpoint for the same source and options when one exists",
    )
    t.add_argument("--quiet", "-q", action="store_true", help="suppress progress messages")

    b = sub.add_parser("batch", help="transcribe many sources in one invocation")
    b.add_argument("sources", nargs="+", help="media URLs or paths to local files")
    b.add_argument("--formats", default="json,srt,txt",
                   help=f"comma-separated outputs (default: json,srt,txt; available: {', '.join(SUPPORTED_FORMATS)})")
    b.add_argument("--output-dir", "-o", default=None, help="directory for written outputs")
    b.add_argument("--language", default=None, help="source language code (e.g. en); default auto-detect")
    b.add_argument("--model", default=None,
                   help="model name; default is the engine's own (whistle, or a "
                        "whisper size such as small/tiny for the whisper engines)")
    b.add_argument("--device", default=None,
                   help="torch device (cuda/cpu) for the whisper engines; default auto (whistle is CPU-only)")
    b.add_argument(
        "--engine",
        default=DEFAULT_ENGINE,
        help=(
            f"speech engine, one of {', '.join(ENGINE_CHOICES)} (default whistle: a "
            "CPU-only native engine needing no torch). whisper is openai-whisper on "
            "the torch/ROCm stack and needs the whisper extra; faster-whisper is an "
            "opt-in CPU/Mac engine and needs its own extra"
        ),
    )
    b.add_argument("--diarize", action="store_true", help="label speakers (same requirements as transcribe)")
    b.add_argument("--translate-to", default=None, metavar="LANG", help="translate transcript into LANG")
    b.add_argument("--cookies-from-browser", default=None,
                   help="pass cookies to yt-dlp from a browser (e.g. firefox)")
    b.add_argument(
        "--resume",
        action="store_true",
        help="reuse a completed checkpoint for each matching source and options",
    )
    b.add_argument("--quiet", "-q", action="store_true", help="suppress per-item progress messages")

    l = sub.add_parser("export", help="re-render an existing transcript JSON")
    l.add_argument("transcript", help="path to a transcript .json file")
    l.add_argument("--format", "-f", default="srt", help=f"output format ({', '.join(SUPPORTED_FORMATS)})")
    l.add_argument("--output", "-o", default=None, help="output file (default stdout)")

    sub.add_parser("sources", help="list recognised platforms")
    sub.add_parser("doctor", help="report the versions and tools this install will use")
    st = sub.add_parser(
        "selftest",
        help="run a real end-to-end check on this machine (compute device + a tiny transcription)",
    )
    st.add_argument("--engine", default=DEFAULT_ENGINE,
                    help=f"engine for the check, one of {', '.join(ENGINE_CHOICES)} (default whistle)")
    st.add_argument("--model", default=None,
                    help="model for the check; default is the engine's own "
                         "(whistle, or a whisper size such as tiny for the whisper engines)")
    st.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="only check the compute device, do not load a model",
    )
    return p


def _formats(raw: str) -> list[str]:
    return [f.strip().lower().lstrip(".") for f in raw.split(",") if f.strip()]


def _stdout_format(raw: str) -> str:
    """Canonical ``--stdout-format``, or ``ValueError`` when it cannot be printed.

    ``--stdout`` writes to the terminal, so it can only carry a text format.
    The renderer refuses binary and unknown formats, but it is reached only
    after the job has been submitted and the transcription paid for, so the run
    ends in a traceback with the whole wait already spent. The format is known
    from the arguments alone and is settled here instead - before any
    acquisition, inference, or job record exists.
    """
    fmt = raw.strip().lower().lstrip(".")
    if fmt in TEXT_FORMATS:
        return fmt
    choices = ", ".join(TEXT_FORMATS)
    if fmt in BINARY_FORMATS:
        raise ValueError(
            f"--stdout-format {raw}: {fmt} is a binary format and cannot be printed "
            f"to a terminal; write it to a file instead (text formats: {choices})"
        )
    raise ValueError(f"unsupported --stdout-format: {raw} (choose from {choices})")


def _store_is_durable() -> bool:
    """Whether the default store survives this process.

    ``_make_store`` falls back to ``MemoryJobStore`` when TEXTFLOWKIT_DB is
    unset, and an in-memory store cannot carry a checkpoint into a later
    invocation. Resume depends on that carrying, so callers warn instead of
    silently redoing the work.
    """
    return bool(os.environ.get("TEXTFLOWKIT_DB"))


def _recover_owned_store() -> int:
    """Recover orphaned rows once for the store this CLI process owns.

    A CLI invocation owns its store for the duration of the command: the jobs
    this process runs are created after this call, so any PENDING/RUNNING row
    already in a durable store is an orphan from a process that died - it has no
    worker, and without recovery a ``--resume`` reads it as "already active" and
    refuses to reuse the saved work (audit finding AL-001).

    This belongs at the CLI's process/store ownership boundary, *not* inside
    ``submit_request(background=False)``: an embedding process can hold
    genuinely live work, so an unconditional reap there would fail rows it does
    not own. Recovery is delegated to the shared per-store owner, so a single
    CLI process reaps exactly once even if both the transcribe and batch paths
    were to ask. It runs only against a durable store; an in-memory store is
    fresh per process and has no orphans to recover.

    Returns 0 when recovery ran or was unnecessary, and 1 (with a clear message
    on stderr, no traceback) when the store rejected the recovery write: the
    command must fail rather than proceed over a store whose orphans still read
    ``running``.
    """
    if not _store_is_durable():
        return 0
    try:
        recover_startup(get_default_store())
    except StartupRecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _preflight_engine(name: str) -> str | None:
    """Reject an unusable ``--engine`` before any work is submitted.

    The engine name and, for an optional engine, whether its package is even
    importable are both knowable from the arguments alone. Discovering either
    later means discovering it after the media was acquired, the audio decoded
    and a job record written - the same reason ``--stdout-format`` is settled
    here. Returns an error message, or ``None`` when the engine is usable.
    """
    try:
        ensure_engine_available(name)
    except (ValueError, RuntimeError) as exc:
        return str(exc)
    return None


def _stage_printer(stage: str) -> None:
    """Report an in-flight pipeline stage to stderr, flushed.

    Progress belongs on stderr so the machine-readable stdout stays clean, and
    it is flushed because the point is prompt feedback: a line that sits in a
    block buffer until the (long) run ends tells an operator nothing while the
    stage is still in flight. The callback is only ever passed for a non-quiet
    run; quiet mode passes none.
    """
    print(f"{stage}...", file=sys.stderr, flush=True)


def _cmd_transcribe(args: argparse.Namespace) -> int:
    formats = _formats(args.formats)
    error = _preflight_engine(args.engine)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.stdout:
        try:
            args.stdout_format = _stdout_format(args.stdout_format)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    output_dir = args.output_dir
    if output_dir is None and not args.stdout:
        output_dir = "."
    if args.resume and not _store_is_durable():
        print(
            "warning: --resume needs a durable store; TEXTFLOWKIT_DB is not set, "
            "so no checkpoint from an earlier run can be found. Re-running from "
            "scratch. Set TEXTFLOWKIT_DB to persist jobs and checkpoints.",
            file=sys.stderr,
        )

    # In-flight feedback is stderr-only and progress chatter, so quiet mode
    # passes no observer and the transcript/path contract on stdout is untouched.
    # The observer is a process-local display sink: it is threaded to the runner
    # but never stored in the request or the checkpoint.
    notify = None if args.quiet else _stage_printer
    try:
        request = SubmissionRequest(
            source=args.source, language=args.language, formats=formats,
            output_dir=output_dir, model=args.model, engine=args.engine,
            device=args.device,
            cookies_from_browser=args.cookies_from_browser,
            diarize=args.diarize, translate_to=args.translate_to,
        )
        store = get_default_store()
        job = submit_request(
            store, request, background=False, resume=args.resume, notify=notify,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if job.state is not JobState.DONE:
        print(f"error: {job.error or job.state.value}", file=sys.stderr)
        return 1
    transcript = transcript_for(job)
    if transcript is None:
        print("error: completed job contains no transcript", file=sys.stderr)
        return 1
    return _finish_transcribe(
        args, TranscribeResult(transcript=transcript, outputs=[Path(p) for p in job.outputs]),
        job, store,
    )


def _finish_transcribe(
    args: argparse.Namespace,
    result,
    job,
    store,
) -> int:
    """Record and print a result that came from the pipeline or reuse."""
    from textflowkit.core.pipeline import TranscribeResult

    if not isinstance(result, TranscribeResult):
        transcript, outputs = result
        result = TranscribeResult(transcript=transcript, outputs=list(outputs))
    # The runner is not the only writer that finishes a job, so this transition
    # keeps the same storage rule: the transcript goes in once, and the
    # checkpoint keeps only the metadata a later request is matched against. The
    # row is re-read rather than trusting the caller's object: a stale one
    # carries no checkpoint, and writing that absence over a real one would throw
    # away the job's resume identity. Both fields go in one update, so a row is
    # never left holding neither copy.
    current = store.get(job.id)
    fields = {
        "state": JobState.DONE,
        "progress": "complete",
        "transcript": result.transcript.to_dict(),
        "outputs": [str(p) for p in result.outputs],
    }
    metadata = metadata_only_checkpoint(current.checkpoint if current is not None else None)
    if metadata is not None:
        fields["checkpoint"] = metadata
    store.update(job.id, **fields)

    tr = result.transcript
    if not args.quiet:
        segs = len(tr.segments)
        print(f"platform : {tr.platform}", file=sys.stderr)
        print(f"language : {tr.language or 'unknown'}", file=sys.stderr)
        print(f"segments : {segs}", file=sys.stderr)
        if tr.metadata.get("model"):
            print(f"engine   : {tr.engine} ({tr.metadata.get('model')} on {tr.metadata.get('device')})", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(render(tr, args.stdout_format))
        return 0

    for path in result.outputs:
        print(str(path))
    return 0


def _print_item(item) -> None:
    """Emit one batch item's outcome, flushed so it lands as the item finishes.

    The flush is the point of the feedback: an item that completed must be
    visible while a later, slow item still runs, so the line cannot wait in a
    block buffer. Shared with the final summary loop so the per-item rendering
    lives in exactly one place.
    """
    detail = item.error or ", ".join(item.outputs)
    suffix = f" - {detail}" if detail else ""
    print(f"{item.status:<9} {item.source}{suffix}", flush=True)


def _cmd_batch(args: argparse.Namespace) -> int:
    # One engine for the whole batch, so a bad name fails before the first item
    # is submitted rather than once per source.
    error = _preflight_engine(args.engine)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.resume and not _store_is_durable():
        print(
            "warning: batch --resume cannot survive a process restart without "
            "TEXTFLOWKIT_DB; this run may repeat completed transcription. "
            "Set TEXTFLOWKIT_DB for durable resume.",
            file=sys.stderr,
        )
    # Non-quiet runs report each item the moment it finishes, so a slow later
    # item does not conceal the earlier ones. Quiet mode passes no callback and
    # the finished report is not replayed, so per-item lines stay suppressed; the
    # final summary prints either way. The callback is process-local and is not
    # part of the report, so the summary figures are unchanged.
    report = run_batch(
        list(args.sources),
        store=get_default_store(),
        resume=args.resume,
        language=args.language,
        formats=_formats(args.formats),
        output_dir=args.output_dir or ".",
        model=args.model,
        engine=args.engine,
        device=args.device,
        cookies_from_browser=args.cookies_from_browser,
        diarize=args.diarize,
        translate_to=args.translate_to,
        on_item_complete=None if args.quiet else _print_item,
    )
    print(
        f"batch: {report.total} total, {report.succeeded} succeeded, "
        f"{report.failed} failed, {report.skipped} skipped"
    )
    return 0 if report.ok else 1


def _cmd_export(args: argparse.Namespace) -> int:
    path = Path(args.transcript)
    if not path.exists():
        print(f"error: no such transcript: {path}", file=sys.stderr)
        return 1
    try:
        tr = Transcript.load_json(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: could not read transcript: {exc}", file=sys.stderr)
        return 1
    try:
        fmt = args.format.lower().lstrip(".")
        if fmt in BINARY_FORMATS and not args.output:
            raise ValueError(f"--output is required for binary {fmt} export")
        content = render_bytes(tr, fmt, title=path.stem)
    except (ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.output:
        try:
            atomic_write_bytes(Path(args.output), content, replace=True)
        except OSError as exc:
            print(f"error: could not write export: {exc}", file=sys.stderr)
            return 1
        print(str(Path(args.output)))
    else:
        sys.stdout.write(content.decode("utf-8"))
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    """Print the environment this install will actually use.

    Platform support depends on yt-dlp continuing to work against sites it does
    not own, and that breaks from the outside. When a site stops working, the
    first question is which yt-dlp and which JavaScript runtime are in play -
    this answers it without guessing.
    """
    import shutil

    def line(label: str, value: str) -> None:
        print(f"{label:<18} {value}")

    line("textflowkit", __version__)
    line("python", sys.version.split()[0])
    line("default engine", DEFAULT_ENGINE)

    # ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            out = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True, check=False
            ).stdout.splitlines()
            line("ffmpeg", out[0] if out else ffmpeg)
        except OSError as exc:
            line("ffmpeg", f"{ffmpeg} (could not run: {exc})")
    else:
        line("ffmpeg", "MISSING - required")

    # yt-dlp: which one, and how
    from textflowkit.sources.acquire import detect_js_runtime, require_tool

    try:
        ytdlp_path = require_tool("yt-dlp", module="yt_dlp")
    except AcquisitionError as exc:
        line("yt-dlp", f"MISSING - {exc}")
    else:
        try:
            import yt_dlp
            version = getattr(getattr(yt_dlp, "version", None), "__version__", "unknown")
        except ImportError:
            version = "unknown"
        how = "module (in-process)" if ytdlp_path is None else ytdlp_path
        line("yt-dlp", f"{version} via {how}")

    runtime = detect_js_runtime()
    line("js runtime", runtime or "none found (YouTube formats may be limited)")

    # Optional extras. `whisper` (openai-whisper) is listed too, because since
    # the default engine moved to Whistle it is no longer a base dependency: its
    # absence is expected on a fresh install, and doctor must say so plainly
    # rather than implying the default engine is missing.
    for label, module in (
        ("mcp", "mcp"),
        ("fastapi", "fastapi"),
        ("pyannote", "pyannote.audio"),
        ("whisper", "whisper"),
        ("faster-whisper", "faster_whisper"),
        ("python-docx", "docx"),
        ("reportlab", "reportlab"),
    ):
        try:
            __import__(module)
        except ImportError:
            line(label, "not installed")
        else:
            line(label, "installed")

    # Whistle is the default engine and needs no torch. Report only what is
    # cheap and import-free: which platform the pinned binary would be chosen
    # for, where its cache lives, and whether it is already present or would be
    # downloaded on first use. No asset is fetched and no model is loaded here.
    from textflowkit.core import whistle_assets

    try:
        status = whistle_assets.cache_status()
    except Exception as exc:  # noqa: BLE001 - diagnostic; an unsupported platform is information
        line("whistle platform", f"unsupported: {exc}")
    else:
        line("whistle platform", status["platform"])
        line("whistle cache", status["models_dir"])
        present = "cached" if status.get("model_present") else "not downloaded (fetched on first use)"
        line("whistle model", f"{whistle_assets.WHISTLE_MODEL.filename}: {present}")
    offline = os.environ.get("TEXTFLOWKIT_OFFLINE", "").strip().lower() not in ("", "0", "false")
    line("offline mode", "on (no downloads)" if offline else "off")
    # Telemetry is forced off on every Whistle child; report the gate, not a
    # claim about the upstream binary's own code.
    line("whistle telemetry", "disabled in child env (NEEDLE_TELEMETRY=0, DO_NOT_TRACK=1)")

    # Compute devices are separate decisions: pyannote may be pinned to CPU
    # while Whisper uses ROCm/CUDA, or vice versa.
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            auto_device = "cuda"
            device_label = f"{name} (torch {torch.__version__})"
        else:
            auto_device = "cpu"
            device_label = f"cpu (torch {torch.__version__})"
    except ImportError:
        auto_device = "cpu"
        device_label = "torch not installed"

    line("whisper device", f"{auto_device}: {device_label}")
    from textflowkit.core.diarize import ENV_DIARIZE_DEVICE

    diarize_device = os.environ.get(ENV_DIARIZE_DEVICE) or auto_device
    line("diarize device", f"{diarize_device}: {device_label if diarize_device == auto_device else 'configured'}")

    from textflowkit.core.translate import ENV_OLLAMA_MODEL, OllamaTranslator

    translation_model = os.environ.get(ENV_OLLAMA_MODEL)
    line("translation model", translation_model or f"not configured (set {ENV_OLLAMA_MODEL})")
    if translation_model:
        line("translation route", OllamaTranslator().route)

    line("input root", str(default_input_root() or "unconfined (CLI default)"))
    line("output root", str(output_root()))
    line("jobs store", os.environ.get("TEXTFLOWKIT_DB", "in-memory (not durable)"))
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the compute path works on THIS machine, end to end.

    Defaults to the product's default engine (Whistle). An explicit
    ``--engine whisper --model tiny`` checks the openai-whisper/torch stack
    instead. The GPU path cannot run in hosted CI - no runner has an AMD GPU -
    so the honest way to keep it verified is to make the check reproducible and
    runnable on demand rather than relying on one engineer's memory of a good
    run.
    """
    failures: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))

    def bad(label: str, detail: str) -> None:
        failures.append(label)
        print(f"  FAIL  {label}  ({detail})")

    # The torch matmul probe is the Whisper engine's compute path, so it runs
    # only when that engine is selected. Whistle is CPU-only and needs no torch,
    # so under the default engine this probe would test a stack the run will not
    # use; printing it as a PASS/FAIL would misreport what was checked.
    print("compute")
    if args.engine in ("whisper", "openai-whisper"):
        try:
            import torch

            print(f"  torch {torch.__version__}, hip={torch.version.hip}, cuda_available={torch.cuda.is_available()}")
            device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
            a = torch.randn(512, 512, device=device)
            b = torch.randn(512, 512, device=device)
            c = a @ b
            torch.cuda.synchronize() if torch.cuda.is_available() else None
            assert c.shape == (512, 512)
            if torch.cuda.is_available():
                name = torch.cuda.get_device_name(0)
                ok("matmul on device", name)
            else:
                ok("matmul on device", "cpu (no GPU visible)")
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic
            bad("matmul on device", f"{type(exc).__name__}: {exc}")
    elif args.engine == "faster-whisper":
        # faster-whisper is CTranslate2, not torch, and it is *not* the Whistle
        # native CLI: reporting a Whistle platform here would name a runtime this
        # engine never uses. Its own CPU path is what runs with no device, so
        # report that and nothing pretend-Whistle.
        try:
            from textflowkit.core.engine import FasterWhisperEngine

            engine = FasterWhisperEngine(model=args.model or "small")
            ok(
                "engine compute path",
                f"faster-whisper on {engine.device} ({engine.compute_type})",
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic
            bad("engine compute path", f"{type(exc).__name__}: {exc}")
    else:
        # Whistle runs on CPU as a native binary; there is no torch device to
        # probe. Report the engine and the platform it will actually select -
        # cheap, import-free, and no download.
        try:
            from textflowkit.core import whistle_assets

            ok(
                "engine compute path",
                f"whistle on cpu, platform {whistle_assets.current_platform()}",
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic
            bad("engine compute path", f"{type(exc).__name__}: {exc}")

    def summarise() -> int:
        print()
        if failures:
            print(f"SELFTEST FAILED: {', '.join(failures)}")
            return 1
        print("SELFTEST PASSED")
        return 0

    if args.skip_transcribe:
        return summarise()

    print("transcribe")
    try:
        fixture = files("textflowkit").joinpath("assets/selftest-speech.wav")
        with fixture.open("rb") as stream:
            if not stream.read(12).startswith(b"RIFF"):
                raise ValueError("bundled speech fixture is not a WAV file")
        ok("bundled speech fixture")
        from importlib.resources import as_file

        model = args.model  # None resolves to the selected engine's own default
        with as_file(fixture) as wav, tempfile.TemporaryDirectory(
            prefix="textflowkit-selftest-"
        ) as work:
            engine = get_engine(args.engine, model=model)
            transcribe_input = _selftest_transcribe_input(engine, wav, work_dir=Path(work))
            transcript = engine.transcribe(transcribe_input)
            speech = [s for s in transcript.segments if s.text.strip() and s.end > s.start]
            if not speech:
                raise ValueError("model returned no nonempty timed speech segments")
            recognized = " ".join(s.text for s in speech).lower().split()
            if "transcribe" not in {word.strip(".,!?;:\"'()") for word in recognized}:
                raise ValueError("model did not recognize 'transcribe' in the bundled speech")
            ok(
                "engine produced timed speech",
                f"engine={transcript.engine} model={transcript.metadata.get('model')} "
                f"device={transcript.metadata.get('device')} segments={len(speech)}",
            )
    except Exception as exc:  # noqa: BLE001 - diagnostic
        bad("engine produced timed speech", f"{type(exc).__name__}: {exc}")

    return summarise()


def _selftest_transcribe_input(engine, wav: Path, *, work_dir: Path) -> Path:
    """The WAV to hand the selected engine, normalized if the engine requires it.

    The bundled fixture is 22.05 kHz mono - the rate the Whisper-family engines
    read directly. Whistle's native CLI requires **16 kHz** mono 16-bit PCM and
    refuses anything else, so passing the fixture through unchanged made
    ``selftest`` fail on the default engine even though the product works: the
    pipeline always decodes to 16 kHz via ``extract_audio`` before transcribing,
    and the self-test must exercise that same normalized input rather than a raw
    file the engine would never see in production.

    The normalization reuses the pipeline's own ``extract_audio`` (same ffmpeg
    arguments as a real run). ``work_dir`` is a caller-owned ``TemporaryDirectory``
    that outlives the transcription, so the normalized file is present while the
    engine reads it and removed with the directory afterwards - no global state
    or file is touched. Other engines get the fixture unchanged.
    """
    from textflowkit.core.whistle import WhistleEngine, read_wav_info

    if not isinstance(engine, WhistleEngine):
        return wav
    info = read_wav_info(wav)
    if info.sample_rate == 16000 and info.channels == 1 and info.sample_width == 2:
        return wav  # already in the engine's required format

    from textflowkit.sources.acquire import extract_audio, require_tool

    require_tool("ffmpeg")
    return extract_audio(wav, work_dir=work_dir)


def _cmd_sources(_: argparse.Namespace) -> int:
    for name in sorted(PLATFORMS):
        print(name)
    print("local")
    print("direct")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "transcribe":
        # Recover the durable store this process owns before resume selection,
        # so an interrupted job from a dead process is reusable rather than
        # refused as "already active". Only the commands that submit and resume
        # work need this; `export`, `sources`, `doctor` and `selftest` neither
        # own nor select jobs and are left with their existing side effects.
        recovery = _recover_owned_store()
        if recovery != 0:
            return recovery
        return _cmd_transcribe(args)
    if args.command == "batch":
        recovery = _recover_owned_store()
        if recovery != 0:
            return recovery
        return _cmd_batch(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "sources":
        return _cmd_sources(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "selftest":
        return _cmd_selftest(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
