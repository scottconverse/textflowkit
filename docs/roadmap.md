# Roadmap

## v0.1.0 — current

- [x] Canonical transcript model (`Segment`, `Transcript`)
- [x] Platform detection for 13 sources + local + direct
- [x] Media acquisition via `yt-dlp` (CLI + in-process module fallback)
- [x] Audio extraction via `ffmpeg`
- [x] openai-whisper engine with automatic device selection
- [x] Renderers: TXT, SRT, VTT, JSON, Markdown
- [x] CLI (`transcribe`, `export`, `sources`)
- [x] Test suite

## Next

- [x] Verified end-to-end on local file and live URL (YouTube)
- [x] SSRF guard on user-supplied URLs
- [x] Confined output paths (`TEXTFLOWKIT_OUTPUT_ROOT`)
- [x] Confined input paths (`TEXTFLOWKIT_INPUT_ROOT`)
- [x] Non-loopback HTTP binds refused unless explicit
- [x] CI: ruff + pytest on Python 3.10-3.13
- [x] JavaScript runtime auto-detection for yt-dlp (deno/node/bun/quickjs)
- [x] MCP server adapter (stdio + Streamable HTTP)
- [x] HTTP service adapter (FastAPI, job-based)
- [x] Translation stage (`--translate-to`, local Ollama backend)
- [x] Speaker diarization (`--diarize`, optional pyannote backend)
- [x] Durable job store (SQLite via `TEXTFLOWKIT_DB`)
- [x] Bounded concurrency (`TEXTFLOWKIT_MAX_CONCURRENCY`)
- [x] Job cancellation (`cancel_job`)
- [x] Transcript paging, time-range reads, and search
- [x] `doctor` diagnostic for yt-dlp / JS runtime / extras / device
- [ ] Resumable / batched jobs
- [x] DOCX and PDF export (`export` extra)

## Design constraints

- Platform differences stay in the source layer. The pipeline never branches on
  platform.
- Adapters stay thin. No pipeline logic outside `core/`.
- The canonical transcript JSON is the contract between every stage and every
  consumer.




