# Live microphone streaming — v0.1.11

TextFlowKit can transcribe a **live microphone** as you speak: audio is captured in
the browser, resampled to 16 kHz mono, and streamed to a local WebSocket, and the
transcript comes back a few words at a time. This page is the canonical reference
for that capability — the wire contract, the two ways to turn it on, the exact
commands, and the limits.

The capability is **opt-in, loopback-only, and verified on this machine's native
engine for bounded live runs** — 65 s and 300 s end-to-end native streams ran
locally on Windows x86_64 (see *Limits and known constraints*). Native live support is **Windows x86_64 only**; other live platforms are
unsupported. This v0.1.11 release surface is gated behind an environment switch
so a default install never serves it. The public website is static documentation,
not a streaming service. Publication and remote CI are separate release gates.

> **This is not the standalone `textflowkit` CLI.** The command-line transcriber
> continues to work on files and URLs and does **not** open a microphone or a
> streaming socket — its native audio-stream path stays disabled. Live streaming is
> a separate, opt-in capability of the two *servers* (the developer HTTP API and the
> local browser UI) that ships in the same wheel.

![Optional Windows x86_64 streaming: bounded PCM passes through an authenticated loopback WebSocket to an ephemeral StreamingSession and one owned native child; incremental events return, finish flushes to a canonical Transcript, and cancel releases the session. No durable job or resume; off by default; physical microphone capture not verified; the public site serves no streams.](assets/live-streaming-flow.svg)

This separate live path bypasses durable job submission; the file/URL job
diagrams describe the existing standalone workflow.

## What it is

- A **WebSocket endpoint** (`/stream` on the developer HTTP API, `/api/stream` on
  the local UI) that accepts PCM audio frames and returns transcript events.
- A **runnable browser example** (`GET /streaming-example`, or
  `/api/streaming-example` under the UI) — a self-hosted page that captures the
  microphone with an `AudioWorklet`, resamples in the worklet, and shows the
  transcript. No CDN, no analytics, no external fetch.
- A **transport-neutral core** (`textflowkit.core.streaming`): the same
  `StreamingSession` object could be driven by any transport. The WebSocket adapter
  is a thin layer over it.

## Turning it on

Streaming is off unless `TEXTFLOWKIT_STREAMING=1` is set in the server process. When
off, the endpoint does not exist — a connection is refused before any session or
audio buffer is allocated.

### Option A — the local browser UI (point and click)

```bat
python -m pip install "textflowkit[streaming]==0.1.11"
set TEXTFLOWKIT_STREAMING=1
textflowkit-ui
```

Then open the example the launcher prints, `http://127.0.0.1:<port>/api/streaming-example`,
and press **Start microphone**. The page already holds this UI process's
capability. In the **production profile** the stream additionally requires the API
token, so the page then shows a **token field** — paste `TEXTFLOWKIT_API_TOKEN` into
it and press Start. The field is hidden in the default (developer) profile, where the
capability alone is enough. The token is typed by hand into the field and sent in the
first WebSocket message; it is never written into the page, its JavaScript, or a log.

### Option B — the developer HTTP API (programmatic clients)

```bat
python -m pip install "textflowkit[streaming]==0.1.11"
set TEXTFLOWKIT_STREAMING=1
set TEXTFLOWKIT_API_TOKEN=choose-a-long-random-token
textflowkit-http --streaming
```

The stream is at `ws://127.0.0.1:8767/stream`. Serving the documented import target
directly works too — the route installs at import time when the switch is set — but
you **must** pass the WebSocket limits yourself. The custom launcher
(`textflowkit-http`) applies them; a direct ASGI invocation does not, so state them
explicitly or the protocol's bounds are not in force:

```bat
uvicorn textflowkit.adapters.http_server:app --host 127.0.0.1 --port 8767 ^
  --ws-max-size 32768 --ws-max-queue 5 --ws-per-message-deflate false --no-proxy-headers
```

