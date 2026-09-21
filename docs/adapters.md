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

**Evidence tier: `static`.** The table below was established by reading each
harness's MCP configuration and, for DSH, its published plugin README. **No live
MCP call was made from any of these harnesses.** Config-compatible is not the same
as integration-tested; the server itself was verified over stdio with a real MCP
client (`tests-run`), but that client was not one of these four.

| Harness | stdio | Streamable HTTP | Notes |
|---|---|---|---|
| **DSH** (`@deepseek-ai/dsh-mcp-client` 0.1.1-rc.2) | ✅ | ✅ | `transport: stdio \| streamable-http`. Vendors MCP SDK ^1.12 → locked 1.29.0. Tools become `mcp__<serverName>__<rawName>`. |
| **Claude Code** (2.1.269) | ✅ | ✅ | `claude mcp add --transport http ...` or stdio; add-json accepts stdio/SSE/HTTP. |
| **Codex CLI** (0.147.0) | ✅ | — | Manage with `codex mcp add`. |
| **OpenCode** (1.18.18) | ✅ | ✅ | Config `{"type":"remote","url":...}` for HTTP, or local for stdio. |

The negotiated MCP protocol version from this server is **2025-11-25** — observed
from a real stdio handshake, not from a harness.

Because the tool surface is identical across transports, pick transport by
deployment shape, not by harness.

## MCP (for AI harnesses)

```bash
pip install "textflowkit[mcp]"
textflowkit-mcp                      # stdio (default)
textflowkit-mcp --transport http --host 127.0.0.1 --port 8766
```

Tools: `list_sources`, `transcribe_media`, `get_job_status`, `get_transcript`,
`export_transcript`, `list_jobs`.

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

**No authentication is included.** Bind to localhost, or front it with your own
gateway before exposing it. That is deliberate: auth belongs to the deployment,
not to a transcript library.

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


