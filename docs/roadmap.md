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
- [x] JavaScript runtime auto-detection for yt-dlp (deno/node/bun/quickjs)
- [ ] MCP server adapter (`adapters/mcp.py`)
- [ ] HTTP service adapter (`adapters/http.py`)
- [ ] Translation stage (`--translate-to`)
- [ ] Speaker diarization (optional dependency)
- [ ] Resumable / batched jobs
- [ ] DOCX and PDF export

## Design constraints

- Platform differences stay in the source layer. The pipeline never branches on
  platform.
- Adapters stay thin. No pipeline logic outside `core/`.
- The canonical transcript JSON is the contract between every stage and every
  consumer.


