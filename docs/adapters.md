# Adapters and integration

textflowkit has one core and several thin doors. Nothing is duplicated between
them: the CLI, the MCP server, and the HTTP API all call
`textflowkit.core.submission` and share one job model.

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

Resuming across process restarts needs a **durable job store**: set
`TEXTFLOWKIT_DB` to a SQLite file path. Batch can run ephemerally, but
`batch --resume` cannot recover past work without it.
Without it the store lives in the process, so no checkpoint can outlive the run
and `--resume` cannot find anything to reuse. The CLI says so on stderr rather
than silently re-transcribing - if you see that warning, set `TEXTFLOWKIT_DB`.

For local files, resume requires the source to still exist and match the saved
path, size, and SHA-256 digest as well as model/language/options. A changed or
missing local file produces an error; resubmit without resume to transcribe the
new content. Legacy local checkpoints without a digest are rejected for reuse.
URL resume reuses the saved transcript for the same URL/options but makes no
claim that the remote media bytes have remained unchanged.

## Harness transport support

**Evidence tier: historical live-connection.** DSH, Claude Code, and OpenCode's
own MCP clients connected on 2026-09-21; Codex desktop later made a successful
`list_jobs` call after its unrelated model-catalog repair. These checks are
stronger than reading config files, but are **not** end-to-end transcription
runs driven by each harness. Versions and behavior can change.

| Harness | Version | Transport used | How it was verified |
|---|---|---|---|
| **DSH** | 0.1.5-rc.2 | stdio | Profile composed with an `insert` patch and `failOnStartupError: true`; DSH's own MCP client spawned the server as a child, completed the handshake, discovered all **then-current 8 tools**, and a real `list_sources` call returned data. Resume/batch tools were added later and are not covered by this historical harness check. |
| **Claude Code** | 2.1.269 | stdio | `claude mcp add` + `claude mcp list` → `√ Connected` |
| **Codex desktop** | later check | stdio | After an unrelated model-catalog compatibility repair, a live textflowkit `list_jobs` call succeeded. The 2026-09-21 Codex CLI 0.147.0 attempt had failed before connection; that older failure is not a current textflowkit result. |
| **OpenCode** | 1.18.18 | Streamable HTTP | `opencode mcp add --url` + `opencode mcp list` → `✓ textflowkit connected`; `opencode mcp debug` → `HTTP response: 200 OK` |

Two transport notes learned from doing this:

- **OpenCode's tested `mcp add` path accepts only `--url`** — no command flag.
  The verified connection therefore used HTTP (`textflowkit-mcp --transport
  http`); this is not a claim that every OpenCode configuration forbids stdio.
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
python -m pip install 'textflowkit[mcp]'
textflowkit-mcp                      # stdio (default)
textflowkit-mcp --transport http --host 127.0.0.1 --port 8766
```

Tools: `list_sources`, `transcribe_media`, `submit_batch_media`, `resume_job`,
`get_job_status`, `get_transcript`, `export_transcript`, `list_jobs`,
`cancel_job`, `search_transcript`.

Read-only tools carry `readOnlyHint: true`. Submission and export tools carry
`openWorldHint: true`; resume and cancellation are marked as mutating.

### Harness configuration

DSH (`cordis.yml` patch), HTTP example (**not live-connection verified**):

```yaml
- insert:
    - id: mcp-textflowkit
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: textflowkit
        transport: streamable-http
        url: http://127.0.0.1:8766/mcp
```

For verified DSH stdio configuration, use the `insert:` patch above and point
`command` at the installed `textflowkit-mcp` executable.

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
python -m pip install 'textflowkit[http]'
textflowkit-http --host 127.0.0.1 --port 8767
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/sources` | platforms, formats |
| POST | `/jobs` | submit a job (202 + job id) |
| POST | `/jobs/batch` | submit independent jobs (`{"jobs":[...],"resume":true}`) |
| POST | `/jobs/{id}/resume` | resume a saved durable request/checkpoint |
| GET | `/jobs` | list recent jobs |
| GET | `/jobs/{id}` | job status |
| GET | `/jobs/{id}/transcript?format=&offset=&limit=&start=&end=&include_words=` | rendered transcript, optionally sliced; word timings are opt-in for JSON |
| GET | `/jobs/{id}/search?q=&limit=&context=` | search a transcript |
| POST | `/jobs/{id}/export?formats=docx&formats=pdf` | write files to disk (docx/pdf included) |
| POST | `/jobs/{id}/cancel` | request cancellation |

### Developer mode and production profile

Developer mode is localhost-only by default and has no authentication. Binding
to a reachable interface is **refused at startup** unless explicitly enabled:

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

Do not expose developer mode to untrusted callers. For the JSON HTTP adapter,
`TEXTFLOWKIT_PROFILE=production` fails closed unless a Bearer API token, explicit
input/output/work roots, and an on-disk SQLite job store are configured. It
enforces a bounded request body, per-process rate limit, pending queue, media
size, source duration, rendered-output size, and transcript page size. Known
oversize downloads are refused before transfer; download progress is capped
during transfer. A bounded ffprobe rejects known overlong sources before full
decode; ffmpeg decode has a wall-clock timeout, cancellation, and decoded-byte
and duration caps even when metadata is missing. Configure
`TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS` (default 600) if the default is too short
for your host. Each job
gets its own scratch directory under `TEXTFLOWKIT_WORK_ROOT`.