The launcher sets exactly these bounds (`ws_max_size <= 32768`, `ws_max_queue <= 5`,
permessage-deflate off). They are **not** automatic under a plain `uvicorn ...` line —
`uvicorn`'s own defaults are larger and it enables permessage-deflate, so a custom
launch and a direct launch are only equivalent when you pass the flags above.
`--no-proxy-headers` keeps a proxy header from spoofing the peer address the loopback
check reads. Starting a stream without the `streaming` extra fails with a clear
message rather than serving a broken route.

## The wire protocol (version 1)

One connection is one session. The client sends one JSON **start** message, then
binary **audio** frames, then a **control**. The server sends one JSON **ready**,
zero or more **event** messages, and exactly one terminal **final** or **error**.

### Client → server

**Start** (text, exactly once, first message):

```json
{
  "type": "start",
  "version": 1,
  "format": "pcm_s16le",
  "sample_rate": 16000,
  "channels": 1,
  "language": "en",
  "token": "..."
}
```

- `version` must be the number `1` (not the boolean `true`); `sample_rate`,
  `channels`, and `format` are validated strictly. Anything else is a protocol
  error.
- `language` is `null` (auto-detect) or one of the seven codes the engine
  accepts: `"en"`, `"de"`, `"fr"`, `"es"`, `"it"`, `"nl"`, `"pl"`. Anything else —
  a code not in that set, or a non-string — is a protocol error. **Only `"en"` has
  been exercised on the native engine here**; the other six are accepted by the
  engine and passed through, but are not covered by local live evidence.
- Credential fields: `token` (the API Bearer token) and/or `capability` (the UI's
  per-process token). **Which are required depends on where you connected, and in
  the production UI it is *both*.** Standalone and headless clients authenticate by
  `token` (or an `Authorization: Bearer` header); the developer UI needs
  `capability` alone; the production UI (same origin) requires `capability` **and**
  `token`. See *Authentication* below — the guard is not a simple "exactly one".

**Audio** (binary): mono signed-16 little-endian at 16 kHz, at most **32000 bytes**
per frame (one second). An empty frame is a protocol error. Frames are buffered and
fed to the core in arrival order.

**Control** (text): `{"type": "finish"}` asks the server to flush and finalize;
`{"type": "cancel"}` abandons the session. A control message is at most **8192
bytes** measured in UTF-8 bytes, not characters.

### Server → client

**Ready** (text, once):

```json
{"type": "ready", "version": 1, "session": "...",
 "limits": {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le",
            "max_chunk_bytes": 32000, "max_queue_seconds": 5,
            "chunk_deadline_s": 10.0, "idle_timeout_s": 60.0}}
```

**Event** (text, repeated): one transcript update. Its `event` member is exactly
`StreamEvent.to_dict()` — a delta appended once (`committed_delta`), the live tail
you must **replace** (`pending`), a monotonic `seq`, the session sample clock
(`t_audio_s`), and word timings. An `event` message is **not** a transcript; the
canonical transcript arrives only in the `final` message.

```json
{"type": "event",
 "event": {"seq": 3, "kind": "transcript",
           "committed_delta": "good morning",
           "pending": "everyone thanks for",
           "is_final": false, "t_audio_s": 2.5, "language": "en",
           "pass_ms": 4.1,
           "words": [{"text": "good", "start": 0.16, "end": 0.4, "probability": 0.99}]}}
```

**Final** (text, once, after `finish`): the remaining **transcript** events are
drained first, then one terminal message. Its `event` is the final
`StreamEvent` (`is_final: true`, empty `pending`), and its `transcript` is the
canonical `Transcript` mapping — `source`, `language`, `duration`, `engine`,
`metadata`, and **`segments`** (not a `text` key), each segment carrying the
committed text and word timings:

```json
{"type": "final",
 "event": {"seq": 12, "kind": "final", "committed_delta": "from the last quarter.",
           "pending": "", "is_final": true, "t_audio_s": 12.0, "language": "en",
           "pass_ms": 0.0, "words": [{"text": "quarter.", "start": 11.76, "end": 11.92, "probability": 0.71}]},
 "transcript": {"source": "stream:1a113c6309f-3380", "language": "en", "platform": null,
                "duration": 12.0, "engine": "whistle-streaming",
                "metadata": {"events": 13, "live": true},
                "segments": [{"start": 0.0, "end": 12.0, "text": "Good morning, everyone. …",
                              "speaker": null, "translated_text": null, "hidden": false,
                              "words": [{"start": 0.16, "end": 0.4, "text": "Good"}]}]}}
```

