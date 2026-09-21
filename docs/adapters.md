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



