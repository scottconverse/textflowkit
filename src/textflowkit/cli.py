"""textflowkit command-line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from textflowkit import __version__
from textflowkit.core.model import Transcript
from textflowkit.core.paths import default_input_root, output_root
from textflowkit.core.pipeline import PipelineError, transcribe
from textflowkit.render import SUPPORTED_FORMATS, render
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
    t.add_argument("--quiet", "-q", action="store_true", help="suppress progress messages")

    l = sub.add_parser("export", help="re-render an existing transcript JSON")
    l.add_argument("transcript", help="path to a transcript .json file")
    l.add_argument("--format", "-f", default="srt", help=f"output format ({', '.join(SUPPORTED_FORMATS)})")
    l.add_argument("--output", "-o", default=None, help="output file (default stdout)")

    sub.add_parser("sources", help="list recognised platforms")
    sub.add_parser("doctor", help="report the versions and tools this install will use")
    return p


def _cmd_transcribe(args: argparse.Namespace) -> int:
    formats = [f.strip().lower().lstrip(".") for f in args.formats.split(",") if f.strip()]
    output_dir = args.output_dir
    if output_dir is None and not args.stdout:
        output_dir = "."

    try:
        result = transcribe(
            args.source,
            language=args.language,
            formats=formats,
            output_dir=output_dir,
            model=args.model,
            device=args.device,
            cookies_from_browser=args.cookies_from_browser,
            diarize=args.diarize,
            translate_to=args.translate_to,
        )
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
        content = render(tr, args.format, title=path.stem)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(str(Path(args.output)))
    else:
        sys.stdout.write(content)
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

    # compute
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            line("device", f"{name} (torch {torch.__version__})")
        else:
            line("device", f"cpu (torch {torch.__version__})")
    except ImportError:
        line("device", "torch not installed")

    line("input root", str(default_input_root() or "unconfined (CLI default)"))
    line("output root", str(output_root()))
    line("jobs store", os.environ.get("TEXTFLOWKIT_DB", "in-memory (not durable)"))
    return 0


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
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "sources":
        return _cmd_sources(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


