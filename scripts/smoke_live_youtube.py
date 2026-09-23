"""Opt-in, live-network release smoke for a public YouTube clip.

This is intentionally outside deterministic pytest/PR CI. It exercises URL
acquisition, ffmpeg, Whisper and JSON rendering through the installed CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--receipt", type=Path, help="optional JSON receipt path")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    with tempfile.TemporaryDirectory(prefix="textflowkit-live-youtube-") as temp:
        root = Path(temp)
        env = dict(os.environ)
        env["TEXTFLOWKIT_PROFILE"] = "developer"
        env["TEXTFLOWKIT_DB"] = str(root / "jobs.db")
        env["TEXTFLOWKIT_OUTPUT_ROOT"] = temp
        env["TEXTFLOWKIT_WORK_ROOT"] = str(root / "work")
        command = [
            sys.executable, "-m", "textflowkit.cli", "transcribe", args.url,
            "--model", args.model, "--device", args.device,
            "--formats", "json", "--output-dir", temp, "--quiet",
        ]
        try:
            run = subprocess.run(command, env=env, capture_output=True, text=True,
                                 timeout=args.timeout, check=False)
        except subprocess.TimeoutExpired:
            print(f"live smoke failed: transcription exceeded {args.timeout}s", file=sys.stderr)
            return 1
        if run.returncode != 0:
            print(f"live smoke failed (exit {run.returncode}): {run.stderr[-3000:]}",
                  file=sys.stderr)
            return 1
        files = list(root.glob("*.json"))
        if len(files) != 1:
            print(f"live smoke failed: expected one transcript JSON, found {len(files)}",
                  file=sys.stderr)
            return 1
        raw = files[0].read_bytes()
        try:
            payload = json.loads(raw)
            segments = payload["segments"]
            valid = bool(segments) and all(
                isinstance(s["start"], (int, float))
                and isinstance(s["end"], (int, float))
                and 0 <= s["start"] < s["end"]
                and isinstance(s["text"], str) and s["text"].strip()
                for s in segments
            )
            if payload.get("platform") != "youtube" or not valid:
                raise ValueError("missing platform, nonempty text, or valid timestamps")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"live smoke failed: unparseable or invalid transcript: {exc}",
                  file=sys.stderr)
            return 1
        receipt = {
            "url": args.url,
            "model": args.model,
            "device": args.device,
            "platform": payload["platform"],
            "segment_count": len(segments),
            "first_start": segments[0]["start"],
            "last_end": segments[-1]["end"],
            "transcript_sha256": hashlib.sha256(raw).hexdigest(),
        }
        encoded = json.dumps(receipt, indent=2)
        print(encoded)
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
