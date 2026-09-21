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

## Harness transport support

**Evidence tier: `browsed` / live-connection.** Each harness's own MCP client was
pointed at this server and reported a connection, on 2026-09-21. This is stronger
than reading config files, and it is still **not** an end-to-end transcription run
driven by each harness — no harness was asked to complete a real transcription
task through the tools.

| Harness | Version | Transport used | How it was verified |
|---|---|---|---|
| **DSH** | 0.1.1-rc.2 | stdio | Profile composed with an `insert` patch; the DSH node process **spawned our server as a child** (parent/child confirmed from the process table) |
| **Claude Code** | 2.1.269 | stdio | `claude mcp add` + `claude mcp list` → `√ Connected` |
| **Codex CLI** | 0.147.0 | stdio | `codex mcp add` → `enabled: true`; server reachable via Codex's exact configured command; handshake returned 6 tools |
| **OpenCode** | 1.18.18 | Streamable HTTP | `opencode mcp add --url` + `opencode mcp list` → `✓ textflowkit connected`; `opencode mcp debug` → `HTTP response: 200 OK` |

Two transport notes learned from doing this:

- **OpenCode's `mcp add` accepts only `--url`** — no command flag. OpenCode must
  use the HTTP transport (`textflowkit-mcp --transport http`), not stdio.
- **Claude Code's `mcp add` cannot pass a bare `-m module` argument** in this
  version. Use the `textflowkit-mcp` console script instead — that form is
  verified connected above.

The negotiated MCP protocol version from this server is **2025-11-25** — observed
from a real stdio handshake.

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
pip install "textflowkit[mcp]"
textflowkit-mcp                      # stdio (default)
textflowkit-mcp --transport http --host 127.0.0.1 --port 8766
```

Tools: `list_sources`, `transcribe_media`, `get_job_status`, `get_transcript`,
`export_transcript`, `list_jobs`, `cancel_job`.

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
pip install "textflowkit[http]"
textflowkit-http --host 127.0.0.1 --port 8767
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/sources` | platforms, formats |
| POST | `/jobs` | submit a job (202 + job id) |
| GET | `/jobs` | list recent jobs |
| GET | `/jobs/{id}` | job status |
| GET | `/jobs/{id}/transcript?format=` | rendered transcript |
| POST | `/jobs/{id}/export` | write files to disk |
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

## Input paths are confined

Adapters accept a **local file path** from a caller — and there the caller may be
a model acting on untrusted content, not the machine's owner. Local inputs are
confined to an allowed root:

- `TEXTFLOWKIT_INPUT_ROOT` sets it.
- **For the adapters the default is the current working directory**, so an MCP or
  HTTP caller cannot name an arbitrary file on the host.
- Paths outside the root, and `..` escapes, are rejected before the file is read.

```bash
TEXTFLOWKIT_INPUT_ROOT=/srv/media textflowkit-mcp --transport http
TEXTFLOWKIT_INPUT_ROOT=/            # no practical confinement
```

The CLI is deliberately **not** confined: the user typed the path and is the
principal.

## Output paths are confined

Adapters accept a destination directory from a caller — a CLI user, an HTTP
client, or a model. That path is resolved against an **allowed root**:

- `TEXTFLOWKIT_OUTPUT_ROOT` sets the root.
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