```bash
export TEXTFLOWKIT_PROFILE=production
export TEXTFLOWKIT_API_TOKEN='replace-with-a-long-random-secret'
export TEXTFLOWKIT_INPUT_ROOT=/srv/textflowkit/input
export TEXTFLOWKIT_OUTPUT_ROOT=/srv/textflowkit/output
export TEXTFLOWKIT_WORK_ROOT=/srv/textflowkit/work
export TEXTFLOWKIT_DB=/srv/textflowkit/jobs.db
export TEXTFLOWKIT_EGRESS_PROXY=http://127.0.0.1:8888 # SSRF-filtering proxy, required for URL input
textflowkit-http --host 127.0.0.1 --port 8767
```

Send `Authorization: Bearer <token>` on every request. For remote clients,
terminate TLS and enforce independent rate/egress policy at a trusted gateway;
the built-in limiter is per process, not a distributed quota. A proxy is not
bundled: production URL jobs fail if `TEXTFLOWKIT_EGRESS_PROXY` is unset, and
the operator must ensure that proxy blocks private/loopback destinations and
DNS rebinding. External JavaScript runtimes are disabled for production URL
jobs because they are not guaranteed to honor yt-dlp's proxy; this may limit
some YouTube formats. Local-file jobs do not require network egress. Streamable-HTTP
MCP is a separate surface and should remain on loopback or behind a gateway;
the JSON HTTP production token does not automatically secure it.

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
| `running` | `cancel_requested` is set. Download progress and ffmpeg decode check it during work; model inference and postprocessors stop at their **next stage boundary**. Until then it stays `running` with `progress: "cancelling"`. |
| terminal | Refused, with the current state and a reason. |

**The honest limit:** a job inside a single long model call cannot be interrupted
mid-call. Download hooks and the ffmpeg process respond during acquisition and
decode, but cancellation during a 20-minute Whisper call takes effect when that
call returns. The API reports `cancelling` rather than claiming an instant stop
it cannot deliver.

## Export formats

Two paths, because two kinds of output:

| | Formats | Returned |
|---|---|---|
| **inline** (`get_transcript`, `GET /transcript`) | txt, srt, vtt, md, json | as text in the response |
| **export** (`export_transcript`, `POST /export`) | all of the above **plus docx, pdf** | written to disk |

Binary formats cannot be returned inline, and asking for one that way returns a
clear message rather than failing deeper down.

```bash
python -m pip install 'textflowkit[export]'  # python-docx + reportlab
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
the gated model, so assignment logic is tested with a stub. A Windows ROCm
development-machine run was verified separately; it does not establish that
every user's gated-model access or GPU setup will work.

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
| `include_words` | include source-language word timings in JSON; default `false` on MCP and HTTP reads |

Saved JSON, SQLite job records, and resume checkpoints retain word timings even
when `include_words=false`; the option reduces response size, not storage size.
In one short reviewer sample, transcript JSON grew from 508 to 1,641 bytes
(roughly threefold); the multiplier varies with segment and word counts.
If the transcript was translated, the optional word timings still refer to the
**original spoken language**, not the translated segment text. The Python API
also retains the original word timings.

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
a model acting on untrusted content, not the machine's owner. By default, local
inputs are **not confined**: the process may read any file its Windows or POSIX
account can read. Set an input root when callers are less trusted than that
account:

- Nothing is confined unless you set `TEXTFLOWKIT_INPUT_ROOT`. By default an
  adapter has the same access to the machine as the person who started it,
  which is the point: an agent running on your behalf can reach your files.
- Once `TEXTFLOWKIT_INPUT_ROOT` is set, paths outside that root, including
  `..` and symlink escapes, are rejected before the file is read.

```bash
TEXTFLOWKIT_INPUT_ROOT=/srv/media textflowkit-mcp --transport http
TEXTFLOWKIT_INPUT_ROOT=/            # no practical confinement
```

Neither the CLI nor the adapters confine by default: the caller is the
principal.

## Output paths

Adapters accept a destination directory from a caller — a CLI user, an HTTP
client, or a model. By default, an explicit destination may be anywhere the
process account can write. An **allowed root** applies only when configured:

- Nothing is confined unless you set `TEXTFLOWKIT_OUTPUT_ROOT`. The default is
  the current working directory, but a caller may still ask for any path the
  operator could write; only an explicit root imposes a boundary.
- When unset, the current working directory is the *default destination*, not a
  boundary. The CLI's default of writing where it was run still works, but an
  explicit absolute path elsewhere is allowed.
- When `TEXTFLOWKIT_OUTPUT_ROOT` is set, `..` segments, absolute paths outside
  the root, and symlinks that escape are rejected with an actionable error
  (`422` on HTTP; an `error` field on MCP).

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

By default the job store is in-memory and bounded (`max_jobs=200`, terminal
jobs evicted first). Set `TEXTFLOWKIT_DB` to use the durable SQLite store for
restart-safe jobs and checkpoints. The SQLite store assumes one owning process.
