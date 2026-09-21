"""textflowkit command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from textflowkit import __version__
from textflowkit.core.model import Transcript
from textflowkit.core.pipeline import PipelineError, transcribe
from textflowkit.render import SUPPORTED_FORMATS, render
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
    t.add_argument("--speaker-labels", action="store_true", help="enable speaker labelling (engine-dependent)")
    t.add_argument("--cookies-from-browser", default=None,
                   help="pass cookies to yt-dlp from a browser (e.g. firefox) for access-controlled content")
    t.add_argument("--stdout", action="store_true", help="print transcript to stdout instead of writing files")
    t.add_argument("--stdout-format", default="txt", help="format for --stdout (default txt)")
    t.add_argument("--quiet", "-q", action="store_true", help="suppress progress messages")

    l = sub.add_parser("export", help="re-render an existing transcript JSON")
    l.add_argument("transcript", help="path to a transcript .json file")
    l.add_argument("--format", "-f", default="srt", help=f"output format ({', '.join(SUPPORTED_FORMATS)})")
    l.add_argument("--output", "-o", default=None, help="output file (default stdout)")

    s = sub.add_parser("sources", help="list recognised platforms")
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
            speaker_labels=args.speaker_labels,
            cookies_from_browser=args.cookies_from_browser,
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
    except Exception as exc:
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
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
