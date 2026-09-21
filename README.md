# textflowkit

Cross-platform media transcription toolkit. **One core, one CLI, thin adapters.**

Paste a URL or point at a file; get timestamped transcripts and subtitle files back.
Built as a reusable primitive for developers — designed to sit under multiple
products, AI harnesses, and agents.

---

> ## ⚠️ NO WARRANTY — AS IS
>
> **This software is provided "AS IS", WITHOUT WARRANTY OF ANY KIND**, express or
> implied, including but not limited to the warranties of MERCHANTABILITY, FITNESS
> FOR A PARTICULAR PURPOSE, and NONINFRINGEMENT. See [LICENSE](LICENSE) (Apache-2.0,
> §7–8) for the full disclaimer and limitation of liability.
>
> **You are responsible for what you transcribe.** textflowkit can fetch media from
> third-party platforms. Copyright, terms-of-service, and privacy obligations for any
> media you choose to process are **yours alone**. See [LEGAL.md](LEGAL.md).

---

## What it does

```
URL or file  ─►  detect platform  ─►  acquire media  ─►  ffmpeg
                                                          │
                              ┌───────────────────────────┘
                              ▼
                    speech-to-text (Whisper)
                              │
                              ▼
                 canonical transcript (JSON)
                              │
        ┌─────────┬───────────┼───────────┬──────────┐
        ▼         ▼           ▼           ▼          ▼
       TXT       SRT         VTT        JSON     Markdown
```

## Architecture

The design principle is **one engine, three doors**. Everything of substance lives in
the core; the interfaces are thin.

| Layer | Path | Responsibility |
|---|---|---|
| **Core** | `src/textflowkit/core` | Canonical transcript model, pipeline orchestration |
| **Sources** | `src/textflowkit/sources` | Per-platform URL normalization + media acquisition |
| **Renderers** | `src/textflowkit/render` | TXT / SRT / VTT / JSON / Markdown output |
| **CLI** | `src/textflowkit/cli.py` | Reference interface (subprocess-friendly) |
| **MCP** | `src/textflowkit/adapters/mcp_server.py` | stdio + Streamable HTTP, for AI harnesses |
| **HTTP** | `src/textflowkit/adapters/http_server.py` | JSON API, for software products and web frontends |

Because the core owns the pipeline, adding a door is cheap — and adding a platform
means writing one source adapter, not another tool.

## Supported sources

YouTube · TikTok · Facebook · Instagram · Vimeo · Twitch · Bilibili · Rumble ·
Kick · Zoom · Medal · Loom · Dropbox — plus **direct media URLs and local files**.

Platform coverage depends on `yt-dlp`; some sources require cookies or change
their access rules frequently. See [docs/sources.md](docs/sources.md).

## Install

Requires **Python ≥ 3.10** and **ffmpeg** on `PATH`.

```bash
pip install textflowkit
```

Optional extras:

```bash
pip install "textflowkit[mcp]"   # MCP server adapter
pip install "textflowkit[dev]"   # tests + linter
```

## Usage

```bash
# transcribe a URL or a local file
textflowkit transcribe "https://www.youtube.com/watch?v=..."

# pick formats and an output directory
textflowkit transcribe ./talk.mp4 --formats srt,vtt,txt,json --output-dir ./out

# force a language, translate afterward
textflowkit transcribe "$URL" --language en --translate-to es
```

## Use as an MCP server

```bash
pip install "textflowkit[mcp]"
textflowkit-mcp                                  # stdio
textflowkit-mcp --transport http --port 8766     # Streamable HTTP
```

Tools: `transcribe_media`, `get_job_status`, `get_transcript`,
`export_transcript`, `list_sources`, `list_jobs`.

Verified against **DSH**, Claude Code, Codex, and OpenCode. See
[docs/adapters.md](docs/adapters.md) for configuration for each.

## Use as an HTTP API

```bash
pip install "textflowkit[http]"
textflowkit-http --port 8767
```

Submit a job, poll it, fetch the transcript. No authentication is bundled —
bind to localhost or front it with your own gateway.

## Long jobs never block

Every interface is **job-based**: `transcribe_media` returns a job id
immediately and you poll for completion. That is what lets the same core serve a
CLI, AI harnesses, software products, and a future web frontend without
interface changes.

## Status

**v0.1.0 — early.** Core, CLI, MCP, and HTTP all work and are verified
end-to-end on real media. See [docs/roadmap.md](docs/roadmap.md).

## License

Apache-2.0 — see [LICENSE](LICENSE). Includes an explicit patent grant and a
limitation of liability.

## Contributing

Issues and PRs welcome. Please read [LEGAL.md](LEGAL.md) before adding a source
adapter.

