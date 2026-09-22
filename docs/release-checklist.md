# Release verification checklist

This checklist separates deterministic CI from a live network/site/model check.
It does not make the other 12 recognized sites end-to-end verified.

1. Run `ruff check .` and `python -m pytest` on the candidate commit. Confirm
   the Windows/Linux/macOS Python matrix and installed-wheel smoke pass in CI.
2. On a machine with Python, ffmpeg, yt-dlp dependencies, and network access,
   run the opt-in public-video smoke from the candidate checkout:

   ```bash
   python scripts/smoke_live_youtube.py --receipt live-youtube-receipt.json
   ```

   The default URL is a public YouTube clip. The script runs the real CLI with
   Whisper `tiny` on CPU, requires one parseable JSON transcript, at least one
   nonempty timestamped segment, and `platform=youtube`. It exits nonzero for
   download, timeout, transcription, render, or structure failure. Its default
   timeout is 300 seconds; `--url`, `--model`, `--device`, and `--timeout` may be
   overridden deliberately and recorded with the receipt.
3. Alternatively dispatch the `Live YouTube transcription smoke` GitHub Actions
   workflow manually on the candidate ref. Download and retain its JSON receipt.
4. If the site blocks automation, the video changes, or the model output drifts,
   record the failure and investigate. Do not turn a failed live gate into a
   green claim by pointing to deterministic mocked URL tests. A maintainer may
   choose a new public fixture URL and update this checklist/script in review.
5. Only after the live smoke, deterministic matrix, and release asset hashes
   agree with the candidate commit should release notes say that YouTube has a
   maintained live URL gate. State the date/ref/receipt and distinguish this
   one URL from the 12 other recognized sites.

The script writes media/transcripts to an isolated temporary directory and
deletes it. Save its small JSON receipt outside the repository or as a CI
artifact; do not commit downloaded media or transcripts.
