# Adapters and integration

textflowkit has one core and several thin doors. Nothing is duplicated between
them: the CLI, the MCP server, and the HTTP API all call
`textflowkit.core.runner.submit` and share one job model.

```
                    ┌──────────────────────┐
   CLI ────────────►│                      │
                    │   core              │
   MCP  ───────────►│   (pipeline + jobs) │
                    │                      │
   HTTP ───────────►└──────────────────────┘
```

## Resuming and batching

`textflowkit transcribe --resume` reuses completed work, and
`textflowkit batch` runs many sources in one invocation.

Both need a **durable job store**: set `TEXTFLOWKIT_DB` to a SQLite file path.
Without it the store lives in the process, so no checkpoint can outlive the run
and `--resume` cannot find anything to reuse. The CLI says so on stderr rather
than silently re-transcribing - if you see that warning, set `TEXTFLOWKIT_DB`.

Resume also requires the **source to still exist** and the same source, model,
language, and options as the original run. A resumed run re-validates all of
them; a mismatch starts clean rather than mixing two runs into one transcript.

## Harness transport support

**Evidence tier: `browsed` / live-connection.** Each harness's own MCP client was
pointed at this server and reported a connection, on 2026-09-21. This is stronger
than reading config files, and it is still **not** an end-to-end transcription run
driven by each harness — no harness was asked to complete a real transcription
task through the tools.

| Harness | Version | Transport used | How it was verified |
|---|---|---|---|
| **DSH** | 0.1.5-rc.2 | stdio | Profile composed with an `insert` patch and `failOnStartupError: true`; DSH's own MCP client spawned the server as a child, completed the handshake, discovered **all 8 tools**, and a real `list_sources` call returned data |
| **Claude Code** | 2.1.269 | stdio | `claude mcp add` + `claude mcp list` → `√ Connected` |
| **Codex CLI** | 0.147.0 | stdio | Entry present in `~/.codex/config.toml`. **Not live-verified on 2026-09-21**: the CLI aborts on an unrelated malformed model-catalog file, so no handshake was observed this run. The earlier note above reflects a previous run and is not current evidence. |
| **OpenCode** | 1.18.18 | Streamable HTTP | `opencode mcp add --url` + `opencode mcp list` → `✓ textflowkit connected`; `opencode mcp debug` → `HTTP response: 200 OK` |

Two transport notes learned from doing this:

- **OpenCode's `mcp add` accepts only `--url`** — no command flag. OpenCode must
  use the HTTP transport (`textflowkit-mcp --transport http`), not stdio.
- **Claude Code's `mcp add` cannot pass a bare `-m module` argument** in this
  version. Use the `textflowkit-mcp` console script instead — that form is
  verified connected above.

The negotiated MCP protocol version from a real stdio handshake on 2026-09-21 is
**2025-06-18** (asserted by `tests/test_stdio_protocol.py`, which records the
version the server actually reports rather than a remembered value).

### DSH configuration (stdio)

```yaml
- insert:
    - id: mcp-textflowkit
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: textflowkit
        transport: stdio
        command: /path/to/textflowkit-mcp
```

`insert:` is required — a patch entry that is not wrapped in `insert` is treated as
targeting an existing entry and fails with `patch: entry "<id>" not found`.
## MCP (for AI harnesses)

```bash
pip install -e ".[mcp]"
textflowkit-mcp                      # stdio (default)
textflowkit-mcp --transport http --host 127.0.0.1 --port 8766
```

Tools: `list_sources`, `transcribe_media`, `get_job_status`, `get_transcript`,
`export_transcript`, `list_jobs`, `cancel_job`, `search_transcript`.

Read-only tools carry `readOnlyHint: true`; the two that touch the network or
disk carry `openWorldHint: true`.

### Harness configuration

DSH (`cordis.yml`), HTTP:

```yaml
- id: mcp-textflowkit
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: textflowkit
    transport: streamable-http
    url: http://127.0.0.1:8766/mcp
```

DSH, stdio:

```yaml
- id: mcp-textflowkit
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: textflowkit
    transport: stdio
    command: textflowkit-mcp
```

Claude Code:

```bash
claude mcp add textflowkit -- textflowkit-mcp
claude mcp add --transport http textflowkit http://127.0.0.1:8766/mcp
```

OpenCode (`opencode.json`):

```json
{ "mcp": { "textflowkit": { "type": "remote", "url": "http://127.0.0.1:8766/mcp" } } }
```

## HTTP (for software products and web frontends)

```bash
pip install -e ".[http]"
textflowkit-http --host 127.0.0.1 --port 8767
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/sources` | platforms, formats |
| POST | `/jobs` | submit a job (202 + job id) |
| GET | `/jobs` | list recent jobs |
| GET | `/jobs/{id}` | job status |
| GET | `/jobs/{id}/transcript?format=&offset=&limit=&start=&end=` | rendered transcript, optionally sliced |
| GET | `/jobs/{id}/search?q=&limit=&context=` | search a transcript |
| POST | `/jobs/{id}/export?formats=docx&formats=pdf` | write files to disk (docx/pdf included) |
| POST | `/jobs/{id}/cancel` | request cancellation |

