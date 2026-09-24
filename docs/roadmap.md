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
- [x] Core resume and batch shared by CLI, MCP, and HTTP
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
- [ ] Ship v0.1.5 review follow-ups: speech-bearing self-test, full media
  duration, retained word timings, Python API documentation, richer PyPI
  project links, a small core wheel with optional offline fonts, and a
  tokenless Trusted Publishing release workflow for both packages

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
