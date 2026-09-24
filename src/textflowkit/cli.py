"""textflowkit command-line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from textflowkit import __version__
from textflowkit.core.batch import run_batch
from textflowkit.core.engine import get_engine
from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.core.model import Transcript
from textflowkit.core.paths import default_input_root, output_root
from textflowkit.core.pipeline import TranscribeResult
from textflowkit.core.runner import transcript_for
from textflowkit.core.submission import SubmissionRequest, submit_request
from textflowkit.render import (
    BINARY_FORMATS,
    SUPPORTED_FORMATS,
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
    t.add_argument("--model", default="small", help="whisper model size (tiny/base/small/medium/large); default small")
    t.add_argument("--device", default=None, help="torch device (cuda/cpu); default auto")
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
    t.add_argument("--stdout-format", default="txt", help="format for --stdout (default txt)")
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
    b.add_argument("--model", default="small", help="whisper model size (tiny/base/small/medium/large); default small")
    b.add_argument("--device", default=None, help="torch device (cuda/cpu); default auto")
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
    st.add_argument("--model", default="tiny", help="whisper model for the check (default tiny)")
    st.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="only check the compute device, do not load a model",
    )
    return p


def _formats(raw: str) -> list[str]:
    return [f.strip().lower().lstrip(".") for f in raw.split(",") if f.strip()]


def _store_is_durable() -> bool:
    """Whether the default store survives this process.

    ``_make_store`` falls back to ``MemoryJobStore`` when TEXTFLOWKIT_DB is
    unset, and an in-memory store cannot carry a checkpoint into a later
    invocation. Resume depends on that carrying, so callers warn instead of
    silently redoing the work.
    """
    return bool(os.environ.get("TEXTFLOWKIT_DB"))


def _cmd_transcribe(args: argparse.Namespace) -> int:
    formats = _formats(args.formats)
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

    try:
        request = SubmissionRequest(
            source=args.source, language=args.language, formats=formats,
            output_dir=output_dir, model=args.model, device=args.device,
            cookies_from_browser=args.cookies_from_browser,
            diarize=args.diarize, translate_to=args.translate_to,
        )
        store = get_default_store()
        job = submit_request(store, request, background=False, resume=args.resume)
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
    store.update(
        job.id,
        state=JobState.DONE,
        progress="complete",
        transcript=result.transcript.to_dict(),
        outputs=[str(p) for p in result.outputs],
    )

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


def _cmd_batch(args: argparse.Namespace) -> int:
    if args.resume and not _store_is_durable():
        print(
            "warning: batch --resume cannot survive a process restart without "
            "TEXTFLOWKIT_DB; this run may repeat completed transcription. "
            "Set TEXTFLOWKIT_DB for durable resume.",
            file=sys.stderr,
        )
    report = run_batch(
        list(args.sources),
        store=get_default_store(),
        resume=args.resume,
        language=args.language,
        formats=_formats(args.formats),
        output_dir=args.output_dir or ".",
        model=args.model,
        device=args.device,
        cookies_from_browser=args.cookies_from_browser,
        diarize=args.diarize,
        translate_to=args.translate_to,
    )
    if not args.quiet:
        for item in report.items:
            detail = item.error or ", ".join(item.outputs)
            suffix = f" - {detail}" if detail else ""
            print(f"{item.status:<9} {item.source}{suffix}")
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

    # optional extras
    for label, module in (
        ("mcp", "mcp"),
        ("fastapi", "fastapi"),
        ("pyannote", "pyannote.audio"),
        ("python-docx", "docx"),
        ("reportlab", "reportlab"),
    ):
        try:
            __import__(module)
        except ImportError:
            line(label, "not installed")
        else:
            line(label, "installed")

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

    The GPU path cannot run in hosted CI - no runner has an AMD GPU - so the
    honest way to keep it verified is to make the check reproducible and runnable
    on demand rather than relying on one engineer's memory of a good run.
    """
    failures: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))

    def bad(label: str, detail: str) -> None:
        failures.append(label)
        print(f"  FAIL  {label}  ({detail})")

    print("compute")
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

        with as_file(fixture) as wav:
            engine = get_engine("whisper", model=args.model)
            transcript = engine.transcribe(wav)
            speech = [s for s in transcript.segments if s.text.strip() and s.end > s.start]
            if not speech:
                raise ValueError("model returned no nonempty timed speech segments")
            ok("whisper produced timed speech", f"model={args.model} device={transcript.metadata.get('device')} segments={len(speech)}")
    except Exception as exc:  # noqa: BLE001 - diagnostic
        bad("whisper produced timed speech", f"{type(exc).__name__}: {exc}")

    return summarise()


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
        return _cmd_transcribe(args)
    if args.command == "batch":
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
