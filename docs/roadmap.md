# Roadmap

## v0.1.0 — original release

- [x] Canonical transcript model (`Segment`, `Transcript`)
- [x] Platform detection for 13 sources + local + direct
- [x] Media acquisition via `yt-dlp`
- [x] Audio extraction via `ffmpeg`
- [x] openai-whisper engine with automatic device selection
- [x] Renderers: TXT, SRT, VTT, JSON, Markdown
- [x] CLI (`transcribe`, `export`, `sources`)
- [x] Test suite

## v0.1.1 — stabilization

- [x] Verified end-to-end on local file and live URL (YouTube)
- [x] SSRF guard on user-supplied URLs
- [x] Confined output paths (`TEXTFLOWKIT_OUTPUT_ROOT`)
- [x] Confined input paths (`TEXTFLOWKIT_INPUT_ROOT`)
- [x] Non-loopback HTTP binds refused unless explicit
- [x] CI: ruff + pytest on Python 3.10-3.13
- [x] JavaScript runtime auto-detection for yt-dlp (deno/node/bun/quickjs)
- [x] MCP server adapter (stdio + Streamable HTTP)
- [x] HTTP service adapter (FastAPI, job-based)
- [x] Translation stage (`--translate-to`, explicitly configured Ollama model)
- [x] Speaker diarization (`--diarize`, optional pyannote backend)
- [x] Durable job store (SQLite via `TEXTFLOWKIT_DB`)
- [x] Bounded concurrency (`TEXTFLOWKIT_MAX_CONCURRENCY`)
- [x] Job cancellation (`cancel_job`)
- [x] Transcript paging, time-range reads, and search
- [x] `doctor` diagnostic for yt-dlp / JS runtime / extras / device
- [x] `selftest` for verifying the compute path on a given machine
- [x] Translation transport tested in CI against a stub Ollama
- [x] Resumable jobs (`--resume`, checkpoint per source) and `batch` subcommand
- [x] MCP protocol verified over real stdio (`tests/test_stdio_protocol.py`)
- [x] DOCX and PDF export (`export` extra)
- [x] Executor survives unexpected failures, with a bounded pending queue
- [x] Collision-resistant output names, atomic writes, and scratch cleanup
- [x] Shared submission and resume core for CLI, MCP, and HTTP - every path submits
  through `core.submission.submit_request` and resumes through the same core; the
  CLI's `batch` adds its own synchronous per-item report loop, while MCP/HTTP
  `submit_batch` only returns job handles
- [x] Explicit translation model; separate Whisper/pyannote device reporting
- [x] Cached model objects and ROCm/CUDA selection for diarization
- [x] CLI binary exports and Unicode-capable PDF fonts
- [x] Lean job status, consistent list limits, and correct empty search results
- [x] Opt-in authenticated JSON HTTP production profile with roots and limits
- [x] Fresh installed-wheel smoke of all three entry points on Windows, macOS, and Linux

## v0.1.2 — release evidence

- [x] Replace the blocked GitHub-hosted YouTube workflow with a documented,
  receipt-producing Windows maintainer check for the exact clean candidate commit
- [x] Keep the deterministic cross-platform GitHub CI as the automated gate
- [x] Align the release-facing documentation and package version

## v0.1.3 — developer landing and maintenance

- [x] Publish a static GitHub Pages landing page from `main` `/docs`
- [x] Correct the default input/output path-confinement documentation
- [x] Update pinned CI checkout and Python setup actions and verify the combined matrix

## Website hosting

- [x] Move the static landing page to Cloudflare Pages Free, connected to the
  GitHub `main` branch with `docs/` as the site root
- [x] Serve `www.textflowkit.org` over HTTPS and redirect the apex domain to it

See [site deployment](site-deployment.md) for the hosting configuration. This
static page is documentation and a product landing page, not a hosted
transcription service.

## Package distribution

- [x] Publish the verified v0.1.3 wheel and source archive on PyPI
- [x] Prepare v0.1.4 with a PyPI-first package description and install guide
- [x] Make PyPI the default developer install path while keeping GitHub
  releases and the native-Windows ROCm dependency instructions