**Error** (text, once, terminal): `{"type": "error", "code": "...", "message": "..."}`.
These are the codes the transport actually emits:

| Code | When |
| --- | --- |
| `STREAMING_UNAUTHORIZED` | The `start` did not authenticate (no anonymous path). |
| `STREAMING_PROTOCOL_ERROR` | A malformed `start`, an oversize/empty/odd audio frame, audio after `finish`, malformed JSON, or an unknown control. |
| `STREAMING_SESSION_LIMIT_ERROR` | Starting would exceed the process-wide concurrent-session ceiling. |
| `STREAMING_QUEUE_FULL` | The core's input audio queue would exceed its 5 s bound. |
| `STREAMING_SESSION_STATE_ERROR` | A lifecycle call in a state that forbids it (e.g. feed after finish). |
| `STREAMING_WORKER_ERROR` | The owned native child died, stalled, or could not launch. |
| `STREAMING_RESOURCE_BUDGET_EXCEEDED` | A per-session budget (transcript size, queued events) was crossed. |
| `STREAMING_HANDSHAKE_TIMEOUT` | No `start` arrived before the handshake deadline (5 s). |
| `STREAMING_IDLE_TIMEOUT` | No audio or control for 60 s. |
| `STREAMING_FINISH_TIMEOUT` | The flush did not complete within its deadline. |
| `STREAMING_TRANSCRIPT_ERROR` | The final transcript could not be built. |

The message never echoes a secret.

### Lifecycle rules

- **No replay, no resume, no reconnect.** A session is ephemeral: there is no job
  row for it and no audio archive. If the socket drops, the session is cancelled and
  gone; open a new one.
- **No audio after `finish`.** Sending audio while finishing is a protocol error.
- The idle timeout is 60 s; the client must send audio or a control within that
  window.
- **The input queue is bounded, not acknowledged per frame.** The core holds at
  most 5 s of **accepted-but-unprocessed** audio — frames the core has taken in but
  the engine has not yet consumed (fed minus processed), *not* audio awaiting
  delivery; a frame that would push that backlog past 5 s is refused with
  `STREAMING_QUEUE_FULL`, never silently dropped. Separately, that same
  accepted-but-unprocessed backlog must be processed within a 10 s deadline
  (`chunk_deadline_s`) or the session fails with `STREAMING_WORKER_ERROR` — the
  bound is on the *oldest outstanding* audio, not on each frame, so there is no
  per-frame acknowledgment a client participates in.
- A **slow reader** (a peer that stops reading) is aborted, not buffered without
  bound. Every server send is bounded (5 s).

## Authentication

The rule is fail-closed: **there is no anonymous path.** Which credential proves you
depends on who you are.

| Caller | Where | Credential |
| --- | --- | --- |
| Browser served by the local UI, **developer profile** | `/api/stream` from the UI's own origin | `capability` — the UI's per-process token, embedded in the page. The token is *not* required. |
| Browser served by the local UI, **production profile** | `/api/stream` from the UI's own origin | `capability` **and** `token` (the API token). Both are required; the capability is the extra same-origin check, never a replacement for the token. |
| Programmatic client (no `Origin`) | either endpoint | `Authorization: Bearer <TEXTFLOWKIT_API_TOKEN>` header, **or** `token` in the start JSON |
| Configured cross-origin app | the standalone API only | `token` (API token); the origin must be listed in `TEXTFLOWKIT_STREAMING_ORIGINS` |

- The **UI's `/api/stream` accepts only the UI's own origin.** The cross-app allowlist
  never widens it: a browser on any other origin is refused at the handshake, before
  a session is allocated, even if it presents a valid capability.
