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
- [ ] Follow-up: normalize exported file permissions on POSIX to respect the
  process umask. Current atomic temporary files can leave outputs mode `0600`,
  preventing another account (such as a separate web server user) from reading
  exported subtitles or documents. Fix and regression-test separately.
  The mode is now measured and applied before publication, with regression
  tests for it, but the POSIX behaviour has not been verified on a live POSIX
  host yet, so this stays open until CI or a POSIX machine confirms it.
- [ ] Follow-up: decouple the fonts package's version from core releases so
  unchanged font wheels are not rebuilt/uploaded every patch. The publish
  workflow now resolves the fonts version against PyPI before it builds: an
  unpublished version is built and uploaded, and a published one is fetched
  from PyPI with its recorded SHA-256 and size verified and is never rebuilt,
  re-uploaded, or hidden behind `skip-existing`. A changed fonts package must
  carry a new version, and the fonts upload and the core upload are conditional
  so the reuse path still publishes the core release. See the
  [release checklist](release-checklist.md#the-fonts-version-contract-issue-15).
  This stays open until a release actually reuses a published fonts version on
  PyPI. v0.1.6 exercised only the new-fonts path, where 0.1.6 was not yet
  published and both packages were built and uploaded; the reuse path is still
  covered by deterministic tests over injected index responses, not by a live
  tag.

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
