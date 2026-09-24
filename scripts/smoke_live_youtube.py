"""Opt-in, live-network release smoke for a public YouTube clip.

This is intentionally outside deterministic pytest/PR CI. It exercises URL
acquisition, ffmpeg, Whisper and JSON rendering through the installed CLI, then
checks that the transcript says something recognizable: a caller-named word or
short phrase, defaulting to a word the documented default clip is observed to
say. That is an assertion about one clip, not a transcription-accuracy test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
# One whole word the default clip is observed to say. Measured 2026-09-24 on
# this checkout with `--model tiny --device cpu`: the first of its three
# segments is "Alright so here we are one of the elephants." Whisper output can
# drift, but a plausible unrelated clip cannot produce this word, so it
# separates recognized speech from any nonempty timed text. It is one word of
# one URL, not a transcript-accuracy claim.
DEFAULT_EXPECT_TEXT = "elephants"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalize_words(text: str) -> list[str]:
    """Lowercase whole words, dropping punctuation, for phrase comparison.

    Apostrophes stay inside a word so "that's" is one token rather than two.
    """
    words: list[str] = []
    current: list[str] = []
    for char in text.replace("’", "'").lower():
        if char.isalnum() or char == "'":
            current.append(char)
        elif current:
            words.append("".join(current).strip("'"))
            current = []
    if current:
        words.append("".join(current).strip("'"))
    return [word for word in words if word]


def _contains_whole_phrase(haystack: list[str], phrase: list[str]) -> bool:
    """True if `phrase` appears in `haystack` as consecutive whole words."""
    if not phrase:
        return False
    width = len(phrase)
    return any(
        haystack[index:index + width] == phrase
        for index in range(len(haystack) - width + 1)
    )


def _candidate_commit() -> str | None:
    """Require an attributable, clean source checkout for a release receipt."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"live smoke failed: cannot inspect candidate commit: {exc}", file=sys.stderr)
        return None
    if commit.returncode != 0 or status.returncode != 0 or len(commit.stdout.strip()) != 40:
        print("live smoke failed: run from a Git checkout with a valid HEAD", file=sys.stderr)
        return None
    if status.stdout.strip():
        print("live smoke failed: checkout has uncommitted changes; commit the candidate first",
              file=sys.stderr)
        return None
    return commit.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--receipt", type=Path, help="optional JSON receipt path")
    parser.add_argument(
        "--expect-text",
        default=None,
        help="word or short phrase expected in the transcript; required when "
             f"--url is not the default clip, whose default is {DEFAULT_EXPECT_TEXT!r}",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    # Settle the content expectation before any Git, download, or model work:
    # the default clip's word says nothing about any other clip.
    if args.expect_text is not None:
        expect_text = args.expect_text
        expect_text_source = "caller-supplied"
    elif args.url == DEFAULT_URL:
        expect_text = DEFAULT_EXPECT_TEXT
        expect_text_source = "default-clip"
    else:
        parser.error(
            "--expect-text is required when --url is not the default clip; name a "
            "word or short phrase you expect to hear there"
        )
    expected_words = _normalize_words(expect_text)
    if not expected_words:
        parser.error("--expect-text must contain at least one letter or digit")
    receipt_path = args.receipt.expanduser().resolve() if args.receipt is not None else None
    if receipt_path is not None:
        if receipt_path == REPO_ROOT or REPO_ROOT in receipt_path.parents:
            print("live smoke failed: save the receipt outside the repository", file=sys.stderr)
            return 1
        if receipt_path.exists():
            print("live smoke failed: receipt already exists; choose a new path", file=sys.stderr)
            return 1
    commit = _candidate_commit()
    if commit is None:
        return 1

    with tempfile.TemporaryDirectory(prefix="textflowkit-live-youtube-") as temp:
        root = Path(temp)
        env = dict(os.environ)
        env["TEXTFLOWKIT_PROFILE"] = "developer"
        env["TEXTFLOWKIT_DB"] = str(root / "jobs.db")
        env["TEXTFLOWKIT_OUTPUT_ROOT"] = temp
        env["TEXTFLOWKIT_WORK_ROOT"] = str(root / "work")
        # An installed wheel may be older than this checkout. Force the child
        # CLI to import the committed candidate source named by the receipt.
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
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
            spoken_words = _normalize_words(" ".join(s["text"] for s in segments))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"live smoke failed: unparseable or invalid transcript: {exc}",
                  file=sys.stderr)
            return 1
        if not _contains_whole_phrase(spoken_words, expected_words):
            print(
                f"live smoke failed: expected text {expect_text!r} not found as whole "
                f"word(s) in the transcript of {args.url}",
                file=sys.stderr,
            )
            return 1
        if _candidate_commit() != commit:
            print("live smoke failed: checkout changed during transcription", file=sys.stderr)
            return 1
        receipt = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": commit,
            "runner_os": platform.system(),
            "python_version": platform.python_version(),
            "url": args.url,
            "model": args.model,
            "device": args.device,
            "platform": payload["platform"],
            "segment_count": len(segments),
            "first_start": segments[0]["start"],
            "last_end": segments[-1]["end"],
            "transcript_sha256": hashlib.sha256(raw).hexdigest(),
            "content_assertion": {
                "expected_text": expect_text,
                "expected_text_source": expect_text_source,
                "matched": True,
                "scope": (
                    "whole word(s) present in the normalized concatenated segment "
                    "text of this one URL; not a general transcript-accuracy "
                    "certification"
                ),
            },
        }
        encoded = json.dumps(receipt, indent=2)
        if receipt_path is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            with receipt_path.open("x", encoding="utf-8") as saved:
                saved.write(encoded + "\n")
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