- The **standalone `/stream`** honours `TEXTFLOWKIT_STREAMING_ORIGINS` — a
  comma-separated list of exact `http(s)` origins (no wildcard, no credentials, no
  path). This is how a separate "God Eye View" dashboard on another origin can open a
  stream; it authenticates with the API token, not a UI capability.
  **A browser needs its own origin listed here, including the standalone example.**
  The standalone config has no own origin and an empty allowlist, so a page served
  from `http://127.0.0.1:8767/streaming-example` is refused at the handshake until the
  operator lists that exact origin:

  ```bat
  set TEXTFLOWKIT_STREAMING_ORIGINS=http://127.0.0.1:8767
  textflowkit-http --streaming
  ```

  There is **no non-browser bypass to infer** the origin from: the server cannot tell
  a local page from a remote one, so a missing entry is a refusal, not a silent
  allowance. A programmatic client (no `Origin`) is unaffected and needs no list entry.
- Streaming is **always loopback**: both the `Host` and the peer address must prove
  loopback, even under `TEXTFLOWKIT_ALLOW_REMOTE`. A non-loopback peer is refused.
- For the **standalone app and any non-UI client**, the API token is required
  regardless of profile: an empty or unset `TEXTFLOWKIT_API_TOKEN` matches nothing —
  it is never treated as anonymous. The developer UI is the one exception: when the
  host does not require a token, the UI's own `capability` is the sole credential.
- In the **production profile** (`TEXTFLOWKIT_PROFILE=production`) the same Bearer /
  token checks that guard HTTP apply, and the UI additionally requires the capability —
  so a production UI stream presents **both**. There is no bypass.
- A token is **never** placed in a URL, a cookie, or a log line. The browser sends it
  in the first WebSocket message.

## Minimal Python client

A non-browser client authenticates with the `Authorization` header. This uses the
`websockets` library (installed by the `streaming` extra):

```python
import asyncio, base64, json, os
import websockets

async def main():
    token = os.environ["TEXTFLOWKIT_API_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    async with websockets.connect("ws://127.0.0.1:8767/stream", additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "type": "start", "version": 1, "format": "pcm_s16le",
            "sample_rate": 16000, "channels": 1, "language": "en",
        }))
        print(await ws.recv())                      # ready
        pcm = b"\x00\x00" * 8000                    # 0.5 s of silence
        await ws.send(pcm)
        await ws.send(json.dumps({"type": "finish"}))
        async for message in ws:
            print(message)                          # events, then final
            if json.loads(message).get("type") in {"final", "error"}:
                break

asyncio.run(main())
```

A browser page authenticates differently: no `Authorization` header (the browser does
not send one), so it puts `capability` (or `token`) in the start JSON. The packaged
example is the worked reference.

## The `StreamingSession` API (transport-neutral core)

Everything above is the *wire*. The wire is a thin adapter over one public object,
`StreamingSession` (importable straight from the package:
`from textflowkit import StreamingSession`). It is transport-neutral — PCM in,
normalized events out, no sockets — so an application can drive live transcription
directly, or build another transport (a CLI tail, a different RPC) on the same core.

### Methods and parameters

| Member | Contract |
| --- | --- |
| `StreamingSession(*, language=None, keywords=None, worker_factory=None, weights_path=None, audio_queue_seconds=5, auto_start=False)` | `language` is `None` (auto-detect) or one of the seven accepted codes. `keywords` is an optional bounded sequence of strings (≤32 keywords — `MAX_KEYWORDS` — each ≤256 chars — `MAX_KEYWORD_CHARS`); it is **accepted and validated but currently has no effect on native inference** — the session stores it and never passes it to the owned worker, whose start command carries only `language` and `weights_path`. Do not rely on it to bias recognition. `audio_queue_seconds` is the input-queue bound (1–5). `auto_start=True` runs `start()` during construction. Constructing does **not** launch the native worker unless `auto_start` is set. |
| `start() -> StreamingSession` | Reserves a session slot, launches the owned worker subprocess, and loads the model. Raises `SessionLimitError` past the concurrency ceiling and `SessionStateError` if called in the wrong state; on failure the worker is torn down and the slot released. Cancel-safe: a `cancel` racing it wins. |
| `feed(audio) -> int` | Queues one PCM frame — s16le mono 16 kHz, non-empty, an even number of bytes, at most 32 000 bytes (1 s). Returns bytes accepted. Raises `StreamingProtocolError` on a malformed frame and `StreamingQueueFullError` if the queue bound (5 s) would be crossed. Never blocks on the engine. |
| `read_event(timeout=None) -> StreamEvent \| None` | Returns the next event, or `None` if none is ready within `timeout` seconds. A worker failure or a protocol violation raises. |
| `finish(timeout=None) -> StreamEvent` | Runs the native stop once, drains the tail, releases the worker. Returns the single final event. Idempotent. |
| `cancel() -> None` | Terminates and releases the owned worker within the 2 s cancel deadline, then makes the session terminal. Safe to call from another thread; it never acts on another process. |
| `close() -> None` | A `cancel()` if the session is still running, else a no-op. |
| `with StreamingSession() as s:` | Context manager: `__enter__` starts the session, `__exit__` calls `finish()` on a clean exit and `cancel()` on an exception. |

