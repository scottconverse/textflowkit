# textflowkit

Cross-platform media transcription toolkit. **One core, one CLI, thin adapters.**

[Project landing page](https://www.textflowkit.org/) ·
[PyPI package](https://pypi.org/project/textflowkit/) ·
[GitHub releases](https://github.com/scottconverse/textflowkit/releases) ·
[User manual](https://github.com/scottconverse/textflowkit/blob/main/docs/user-manual.md) ·
[Developer and integration manual](https://github.com/scottconverse/textflowkit/blob/main/docs/adapters.md)

**Current release: [v0.1.9](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.9).**

The [static site deployment](https://github.com/scottconverse/textflowkit/blob/main/docs/site-deployment.md) is hosted on Cloudflare
Pages. GitHub remains the source and CI host; the website does not run the
transcription engine.

Paste a URL or point at a file; get timestamped transcripts and subtitle files back.
Built as a reusable primitive for developers — designed to sit under multiple
products, AI harnesses, and agents.

---

## Quickstart

Requires **Python ≥ 3.10** and **ffmpeg** on `PATH`.

Windows (PowerShell or CMD):

```powershell
python -m pip install textflowkit
textflowkit doctor
textflowkit transcribe .\meeting.mp4 --formats srt,txt --output-dir .\out
```

macOS or Linux:

```bash
python -m pip install textflowkit
textflowkit doctor
textflowkit transcribe ./meeting.mp4 --formats srt,txt --output-dir ./out
```

`doctor` prints the Python, ffmpeg, yt-dlp, JavaScript-runtime, and
optional-extra versions this install will use. The `transcribe` example writes
the SRT and TXT files into `out\` and prints the path of each one; give it a
supported URL instead of a path to fetch remote media. See **Install** below for
the extras, and **Usage** for language, translation, speaker labels, and resume.

---

## What it does

```
URL or file  ─►  detect platform  ─►  acquire media  ─►  ffmpeg
                                                          │
                              ┌───────────────────────────┘
                              ▼
              speech-to-text (Whistle default; Whisper on request)
                              │
                              ▼
                 canonical transcript (JSON)
                              │
        ┌─────────┬───────────┼───────────┬──────────┐
        ▼         ▼           ▼           ▼          ▼
       TXT       SRT         VTT        JSON     Markdown
```

DOCX and PDF are available through the optional `export` extra.

## Architecture

![TextFlowKit shared-core architecture: four thin entry points (CLI, Python, MCP, HTTP) feed one core that acquires and decodes media, transcribes with the default Whistle engine or an explicitly selected openai-whisper, keeps job state and resume checkpoints in an optional SQLite store, adds optional speaker or translation postprocessing, and publishes TXT, SRT, VTT, JSON, Markdown, and optional DOCX/PDF exports.](https://raw.githubusercontent.com/scottconverse/textflowkit/v0.1.9/docs/assets/architecture-overview.svg)

The design principle is **one engine, three doors**. Everything of substance lives in
the core; the interfaces are thin.

| Layer | Path | Responsibility |
|---|---|---|
| **Core** | `src/textflowkit/core` | Canonical transcript model, pipeline orchestration |
| **Sources** | `src/textflowkit/sources` | Per-platform URL normalization + media acquisition |
| **Renderers** | `src/textflowkit/render` | TXT / SRT / VTT / JSON / Markdown / DOCX / PDF output |
| **CLI** | `src/textflowkit/cli.py` | Reference interface (subprocess-friendly) |
| **MCP** | `src/textflowkit/adapters/mcp_server.py` | stdio + Streamable HTTP, for AI harnesses |
| **HTTP** | `src/textflowkit/adapters/http_server.py` | JSON API, for software products and web frontends |

Because the core owns the pipeline, adding a door is cheap — and adding a platform
means writing one source adapter, not another tool.

For the job lifecycle, process ownership, checkpoints, and output publication,
see [docs/architecture.md](https://github.com/scottconverse/textflowkit/blob/main/docs/architecture.md).

## Recognized sources

YouTube · TikTok · Facebook · Instagram · Vimeo · Twitch · Bilibili · Rumble ·
Kick · Zoom · Medal · Loom · Dropbox — plus **direct media URLs and local files**.

All 13 are recognised through `yt-dlp`; only YouTube has an opt-in, maintained
[live end-to-end smoke](https://github.com/scottconverse/textflowkit/blob/main/docs/release-checklist.md). It is run on a Windows
maintainer machine before a release, not on GitHub-hosted runners or every pull
request. Local files have also been transcribed live. The other
12 are not release-verified end to end, and some sources require cookies or
change their access rules frequently. See [docs/sources.md](https://github.com/scottconverse/textflowkit/blob/main/docs/sources.md).

## Install

Requires **Python ≥ 3.10** and **ffmpeg** on `PATH`.

```bash
python -m pip install textflowkit
textflowkit doctor
```

The default engine is **Whistle**: a native CPU-only transcription CLI that
needs no PyTorch. It downloads one pinned model (about 17 MB) and a small pinned
binary on first use; a fresh default install therefore stays small and pulls no
torch. To use the `openai-whisper` engine instead — the ROCm/CUDA/CPU torch
stack — install the `whisper` extra and name it explicitly:

```bash
python -m pip install 'textflowkit[whisper]'
textflowkit transcribe meeting.mp4 --engine whisper --model small
```

See [Whistle (default engine)](#whistle-default-engine) for platform and
language coverage, and the [install guide](https://github.com/scottconverse/textflowkit/blob/main/docs/install.md)
for the AMD ROCm path, which applies to the explicit `whisper` engine and
diarization.

For MCP or the JSON HTTP adapter, install the matching extra:

```bash
python -m pip install 'textflowkit[mcp]'    # MCP server
python -m pip install 'textflowkit[http]'   # JSON HTTP API
```

PDF/DOCX export is optional. The default wheel stays small; the `export` extra
installs `textflowkit-fonts` for offline multilingual PDF rendering:

```bash
python -m pip install 'textflowkit[export]'
```

## Python API

The same pipeline used by the CLI and adapters is available to Python callers:

```python
from textflowkit import transcribe

result = transcribe(
    "meeting.mp4",            # also accepts supported URLs
    formats=["json", "srt", "txt"],
    output_dir="transcripts", # omit to return the transcript without writing files
)
print(result.transcript.text)
print(result.transcript.duration)  # full media duration in seconds
print(result.outputs)              # pathlib.Path objects for written files
for segment in result.transcript.segments:
    print(segment.start, segment.end, segment.speaker, segment.text)
    for word in segment.words:
        print("  ", word.start, word.end, word.text)
```

`engine` defaults to the product default (Whistle) and `model=None` resolves to
that engine's own model. To use the Whisper-family engines, name one and its
model explicitly: `transcribe("meeting.mp4", engine="whisper", model="small")`.

`transcribe()` returns `TranscribeResult` with a canonical `Transcript` and
written output paths. `Transcript.to_dict()` / `.to_json()` preserve segment and
word timing; older transcript JSON without `words` remains readable. Pass
`input_root=` to confine local input paths for untrusted callers. See
[the install guide](https://github.com/scottconverse/textflowkit/blob/main/docs/install.md)
for ffmpeg and Windows ROCm setup.

MCP and HTTP transcript reads omit word timings by default to keep responses
small; set `include_words=true` on a JSON read to receive them. Saved files,
Python results, and durable job records still retain the source-language words,
including when segment text has been translated.

**AMD ROCm on native Windows:** do not use the generic command in an environment
with a working ROCm PyTorch install. Ordinary dependency resolution can replace
that torch build. Follow the [ROCm install notes](https://github.com/scottconverse/textflowkit/blob/main/docs/install.md)
to preserve it.

The same version's wheel and source archive are also on the
[GitHub release page](https://github.com/scottconverse/textflowkit/releases/latest).
Starting with v0.1.6, every release published by this project's release
workflow carries a `SHA256SUMS` asset listing the SHA-256 hash of each wheel and
source archive it contains, so you can check a download with
`sha256sum -c SHA256SUMS`. Releases published before that change, v0.1.5
included, have no such asset. For editable source development, see
[CONTRIBUTING.md](https://github.com/scottconverse/textflowkit/blob/main/CONTRIBUTING.md).

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

## Whistle (default engine)

**Whistle is the default engine** as of v0.1.9. It is a native CPU-only CLI that
needs no PyTorch, so a fresh `pip install textflowkit` stays small and pulls no
torch; `openai-whisper` is an explicitly selectable opt-in engine.

![Whistle bounded block run and durable resume: a decoded PCM WAV is split into 26-second cores with up to 2 seconds of context (each clip ≤ 30 s), each core runs as one owned child with forced telemetry-off flags, a partial checkpoint is written after each core, and a resume validates the source and decoded-WAV hashes then re-runs only the unfinished cores.](https://raw.githubusercontent.com/scottconverse/textflowkit/v0.1.9/docs/assets/whistle-resume.svg)

- **CPU only, no torch.** It runs a pinned native binary and downloads one pinned
  model on first use. Naming a GPU device is refused rather than silently
  ignored: a GPU request names the explicit `whisper` engine as the alternative,
  and the engine is never switched for you.
- **Seven advertised languages:** `en`, `de`, `fr`, `es`, `it`, `nl`, `pl`. A
  request for another language is refused at request construction, before any
  media is fetched.
- **Native platforms:** Windows x86-64 and arm64, Linux x86-64 and arm64, and
  Apple Silicon. **Intel Macs are not supported**; a request there is refused
  with a message that names the explicit `whisper` engine instead. No WSL layer
  is involved.
- **How it works:** long audio is split into 26-second cores with up to 2 seconds
  of context on each side, so every standalone clip is ≤ 30 seconds (the native
  CLI's limit). Overlap words are selected by core midpoint rather than text
  deduplication, so repeated spoken phrases are preserved.
- **Streaming is not used.** The native `--audio-stream` mode failed its
  coverage and is never passed; only the committed standalone output is parsed.
- **Durable block resume.** With `TEXTFLOWKIT_DB` set, a long run writes partial
  per-block checkpoints; a resume checks the source and decoded-WAV hashes and the
  config, then re-runs only the blocks that were not finished. The durable store
  still assumes one owning process.
- **Cancellation.** A running Whistle run terminates its exact owned child
  process bounded by a per-clip timeout. A single long `openai-whisper` model call
  is still only cancellable at its next stage boundary, as documented for the
  Whisper engine.
- **Telemetry is off, unconditionally.** Every Whistle child process forces
  `NEEDLE_TELEMETRY=0`, `DO_NOT_TRACK=1`, and `CI=1`, overriding a parent that
  opted in. The product ships no analytics, usage SDK, anonymous ids, or events,
  and there is no opt-in setting. This applies the upstream documented gate; it
  is **not** a claim that the upstream binary's tracking code is physically
  removed.
- **Offline and downloads.** Set `TEXTFLOWKIT_OFFLINE` to refuse any download, and
  `TEXTFLOWKIT_MODELS_DIR` to choose the asset directory. The Whistle asset helper
  downloads only pinned runtime/model assets; media acquisition and optional
  translation may independently use the network. These downloads are not
  telemetry.

**Verification boundary.** A local run transcribed a 4-hour (14,407 s)
recording on CPU into 555 clips / 34,596 words / 2,875 segments in 731 s, with
telemetry forced off and offline mode on; a separate run proved durable block
resume across two processes. These are **first local test numbers**, not a
promise of universal performance or of accuracy equal to Whisper — no
human-scored word-error rate is claimed. See the
[user manual](https://github.com/scottconverse/textflowkit/blob/main/docs/user-manual.md)
and [install guide](https://github.com/scottconverse/textflowkit/blob/main/docs/install.md).

## Use as an MCP server

```bash
python -m pip install 'textflowkit[mcp]'
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
See [docs/adapters.md](https://github.com/scottconverse/textflowkit/blob/main/docs/adapters.md) for per-harness configuration.

## Use as an HTTP API

```bash
python -m pip install 'textflowkit[http]'
textflowkit-http --port 8767
```

Submit a job, poll it, fetch the transcript. Developer mode is unauthenticated
and defaults to localhost. The opt-in JSON HTTP production profile requires a
Bearer token, explicit roots, durable SQLite jobs, and request/rate/media/output
limits; URL input additionally requires an SSRF-filtering egress proxy. See
[adapter deployment details](https://github.com/scottconverse/textflowkit/blob/main/docs/adapters.md#developer-mode-and-production-profile).

A `Dockerfile` and a `compose.yaml` for that profile sit at the repository root:
ffmpeg and a JavaScript runtime in the image, a nonroot runtime user, the host
port published to loopback only, and a token the operator supplies (there is no
default). This is a Linux deployment **example**, not a published service - it
ships no TLS gateway and no egress proxy, so URL jobs fail closed until you
provide one. The image has not been built or started anywhere: only static
contract checks cover it, and running an actual build is deliberately out of
scope here, so no image is claimed to build or start. See
[the container example](https://github.com/scottconverse/textflowkit/blob/main/docs/adapters.md#container-example-dockerfile-and-compose).

## Durable, bounded, cancellable

```bash
TEXTFLOWKIT_DB=./jobs.db            # job state survives restart (SQLite)
TEXTFLOWKIT_MAX_CONCURRENCY=1        # default; bounded job worker pool
```

`cancel_job` stops a queued job immediately, or a running job at its next stage
boundary. See [docs/adapters.md](https://github.com/scottconverse/textflowkit/blob/main/docs/adapters.md).

## Long jobs never block

The MCP and HTTP adapters are **job-based**: submission returns a job id
immediately and clients poll for completion. The CLI uses the same core but
waits for the result. This lets AI harnesses, software products, and a future
web frontend share the job contract without blocking a request.

## Status

**v0.1.9 release.** Core, CLI, MCP, and HTTP have automated
coverage. This release makes **Whistle the default engine** — a native CPU-only
transcriber that needs no PyTorch — and moves `openai-whisper` to the optional
`whisper` extra, selected explicitly with `--engine whisper` or
`engine="whisper"`. Legacy engine aliases are preserved, so older saved jobs and
`transcribe()` calls still decode. Whistle refuses an unsupported platform,
language, or GPU request instead of silently falling back. Its engine's
verification boundary is in [Whistle (default engine)](#whistle-default-engine)
above. Release publication uses the tag workflow, which requires successful
exact-commit main CI before PyPI uploads and creates the public GitHub release
only afterward. Check the linked release for artifacts and workflow status; local
source verification is not a fresh PyPI-install or individual-harness receipt.

The v0.1.8 release carried four fixes for the 2026-10-01 post-release audit-lite
(findings AL-001 – AL-004): CLI owning-process startup recovery so an interrupted
run's saved work can be resumed instead of being refused as "already active",
preservation of the completed transcript across resume setup failures, a
correction to the MCP batch cookie capability, and corrected roadmap evidence
receipts. It built on the v0.1.7 audit repair set and the v0.1.6 review repairs
that precede it. The v0.1.7 source candidate was verified with real CLI, HTTP and
MCP speech, seven-format exports and completed resume; the v0.1.8 runtime fixes
were independently checked through fresh-process CLI restart probes, real SQLite
setup-failure tests, and the full Windows test suite. Those checks are not new
individual-harness receipts. That is the previous release's record, not evidence
for v0.1.9.

The v0.1.6 release was the post-v0.1.5 review repair set: security hardening
for media acquisition and the HTTP and MCP adapters, safer subtitle wrapping and
output-file publication, job and checkpoint storage corrections, an opt-in
faster-whisper engine, a container example, and fail-closed release guards for
tagged versions, README claims, and exact-commit CI.
The v0.1.5 release added a speech-bearing self-test, full-media duration,
retained word timings, optional PDF fonts, and tokenless PyPI publishing.
Windows-native ROCm and a local Windows YouTube run on the v0.1.5 release commit
were verified. That run's receipt records the clip, timestamps, and hashes; the
checklist's recognized-speech assertion was added after it, so treat it as shape
evidence for that commit rather than proof of what was said.
The v0.1.6 release is public: the
[v0.1.6 GitHub release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.6)
and both [core](https://pypi.org/project/textflowkit/0.1.6/) and
[fonts](https://pypi.org/project/textflowkit-fonts/0.1.6/) PyPI projects are
live — that is the v0.1.6 release's evidence, not a receipt for a later release.
Everything from here to the end of this section is the v0.1.6 release's
historical record as published: it is not re-verified for v0.1.9, whose separate release evidence is not supplied by these historical paragraphs.
Merged-main CI passed 16/16 on the tagged commit; the published wheel and
sdist digests match the GitHub release assets and their SHA-256 list; and a fresh
Windows Python 3.12 install of `textflowkit[export,mcp,http]==0.1.6` from PyPI
passed `doctor`, `selftest`, and a tiny CPU transcription to JSON and PDF. A
local native-Windows YouTube run on the same commit returned 3 timestamped
segments and a whole-word `elephants` match; its receipt is stored outside this
repository. One clip is not proof of the other 12 recognized platforms or of
general accuracy.
The GitHub-hosted YouTube attempt was blocked by a bot challenge, so hosted
live transcription is not verified.
This does not imply that all 13 platforms or every harness workflow has been
tested end to end. See the [user manual](https://github.com/scottconverse/textflowkit/blob/main/docs/user-manual.md)
and [roadmap](https://github.com/scottconverse/textflowkit/blob/main/docs/roadmap.md).

---

> ## ⚠️ NO WARRANTY — AS IS
>
> **This software is provided "AS IS", WITHOUT WARRANTY OF ANY KIND**, express or
> implied, including but not limited to the warranties of MERCHANTABILITY, FITNESS
> FOR A PARTICULAR PURPOSE, and NONINFRINGEMENT. See [LICENSE](https://github.com/scottconverse/textflowkit/blob/main/LICENSE) (Apache-2.0,
> §7–8) for the full disclaimer and limitation of liability.
>
> **You are responsible for what you transcribe.** textflowkit can fetch media from
> third-party platforms. Copyright, terms-of-service, and privacy obligations for any
> media you choose to process are **yours alone**. See [LEGAL.md](https://github.com/scottconverse/textflowkit/blob/main/LEGAL.md).

## License

Apache-2.0 — see [LICENSE](https://github.com/scottconverse/textflowkit/blob/main/LICENSE). Includes an explicit patent grant and a
limitation of liability.

## Contributing

Issues and PRs welcome. Please read [LEGAL.md](https://github.com/scottconverse/textflowkit/blob/main/LEGAL.md) before adding a source
adapter.