- [x] Ship v0.1.5 review follow-ups: speech-bearing self-test, full media
  duration, retained word timings, Python API documentation, richer PyPI
  project links, a small core wheel with optional offline fonts, and a
  tokenless Trusted Publishing release workflow for both packages

The [v0.1.5 GitHub release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.5)
and both [core](https://pypi.org/project/textflowkit/0.1.5/) and
[fonts](https://pypi.org/project/textflowkit-fonts/0.1.5/) PyPI projects are live.
The first Trusted Publishing run succeeded; all four GitHub/PyPI artifact hashes
matched, and a fresh install passed self-test, transcription, and PDF export.
- [x] Ship v0.1.6 review follow-ups: the post-v0.1.5 review repair set — media
  acquisition and adapter security hardening, safer subtitle wrapping and
  output-file publication, job and checkpoint storage corrections, an opt-in
  faster-whisper engine, a container example, and fail-closed release gates for
  tagged versions, README claims, and exact-commit CI. The repairs are merged
  and the release is public: the
  [v0.1.6 tag](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.6)
  and both [core](https://pypi.org/project/textflowkit/0.1.6/) and
  [fonts](https://pypi.org/project/textflowkit-fonts/0.1.6/) PyPI projects are
  live, merged-main CI passed 16/16 on the tagged commit, the published wheel
  and sdist digests match the GitHub release assets, the landing page serves
  v0.1.6, and a fresh install passed `doctor`, `selftest`, and a CPU
  transcription to JSON and PDF.
- [x] Ship v0.1.7 review follow-ups: the 2026-10-01 audit repair set — a confined
  decoder boundary for rooted local inputs, atomic job-attempt ownership and
  cancellation finalization, atomic worker startup, DOCX completed-output
  identity, per-request completed-stage reuse, an aggregate export preflight,
  translation completeness and per-item batch validation, live terminal
  feedback, and the DOC-001 – DOC-006 documentation corrections. Implementation verification is complete; publication and its exact-commit CI are tracked by the [v0.1.7 release workflow](https://github.com/scottconverse/textflowkit/actions/workflows/publish-pypi.yml). Fonts remain 0.1.6 and are reused without republishing. Historical release evidence above remains scoped to its stated version.
- [x] Ship v0.1.8 review follow-ups: the 2026-10-01 post-release audit-lite
  follow-ups (AL-001 – AL-004). AL-001 gives the CLI owning-process startup
  recovery at its process/store boundary, so an interrupted job's saved work can
  be resumed instead of being refused as "already active"; AL-002 validates and
  rehydrates reusable work **before** publishing a checkpoint, so a resume setup
  failure can no longer replace a durable completed transcript with an empty one;
  AL-003 corrects the MCP batch cookie-capability documentation and its
  schema-consistency test; AL-004 corrects roadmap evidence receipts. These are
  the four fixes recorded for [v0.1.8](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.8),
  whose publication and exact-commit CI are tracked by the [release workflow](https://github.com/scottconverse/textflowkit/actions/workflows/publish-pypi.yml).
  Fonts remain 0.1.6 and are reused without republishing. This entry does not
  restate or rewrite the historical v0.1.7 achievements below, whose evidence
  remains scoped to its stated version.
- [x] Normalize exported file permissions on POSIX to respect the process
  umask. The mode is measured and applied before publication, with regression
  tests for it. The POSIX behaviour is now verified on live POSIX hosts: the
  Linux/macOS jobs of the current-main [CI run 36961982098](https://github.com/scottconverse/textflowkit/actions/runs/36961982098)
  run the full suite (12 OS/Python test jobs plus three installed-wheel jobs
  and Ruff) on the audited commit, and `tests/test_export_file_mode.py` carries
  the six umask/publication cases (Windows-only excluded). This is no longer
  unverified.
- [x] Decouple the fonts package's version from core releases so unchanged font
  wheels are not rebuilt/uploaded every patch. The publish workflow resolves the
  fonts version against PyPI before it builds: an unpublished version is built
  and uploaded, and a published one is fetched from PyPI with its recorded
  SHA-256 and size verified and is never rebuilt, re-uploaded, or hidden behind
  `skip-existing`. A changed fonts package must carry a new version, and the
  fonts upload and the core upload are conditional so the reuse path still
  publishes the core release. See the
  [release checklist](release-checklist.md#the-fonts-version-contract-issue-15).
  The reuse path is now exercised by a live release: the v0.1.7 build
  ([release workflow run 36961162844](https://github.com/scottconverse/textflowkit/actions/runs/36961162844))
  resolved the unchanged 0.1.6 fonts for reuse, **skipped** the fonts upload,
  and published core to PyPI successfully. One nuance stays recorded rather than
  papered over: that same run's GitHub-release job was skipped by a transitive
  condition, and GitHub publication was recovered manually from the original
  verified artifacts; PR #23 repaired the condition on main, so the repaired
  future automatic GitHub-release path has deterministic tests, **not** a second
  live tag run. This entry does not claim the original workflow was all green, nor
  that the repaired publication path has been live-tested.

The 13 listed media platforms are recognised through `yt-dlp`; **only YouTube**
has an opt-in [live URL transcription release gate](release-checklist.md), not
a deterministic pull-request job. The release gate runs locally on Windows,
not on GitHub-hosted runners that were challenged as bots. Production URL jobs additionally
require an operator-provided SSRF-filtering egress proxy. These are deliberate
verification/deployment boundaries, not claims of complete platform coverage.

## Design constraints

- Platform differences stay in the source layer. The pipeline never branches on
  platform.
- Adapters stay thin. No pipeline logic outside `core/`.
- The canonical transcript JSON is the contract between every stage and every
  consumer.

## Later fixes — 2026-10-01 audit

**Status: deferred and still open.** The owner deferred these items on
2026-10-01. They are outside the current core-bug repair goal, are not claimed
fixed in any release, and have no committed delivery date. Finding IDs refer to
the full five-role audit of v0.1.6 main commit
`c2553a0929d8b061cc7f55bfbb736161827d25fc`.

| Finding | Later fix | Completion criterion |
|---|---|---|
| TEST-001 | Recurring installed-package transcription/adapter validation | An isolated installed distribution completes real default-engine speech through the adapters in a recurring validation lane, with version/commit receipts; source-checkout tests alone do not close it. |
| TEST-002 | Real optional-backend compatibility validation | Isolated runs establish supported optional-engine/provider versions without altering the working ROCm stack; unavailable models, credentials or platforms remain explicitly unverified. |
| TEST-003 | Missing-pyannote test oracle | Force the missing dependency deterministically and require the intended missing-extra error; unrelated provider failures must not pass the test. |
| TEST-004 | Translation test oracle | A bounded known-language check establishes meaningful translation, rather than accepting any nonempty or different string. |
| TEST-005 | Current coverage provenance | Produce fresh commit-stamped branch-coverage evidence and a recurring artifact; identify untested branches without treating a coverage percentage as runtime proof. |
| UX-004 | Mobile command-panel scrolling cue | Make horizontally scrolling command examples discoverable at narrow widths and verify the rendered page. |
| UX-005 | Mobile secondary-link ergonomics | Improve small link hit areas and verify narrow-screen layout/interaction; do not claim accessibility conformance from size alone. |
| DOC-008 | Immutable PyPI description erratum | Document the v0.1.6 description's stale evidence wording and correct the next distribution description without claiming historical PyPI metadata was rewritten. |
| UX-003 | Quiet-batch failure diagnostics | Keep failed source/item identity and actionable failure reasons on stderr in quiet mode, while preserving machine-readable stdout. |
| DOC-007 | Diagnostic and developer wording | Align module-versus-executable acquisition, availability-versus-version wording, unrestricted root labels, and durable-storage descriptions with actual behavior. |

Deferral does not remove these findings or waive their acceptance criteria.
Core engineering/runtime bugs and the major documentation corrections remain
in the active repair goal. See the [unreleased changelog](../CHANGELOG.md).