### Binding beyond loopback is refused

The HTTP surfaces have no authentication, so binding one to a reachable
interface would expose it. That specific configuration is **refused at startup**:

```bash
textflowkit-http --host 0.0.0.0
# error: refusing to bind to '0.0.0.0': the HTTP surface has no authentication ...
```

Loopback (`127.0.0.1`, `localhost`, `::1`) is always allowed. To bind elsewhere
you must say so explicitly:

```bash
textflowkit-http --host 0.0.0.0 --allow-remote
TEXTFLOWKIT_ALLOW_REMOTE=1 textflowkit-mcp --transport http --host 0.0.0.0
```

Only do that behind your own gateway. The guard deliberately does not add
authentication - it makes the unsafe configuration an explicit decision instead
of a default.

**No authentication is included.** Bind to localhost, or front it with your own
gateway before exposing it. That is deliberate: auth belongs to the deployment,
not to a transcript library.

## Durable job state

By default jobs live in memory and are lost when the process exits. Set
`TEXTFLOWKIT_DB` to a file path and job state becomes durable:

```bash
TEXTFLOWKIT_DB=/var/lib/textflowkit/jobs.db textflowkit-mcp --transport http
```

Backed by SQLite (WAL). Transcripts, outputs, and job states survive a restart.

**Orphaned work is reaped at startup.** A job left in `pending` or `running` by a
previous process has no worker, so it is failed with a reason rather than reported
as a job that will never finish. This assumes **one owning process per store** -
two processes sharing one `TEXTFLOWKIT_DB` would reap each other's live jobs.

## Concurrency

Each submission used to start an unbounded thread. Jobs now run on a fixed worker
pool:

```bash
TEXTFLOWKIT_MAX_CONCURRENCY=1   # default
```

The default is **1** deliberately. Whisper saturates a GPU on its own, so parallel
jobs thrash VRAM rather than finishing sooner. Raise it only for CPU-bound or
I/O-bound workloads where that reasoning does not apply.

## Cancellation

`cancel_job` (MCP) and `POST /jobs/{id}/cancel` (HTTP) stop a job. Cancellation is
**cooperative**, and it behaves differently depending on job state - deliberately,
because these are genuinely different situations:

| Job state | Behaviour |
|---|---|
| `pending` (queued, not started) | Cancelled immediately. The worker skips it; it never runs. |
| `running` | `cancel_requested` is set and the job stops at its **next stage boundary**. Until then it stays `running` with `progress: "cancelling"`. |
| terminal | Refused, with the current state and a reason. |

**The honest limit:** a job inside a single long model call cannot be interrupted
mid-call. Checkpoints sit at stage boundaries - before resolve, after resolve,
after fetch, after extract, after transcribe - so a cancellation during a
20-minute transcription takes effect when that call returns, not instantly. The
API reports `cancelling` rather than claiming an instant stop it cannot deliver.

## Export formats

Two paths, because two kinds of output:

| | Formats | Returned |
|---|---|---|
| **inline** (`get_transcript`, `GET /transcript`) | txt, srt, vtt, md, json | as text in the response |
| **export** (`export_transcript`, `POST /export`) | all of the above **plus docx, pdf** | written to disk |

Binary formats cannot be returned inline, and asking for one that way returns a
clear message rather than failing deeper down.

```bash
pip install -e ".[export]"        # python-docx + reportlab
curl -X POST "http://127.0.0.1:8767/jobs/$ID/export?formats=docx&formats=pdf"
```

```jsonc
// MCP
{"job_id": "...", "output_dir": "./out", "formats": "docx,pdf,srt"}
```

Both renderers emit the **finished** data model: speaker labels and translated
text are included, with the source line kept alongside the translation so a
reader can see both. `formats` on the HTTP export is an explicit query parameter
- a bare `list[str]` on a POST is treated by FastAPI as a request *body* field,
which silently ignored the argument.

## Translation and speaker labels

Both are opt-in stages that run after transcription, both write into fields the
transcript model already had, and both **fail loudly when unavailable** rather
than returning output that quietly lacks the feature.

### Translation

```bash
export TEXTFLOWKIT_TRANSLATE_MODEL=your-local-ollama-model
textflowkit transcribe "$URL" --translate-to Spanish
```

Translation requires an explicit `TEXTFLOWKIT_TRANSLATE_MODEL`; there is **no
default model** and textflowkit will not silently choose a cloud model. The
default Ollama host is `http://127.0.0.1:11434`, but a model tagged `:cloud`
can send transcript text beyond that local host. Likewise, setting
`TEXTFLOWKIT_OLLAMA_HOST` to a remote server sends text to that host. `doctor`
reports the selected model and route, and each translated transcript records
the route in metadata. Choose a local model if transcripts must stay on this
machine.

