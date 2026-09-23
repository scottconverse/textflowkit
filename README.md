# textflowkit

Cross-platform media transcription toolkit. **One core, one CLI, thin adapters.**

[Project landing page](https://scottconverse.github.io/textflowkit/) ·
[GitHub releases](https://github.com/scottconverse/textflowkit/releases)

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

## Recognized sources

YouTube · TikTok · Facebook · Instagram · Vimeo · Twitch · Bilibili · Rumble ·
Kick · Zoom · Medal · Loom · Dropbox — plus **direct media URLs and local files**.

All 13 are recognised through `yt-dlp`; only YouTube has an opt-in, maintained
[live end-to-end smoke](docs/release-checklist.md). It is run on a Windows
maintainer machine before a release, not on GitHub-hosted runners or every pull
request. Local files have also been transcribed live. The other
12 are not release-verified end to end, and some sources require cookies or
change their access rules frequently. See [docs/sources.md](docs/sources.md).

## Install

Requires **Python ≥ 3.10** and **ffmpeg** on `PATH`.

```bash
git clone https://github.com/scottconverse/textflowkit
cd textflowkit
pip install -e .
```

Or install the built wheel from the release page:

```bash
pip install https://github.com/scottconverse/textflowkit/releases/download/v0.1.2/textflowkit-0.1.2-py3-none-any.whl
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
memory. For a local file, a completed-job resume checks the file still exists
and matches its checkpointed normalized path, size, and SHA-256 content digest.
Missing or changed files fail with an actionable error; v0.1.1-era local
checkpoints without a fingerprint must be resubmitted without `--resume`.
For URLs, resume deliberately reuses the saved transcript by URL/options; it
does **not** assert that the remote bytes are still identical.

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

Tools: `transcribe_media`, `submit_batch_media`, `resume_job`,
`get_job_status`, `get_transcript`,
`export_transcript`, `list_sources`, `list_jobs`, `cancel_job`,
`search_transcript`.

**Connection-smoked against DSH, Claude Code, OpenCode, and Codex desktop.**
These checks are not end-to-end transcription runs driven by each harness:

- **DSH** - the server spawned as a child of the harness's MCP client, which
  then completed an MCP handshake, discovered the tools, and returned real
  data from a `list_sources` call.
- **Claude Code** - `claude mcp list` reports `textflowkit: √ Connected` (stdio).
- **OpenCode** - `opencode mcp list` reports `textflowkit connected` over
  Streamable HTTP.

- **Codex desktop** - after repair of an unrelated model-catalog issue, a live
  `list_jobs` tool call succeeded. This does not prove every tool or a full
  transcription in Codex.

There is also a protocol test that launches the server as a real subprocess and
speaks newline-delimited JSON-RPC over stdio, so the entry point, framing, and
version negotiation are covered on every CI run (`tests/test_stdio_protocol.py`).
See [docs/adapters.md](docs/adapters.md) for per-harness configuration.

## Use as an HTTP API

```bash
pip install -e ".[http]"
textflowkit-http --port 8767
```

Submit a job, poll it, fetch the transcript. Developer mode is unauthenticated
and defaults to localhost. The opt-in JSON HTTP production profile requires a
Bearer token, explicit roots, durable SQLite jobs, and request/rate/media/output
limits; URL input additionally requires an SSRF-filtering egress proxy. See
[adapter deployment details](docs/adapters.md#developer-mode-and-production-profile).

## Durable, bounded, cancellable

```bash
TEXTFLOWKIT_DB=./jobs.db            # job state survives restart (SQLite)
TEXTFLOWKIT_MAX_CONCURRENCY=1        # default; Whisper saturates a GPU alone
```

`cancel_job` stops a queued job immediately, or a running job at its next stage
boundary. See [docs/adapters.md](docs/adapters.md).

## Long jobs never block

The MCP and HTTP adapters are **job-based**: submission returns a job id
immediately and clients poll for completion. The CLI uses the same core but
waits for the result. This lets AI harnesses, software products, and a future
web frontend share the job contract without blocking a request.

## Status

**v0.1.2 release.** Core, CLI, MCP, and HTTP have automated
coverage; Windows-native ROCm and a dated local Windows YouTube run were verified.
The GitHub-hosted YouTube attempt was blocked by a bot challenge, so hosted
live transcription is not verified.
This does not imply that all 13 platforms or every harness workflow has been
tested end to end. See [docs/roadmap.md](docs/roadmap.md).

## License

Apache-2.0 — see [LICENSE](LICENSE). Includes an explicit patent grant and a
limitation of liability.

## Contributing

Issues and PRs welcome. Please read [LEGAL.md](LEGAL.md) before adding a source
adapter.