### State, results, and error semantics

- **State** (`s.state`): `created → running → finished | cancelled | failed`. `s.finished`, `s.cancelled`, and `s.id` (the stable id; the final transcript's `source` is `stream:<id>`) are read-only properties.
- **`final_transcript`** is the canonical `Transcript` — available only for a `finished` session (otherwise `SessionStateError`). `source` is `stream:<id>`; the committed text becomes one `Segment` carrying the word timings; `duration` is the audio actually fed.
- **Errors** are `StreamingError` subclasses, each with a stable `.code`: `StreamingProtocolError` (`STREAMING_PROTOCOL_ERROR`), `StreamingQueueFullError` (`STREAMING_QUEUE_FULL`), `StreamingResourceError` (`STREAMING_RESOURCE_BUDGET_EXCEEDED`), `SessionStateError` (`STREAMING_SESSION_STATE_ERROR`), `SessionLimitError` (`STREAMING_SESSION_LIMIT_ERROR`), and `WorkerProcessError` (`STREAMING_WORKER_ERROR`).
- **`committed_delta` is appended once; `pending` is replaced.** To build the live text, append each event's non-empty `committed_delta` (join with a space) and *replace* your view of the tail with `pending` — never append `pending`.
- **Do not double-count the final event.** `finish()` **returns** the final `StreamEvent` and also **queues** it (with any transcript events from the tail drain before it). If you both iterate `read_event` to `None` and use the return value, you will see the final event twice; consume one or the other, not both. The wire adapter sends it exactly once, inside the `final` message.
- **One reader, one finisher.** The session serializes reads, so concurrent `read_event` calls do not split the stream — but do not run `finish()` concurrently with a live `read_event`. Drain events promptly: the event queue is bounded (1024); if a consumer stops reading, back-pressure raises `StreamingResourceError` rather than dropping silently.

### Minimal paced PCM-frame iterator

This drives the core directly over a Python iterator of PCM frames — no socket.
The frame source is *yours* (a deque, a file decoded to s16le, a callback); the
loop feeds one frame at a time and drains events as they appear, mirroring the
1 s audio cadence the engine expects:

```python
from textflowkit import StreamingSession

def frames():                      # your s16le mono 16 kHz source, <= 1 s each
    while True:
        chunk = next_chunk()       # bytes, e.g. 32000 == 1 s
        if not chunk:
            return
        yield chunk

with StreamingSession(language=None) as live:      # starts the owned worker
    committed = []
    for frame in frames():
        live.feed(frame)
        event = live.read_event(timeout=1.0)         # StreamEvent or None
        if event is not None and not event.is_final:
            if event.committed_delta:
                committed.append(event.committed_delta)   # append once
            # event.pending REPLACES your tail view; never append it
    final = live.finish()                            # returns the final event, and
                                                     # also queues it AFTER the tail
    # The tail of non-final transcript events from the finish drain is queued ahead
    # of the final event, so drain it first - appending the returned final event
    # before these would reverse the transcript order.
    while (tail := live.read_event(timeout=0.0)) is not None:
        if not tail.is_final and tail.committed_delta:
            committed.append(tail.committed_delta)   # transcript events, in order
    if final.committed_delta:
        committed.append(final.committed_delta)      # the returned final, once, last
    print(" ".join(committed))
    print(live.final_transcript.to_dict(include_words=True))
```

The `with` block starts the session on entry. On exit, `__exit__` calls `finish()`
when the body ended cleanly and the session is still `running`, `cancel()` when the
body raised, and `close()` otherwise — so after the explicit `finish()` above the
session is already `finished` and the exit is a no-op. The explicit `finish()` is
shown because it is the only way to *capture* the returned final event.

**Canonical order of committed text.** `finish()` **queues** the final event *after*
any non-final transcript events still buffered from the flush, and the drain above
reads those buffered events *before* it appends the returned final event — so the
joined text follows the audio timeline. The order is: the live events as they were
read, then the finish-drain tail, then the final event last. Appending the returned
final event before draining the queue is the one mistake to avoid: it reverses the
tail and the final, producing a transcript whose last words appear out of order.

## The browser example

`GET /streaming-example` (standalone) and `GET /api/streaming-example` (UI) serve the
same packaged page. It:

- **waits for an explicit click.** Nothing connects and no microphone is requested
  until **Start microphone** is pressed; the browser's permission prompt is the
  authorization.
- captures with an **`AudioWorklet`** — the packaged
  `/assets/streaming-capture-worklet.js` — which resamples any device rate
  (44.1 kHz, 48 kHz, …) to 16 kHz mono with **continuous phase**, so a tone crossing a
  block boundary is neither dropped nor duplicated. Output is signed-16 little-endian,
  clipped, in frames of at most one second.
- **Stop and finish** stops the microphone immediately and hands the worklet a
  single atomic `stop-and-flush` command: the worklet stops producing, posts the
  final partial PCM, and then acknowledges it on the same port, in that order, so no
  `process()` call can slip a frame between the flush and the ack. The page sends the
  flushed PCM (binary) as soon as it arrives — **before** the ack — then, on the ack,
  sends `finish`; the wire order is therefore **flushed PCM first, then the ack, then
  `finish`**, so no audio frame ever follows `finish`. It then closes the audio
  context and the worklet port while **retaining the socket** to receive the server's
  final. If the worklet does **not** acknowledge within a short bound, the stop is an
  **error**: the page cancels the session and releases everything, rather than
  reporting an incomplete flush as a successful finish. **Cancel** releases the
  microphone tracks, the audio
  context, and the worklet immediately and clears every timer the session armed, and
  a stream that arrives after Cancel (a late permission grant, or a worklet that
  loads late) is stopped instead of started.
- renders the transcript with `textContent` only — committed text appended once,
  the pending tail replaced — never `innerHTML`.
- stops if the socket backs up (`bufferedAmount` high-water mark): a live transcript
  must not silently lag.

The resampler is a plain ES module, so it is tested deterministically under Node —
44.1 kHz and 48 kHz inputs, the 16 kHz output rate, amplitude/sign, clipping, the
explicit little-endian byte order, the flush acknowledgement, and that a stopped
worklet emits no further audio — without ever opening a real microphone. The page's
own lifecycle (Start/Cancel ordering, the stop/flush handshake, a socket that closes
early, a stale socket's late callbacks) is driven the same way, against a fake DOM and
fake audio/WebSocket environment.

## Integrating a "God Eye View" app

Any application — a dashboard, an overlay, another local tool — can consume the
stream with **no dependency on TextFlowKit's Python code**. It needs only:

1. A WebSocket connection to `ws://<host>:<port>/stream`.
2. An `Origin` that the server operator listed in `TEXTFLOWKIT_STREAMING_ORIGINS`, and
   the API token as an `Authorization: Bearer` header (or `token` in the start JSON).
3. The start message and binary audio above; interpret `ready`/`event`/`final`/`error`.

The endpoint speaks plain JSON and PCM over a standard WebSocket, so the integrating
app is written in whatever language it already uses. It is a *client* of the contract
on this page, not a Python import.

## Limits and known constraints

- **Loopback only.** Remote streaming is not supported, whatever
  `TEXTFLOWKIT_ALLOW_REMOTE` is set to.
- **Live Windows x86_64 is supported and exercised here.** The stream core runs the
  separately pinned `libneedle3.dll` through direct `ctypes` in one owned native
  child. The standalone file transcriber uses its own native CLI; its defaults
  and cross-platform support are unchanged. **Other live platforms are unsupported**
  until their native member pins and behavior are verified. The transport, browser
  resampler, and core have automated coverage.
- **Bounded native runs are verified locally.** A real live stream ran end-to-end on
  this machine (Windows x86_64) for **65 s** and again for **300 s**, driving the
  product core with paced PCM. A separate assembled UI WebSocket run fed
  prerecorded PCM at real time for 12 s and returned incremental events and a final
  transcript. Longer runs than 300 s were not performed. **A real browser microphone has not yet been captured in this
  evidence** — the browser-side proof drives the packaged page against a fake audio
  environment under Node, not a physical device; treat the microphone path as
  wired-but-unproven on hardware.
- **One ordered consumer.** The server runs a single core consumer; the receiver and
  the consumer never call the core concurrently, and a disconnect cancels the session
  and tears down its child process — including a disconnect during startup or finish.
- **Bounded everything.** Handshake concurrency (8), idle timeout (60 s), per-send
  timeout (5 s), control-message size (8 KiB), audio frame (32 KiB), queue (5 s of
  audio). Above any of these the connection is refused or aborted with an explicit
  error, never a silent drop.
- On server shutdown, live sessions are cancelled **before** the job workers drain,
  so no stream outlives the process.

## The native runtime it loads

The stream is a *separate* artifact from the standalone CLI at the same upstream
revision — the `needle_stream_transcribe_*` symbols live only in the library wheel,
which is why streaming pins its own asset:

- **Pinned wheel:** `cactus_needle` **3.2.0**, at upstream revision
  `2ae11323dc000f5e70c49f7403efa6af12ba9e67`. The whole wheel is pinned by size and
  SHA-256, and the single extracted library member (`needle/libneedle3.dll`) is
  *separately* pinned by size and SHA-256 as an explicit provenance check on the one
  file that actually loads. Only the Windows x86_64 member hash is verified here;
  other platforms are refused rather than run unverified.
- **Cache location:** a versioned subdirectory of the per-user models cache,
  `…/streaming/libneedle3-<revision12>/`. The environment variable
  `TEXTFLOWKIT_STREAMING_DIR` overrides it — a separate cache from the CLI's, so a
  streaming install lives beside, never on top of, the standalone runtime.
- **Shared model:** the streaming worker loads the **same pinned `whistle.cact`**
  model the CLI uses, from the same per-user cache and against the same pin — it does
  not fetch a second copy. An explicit `weights_path` is verified in place against the
  pin.
- **Offline:** with `TEXTFLOWKIT_OFFLINE=1`, a missing library or model raises an
  explicit error instead of downloading; a cached asset is still verified against its
  pin before use.
- **No SDK, no telemetry:** the worker binds the DLL **directly via `ctypes`**; it
  imports no `needle`/`cactus_needle` SDK and therefore never loads upstream's
  telemetry module. Telemetry-off is additionally forced into the child's environment
  (`NEEDLE_TELEMETRY=0`), so nothing about a session is reported upstream.
- **Standalone unchanged.** None of this affects file-based transcription: the CLI
  keeps its own pinned `needle.exe` and its native audio-stream path stays disabled.

### Integration receipt (native, Windows x86_64)

A short real-native UI-stream run over the actual WebSocket transport — a 12 s paced
PCM stream through `textflowkit-ui`'s `/api/stream`, **not** a physical microphone:

- First committed text `1.080 s` after the stream began; the whole 12 s run took
  `12.080 s` wall for 12 s of audio (real-time).
- Disconnect cleanup (owned child terminated and reaped) completed in `0.029 s`,
  leaving zero active sessions.
- A wrong capability **and** a wrong `Origin` were both rejected at the handshake.

This receipt measures transport and teardown timing; it does **not** measure
microphone capture-to-token latency, which no run here has captured. The earlier
65 s and 300 s native proofs above remain the bounded-liveness evidence.

## See also

- [Adapters](adapters.md) — where the HTTP/WebSocket surfaces fit.
- [Architecture](architecture.md) — the pipeline diagram.
- [User manual](user-manual.md) — day-to-day use.
- [Install](install.md) — the `streaming` extra.