Requests are **batched** (20 segments per round trip), and if a batch comes back
unparseable the chunk is retried one segment at a time - correctness does not
depend on the model obeying a format. Identical text is cached, which matters
because transcripts repeat phrases.

The result length is checked against the input, so a misbehaving model cannot
shift text onto the wrong segment. An unreachable backend raises; it never
returns the source text as a translation.

`translation` metadata is recorded on the transcript (backend, route, target, how many
segments were translated).

### Speaker labels

```bash
textflowkit transcribe "$URL" --diarize
HF_TOKEN=hf_...                   # required: the model is gated
TEXTFLOWKIT_DIARIZE_DEVICE=cuda  # optional; ROCm also appears as cuda in torch
```

Requires the optional `diarize` extra (`pip install 'textflowkit[diarize]'`) and a
Hugging Face token with access to the pyannote model. Missing either one **fails
the job with an actionable message** - verified over both the CLI and MCP.

Each segment takes the speaker with the greatest time overlap. A segment with no
overlapping turn is left **unlabelled rather than guessed at**, and exact ties go
to the earlier turn so the result is deterministic.

**Honest limit:** the live pyannote path is not covered by CI. No CI runner has
the gated model, so the assignment logic is tested with a stub and the real model
path is unverified.

## Reading a long transcript

Returning a whole transcript is a context problem. A 19-minute video is already
~51 KB of JSON (~214 segments); a multi-hour recording is several hundred KB
dropped into a model's context in one tool result, mostly irrelevant to the
question being asked.

`get_transcript` accepts a slice, and `search_transcript` finds a phrase:

| Parameter | Meaning |
|---|---|
| `offset` | skip this many segments **within the selected range** |
| `limit` | return at most this many |
| `start` / `end` | restrict by time in seconds (inclusive) |

Filtering is time first, then offset/limit inside that window - `offset` counts
from the start of the requested range, not the start of the transcript. The
response reports `total_segments`, `returned`, and `has_more`, and when more
remain it includes a `next` hint naming the offset to continue from. Truncation
is never silent.

```jsonc
// MCP
{"job_id": "...", "fmt": "json", "offset": 20, "limit": 20}
{"job_id": "...", "query": "neural network", "limit": 5, "context": 1}
```

```bash
# HTTP
curl "http://127.0.0.1:8767/jobs/$ID/transcript?format=srt&start=300&end=320"
curl "http://127.0.0.1:8767/jobs/$ID/search?q=neural%20network&context=1"
```

`search_transcript` is substring matching (not fuzzy), case-insensitive by
default, and matches translated text as well as source text when a translation
is present. `context` includes neighbouring segments, which is usually what makes
a hit readable.

Verified against a real 214-segment transcript: paging returned 20 with
`has_more`, a 300-320s window returned 6 segments, and searching "neural network"
found 3 matches with timestamps.

## Input paths (unconfined by default)

Adapters accept a **local file path** from a caller — and there the caller may be
a model acting on untrusted content, not the machine's owner. Local inputs are
confined to an allowed root:

- Nothing is confined unless you set `TEXTFLOWKIT_INPUT_ROOT`. By default an
  adapter has the same access to the machine as the person who started it,
  which is the point: an agent running on your behalf can reach your files.
- **For the adapters the default is the current working directory**, so an MCP or
  HTTP caller cannot name an arbitrary file on the host.
- Paths outside the root, and `..` escapes, are rejected before the file is read.

```bash
TEXTFLOWKIT_INPUT_ROOT=/srv/media textflowkit-mcp --transport http
TEXTFLOWKIT_INPUT_ROOT=/            # no practical confinement
```

Neither the CLI nor the adapters confine by default: the caller is the
principal.

## Output paths

Adapters accept a destination directory from a caller — a CLI user, an HTTP
client, or a model. That path is resolved against an **allowed root**:

- Nothing is confined unless you set `TEXTFLOWKIT_OUTPUT_ROOT`. The default is
  the current working directory, but a caller may still ask for any path the
  operator could write; only an explicit root imposes a boundary.
- When unset, the root is the current working directory. The CLI's default of
  writing into the directory you ran it from therefore still works.
- `..` segments, absolute paths outside the root, and symlinks that escape are
  rejected with an actionable error (`422` on HTTP; an `error` field on MCP).

```bash
# confine every write from a server to one directory
TEXTFLOWKIT_OUTPUT_ROOT=/srv/transcripts textflowkit-mcp --transport http
```

## Why jobs, not blocking calls

A 19-minute video would otherwise hold a request open for minutes. Returning a
job id immediately means:

- stdio MCP polls in-process
- HTTP gets async for free
- a future website can queue work without interface changes
- cancellation and retries have somewhere to live

The job store is in-memory and bounded (`max_jobs=200`, terminal jobs evicted
first). Swap `JobStore` for a durable backend without touching callers.



