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
git clone https://github.com/scottconverse/textflowkit
cd textflowkit
pip install -e .
```

Or install the built wheel from the release page:

```bash
pip install https://github.com/scottconverse/textflowkit/releases/download/v0.1.0/textflowkit-0.1.0-py3-none-any.whl
```

Not published to PyPI; the repository and its releases are the distribution path.
The release page lists each artifact's SHA-256. (Those values are deliberately
kept out of this file: the README is bundled into the wheel as its description,
so a hash written here would change the artifact it describes.)
Optional extras:

```bash
pip install -e ".[mcp]"    # MCP server adapter
pip install -e ".[http]"   # HTTP API adapter
pip install -e ".[dev]"    # tests + linter
```

## Usage

```bash
# transcribe a URL or a local file
textflowkit transcribe "https://www.youtube.com/watch?v=..."

# pick formats and an output directory
textflowkit transcribe ./talk.mp4 --formats srt,vtt,txt,json --output-dir ./out

# force a language instead of auto-detecting
textflowkit transcribe "$URL" --language en

# translate the transcript (uses the configured backend)
textflowkit transcribe "$URL" --translate-to Spanish

# label speakers (requires the optional extra and a Hugging Face token)
textflowkit transcribe "$URL" --diarize

# resume a previous run instead of starting over
textflowkit transcribe "$URL" --resume
```

**Resume** reuses completed work from an earlier run. It needs two things: the
same source, model, language, and options as the original run, and a durable job
store (`TEXTFLOWKIT_DB`) - a checkpoint cannot outlive a process that kept it in
memory. The source file must still exist; a resumed run re-validates it rather
than trusting a stale path.

```bash

# re-render an existing transcript in another format
textflowkit export ./transcript.json --format vtt
```

## Use as an MCP server

```bash
pip install -e ".[mcp]"
textflowkit-mcp                                  # stdio
textflowkit-mcp --transport http --port 8766     # Streamable HTTP
```

Tools: `transcribe_media`, `get_job_status`, `get_transcript`,
`export_transcript`, `list_sources`, `list_jobs`, `cancel_job`,
`search_transcript`.

**Verified against DSH, Claude Code, and OpenCode** - verified that each
harness's own MCP client connects and sees the tools. This is **not** an
end-to-end transcription run driven by each harness; no harness was asked to
complete a real transcription through the tools:

- **DSH** - the server spawned as a child of the harness's MCP client, which
  then completed an MCP handshake, discovered all 8 tools, and returned real
  data from a `list_sources` call.
- **Claude Code** - `claude mcp list` reports `textflowkit: √ Connected` (stdio).
- **OpenCode** - `opencode mcp list` reports `textflowkit connected` over
  Streamable HTTP.

**Codex** is configured but not live-verified: the entry is present in
`~/.codex/config.toml`, and the CLI could not be exercised because an unrelated
model-catalog file in that config fails to parse. That is a pre-existing issue
with the Codex configuration, not with this server.

There is also a protocol test that launches the server as a real subprocess and
speaks newline-delimited JSON-RPC over stdio, so the entry point, framing, and
version negotiation are covered on every CI run (`tests/test_stdio_protocol.py`).
See [docs/adapters.md](docs/adapters.md) for per-harness configuration.

## Use as an HTTP API

```bash
pip install -e ".[http]"
textflowkit-http --port 8767
```

Submit a job, poll it, fetch the transcript. No authentication is bundled —
bind to localhost or front it with your own gateway.

## Durable, bounded, cancellable

```bash
TEXTFLOWKIT_DB=./jobs.db            # job state survives restart (SQLite)
TEXTFLOWKIT_MAX_CONCURRENCY=1        # default; Whisper saturates a GPU alone
```

`cancel_job` stops a queued job immediately, or a running job at its next stage
boundary. See [docs/adapters.md](docs/adapters.md).

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





