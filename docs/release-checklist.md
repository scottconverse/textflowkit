# Release verification checklist

Deterministic GitHub CI and live YouTube evidence are **different gates**. The
live check runs on a Windows maintainer machine, not a GitHub-hosted runner.
The [hosted attempt on 2026-09-23](https://github.com/scottconverse/textflowkit/actions/runs/35805762808)
failed when YouTube required bot confirmation. A local Windows run passed, but
neither result verifies the other 12 recognized sites.

1. Start from a **committed, clean candidate checkout** on Windows. Use a Python
   environment with textflowkit, `yt-dlp`, Whisper, and ffmpeg/ffprobe installed;
   see [install notes](install.md). The script refuses uncommitted changes so
   its receipt identifies the code that ran. It uses Whisper `tiny` on CPU; no
   ROCm GPU setup is required. The script does not supply browser cookies;
   YouTube may still challenge any particular network or run.
2. Run `ruff check .` and `python -m pytest` on the candidate commit. Confirm
   the Windows/Linux/macOS Python matrix and installed-wheel smoke pass in the
   deterministic [CI workflow](../.github/workflows/ci.yml).
3. From the repository root in PowerShell, write a live receipt **outside** the
   checkout. With the documented `.venv` setup:

   ```powershell
   $receipt = Join-Path $env:USERPROFILE ("Documents\textflowkit-youtube-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")
   .\.venv\Scripts\python.exe scripts\smoke_live_youtube.py --receipt $receipt
   if ($LASTEXITCODE -ne 0) { throw "Live YouTube check failed; do not claim a pass" }
   Get-Content -LiteralPath $receipt
   ```

   The script runs the candidate checkout's source through the real CLI, URL
   acquisition, ffmpeg, Whisper, and JSON rendering. It requires
   `platform=youtube`, at least one nonempty
   timestamped segment, **and recognizable spoken content**. By default it
   requires the whole word `elephants`, which the documented default clip is
   observed to say in its first segment with `--model tiny`. Pointing `--url`
   at any other clip requires
   `--expect-text "<a word or short phrase you expect to hear>"`: the run is
   refused, before it touches Git, the network, or a model, because a word from
   the default clip proves nothing about a different one. The expected text is
   matched against the concatenated segment text as whole words; case,
   punctuation, and segment boundaries are ignored, but word boundaries are
   not, so `elephant` does not match `elephants` and neither matches
   `elephantiasis`. This asserts that this one URL yielded speech a human
   recognizes — it is not a transcript-accuracy certification, and it is not
   evidence about the other 12 recognized sites. The receipt records UTC time,
   candidate Git commit, Windows/Python runtime, URL, model/device, the
   expected text and whether it came from the default clip or the caller, the
   assertion's scope and result, segment count, and transcript SHA-256. It does
   **not** record the transcript. Its default timeout is 300 seconds; `--url`,
   `--model`, `--device`, `--expect-text`, and `--timeout` may be overridden
   deliberately and are reflected in the receipt. No success receipt is written
   on failure, including a content mismatch.
4. Keep the receipt with the release evidence, not in the public repository.
   If YouTube blocks access, the video changes, or transcription drifts, record
   the failure and investigate. Do **not** replace a failed live run with a
   green mocked URL test, an old receipt, or the blocked GitHub-hosted attempt.
   A new public fixture URL requires review and an updated script/checklist —
   in particular a new default URL needs a new observed default word, since
   `elephants` belongs to the current one.
   Receipts written before the content assertion existed — the v0.1.5 receipt
   among them — record platform, timestamps, and hashes only: a nonempty timed
   segment was accepted then. Read one as shape evidence for that commit, never
   as evidence of recognized speech, and do not carry it forward as a
   content-verified receipt for a later release. Only receipts that contain
   `content_assertion` make the content claim.
5. After merging, re-run the local check on the **merged, clean `main` commit**;
   a pre-merge branch receipt does not identify the release commit. Only if the
   current live receipt, deterministic merged-main matrix, and release asset
   hashes agree with that commit may release notes claim a
   live YouTube check. State its date/commit/receipt and distinguish this one
   URL from the other 12 recognized platforms. Do not describe GitHub-hosted
   YouTube automation as a passing gate.

The script deletes its temporary media and transcript after producing the
receipt. It does not upload cookies, media, or transcripts to GitHub.

## PyPI publication

After merged-main CI and the local release checks pass, push an annotated final
version tag (for example `v0.1.6` for a future release) on that verified
`main` commit. Do **not**
create or publish a GitHub release manually. The tag push triggers
[`publish-pypi.yml`](../.github/workflows/publish-pypi.yml). It verifies the
tag, both package versions, and ancestry on `main`; it then requires a
completed, successful `push` run of
[`ci.yml`](../.github/workflows/ci.yml) on `main` whose `head_sha` is the
exact tagged commit, using
[`scripts/verify_release_ci.py`](../scripts/verify_release_ci.py) with the
workflow's `GITHUB_TOKEN` and `actions: read`. A pull-request run, a run for a
different commit, a run still in progress, or a failed run all stop the build
before any distribution is built or uploaded, and an API error or an
unreadable response stops it too. Only then does it build the four artifacts,
publishes `textflowkit-fonts` first and `textflowkit` second using PyPI Trusted
Publishing, then hashes the downloaded distributions with
[`scripts/write_release_manifest.py`](../scripts/write_release_manifest.py) and
creates a **draft** GitHub release with those exact artifacts plus the resulting
`SHA256SUMS` asset. Only after the assets are attached does it make that release
public. The step refuses to write a manifest unless it finds exactly the two
wheels and two sdists from this release's version, so a missing, duplicated, or
stray artifact stops the release instead of publishing misleading hashes.
`SHA256SUMS` is a release **asset**, a sidecar of the attached files; it is not
a table inside the release notes, which GitHub generates. Only releases published
after this step was added have one: v0.1.5 and earlier releases predate it and
have no manifest to compare.
No upload token is passed to CI. Both PyPI projects must have trusted publishers
for owner `scottconverse`, repository `textflowkit`, workflow
`publish-pypi.yml`, and environment `pypi`. The GitHub `pypi` environment
should require maintainer approval before an upload job can proceed.

The automated gate is evidence about the exact tagged commit, and about
`ci.yml` only. It is not evidence about anything else:

- It says nothing about the live YouTube receipt. That remains the separate
  manual Windows gate in steps 1-5; a green hosted run never satisfies it, and
  steps 1-5 never substitute for the CI gate.
- It proves the tagged commit was a green `main` push, not that the tag is the
  current tip of `main`; the workflow still checks ancestry only. Tag the
  verified merged-main commit named by the live receipt.
- A GitHub-hosted run cannot test the other 12 recognized sites, so this gate
  does not extend any platform claim.
- If it stops a release, fix the cause and rerun the workflow; if the tag
  itself is wrong, delete and re-push it on the verified commit rather than
  moving an existing tag.

This is not a transaction across two PyPI projects and GitHub. If fonts upload
succeeds but core fails, **there is no public GitHub release**, but fonts are
already on PyPI. Check whether any core files reached PyPI before retrying:
PyPI will reject duplicate filenames, and this workflow intentionally does not
silently skip them. Correct the cause and rerun only failed jobs when safe; if
some core files are present, reconcile their hashes against the original build
artifact and finish the missing files deliberately. If both PyPI uploads succeed
but draft creation or publishing fails, do not republish either PyPI project;
complete the GitHub draft and assets from the exact build artifact after hash
verification. Do not move the published version tag or claim a complete release
until all four PyPI files and all four GitHub assets match.

After publication, compare the `SHA256SUMS` release asset against the GitHub
release assets and the PyPI SHA-256 digests for **both** packages, and
clean-install the exact version with
`textflowkit[export,mcp,http]`. Run `doctor`, `selftest`, and an actual PDF export.
An install with `--no-deps` only proves distribution wiring, not transcription.

Never place an API token in this repository, a CI log, or a shell command line.
An upload is a separate public release action; passing CI alone does not
authorize it. Do not call a release complete if either package upload failed.
