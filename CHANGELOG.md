# Changelog

## Unreleased

- Open AI comparison across the full browser workspace with equal Original/AI-cleaned columns, labeled narrow-screen stacking, and Back/Escape navigation. Preserve timestamps, review reasons and corrected exports.

## v0.1.17 — 2026-10-20

- Enforce reported uncertainty preservation with a deterministic source-token diff: suppress whole overlapping edits and boundary insertions, keep independent corrections, and fall back to original segment text when precise mapping is unavailable. Invalid source flags still fail closed; no extra inference pass.

- Improve opt-in local transcript proofreading with a bounded neighboring passage and explicit uncertainty metadata. Derive exact offsets from model-reported source wording; preserve unclear wording verbatim, highlight review spans and retain review markers in corrected TXT and clipboard. Existing saved variants remain usable with an unassessed notice; canonical source, timings, hidden-segment exports and local-only safeguards remain unchanged. One inference per chunk; no accuracy guarantee. Core and Windows version 0.1.17; fonts remain 0.1.6.

## v0.1.16 — 2026-10-20

No-telemetry privacy release; core 0.1.16, unchanged optional fonts 0.1.6.

- Disable optional pyannote usage metrics and Hugging Face telemetry before optional imports; preserve Whistle as the default engine and its forced-off child settings, necessary model/media downloads, explicit media/model requests and gated-model authentication.
- Disable Cloudflare Web Analytics/RUM injection and Network Error Logging for textflowkit.org; verified in production on 2026-10-20 and documented separately from repository HTML and necessary provider operational/security logs.

- Publish the genuine `TextFlowKit-0.1.16-windows-x64-setup.exe`, built from the immutable tag and published core wheel, with matching checksums, payload manifest and unchanged dependency sources/notices. Actual isolated install, version readback, native Whistle speech selftest, privacy guards and browser UI launch passed. See [acceptance](docs/verification/windows-installer-v016-20261020.md).

## Documentation follow-up — 2026-10-20 (no new binary release)

- Check current README, manuals, website, architecture diagrams, packaging and contributor instructions against v0.1.15 interfaces. Correct the HTTP example's engine/model mismatch, GPU selftest selection, export endpoint, broken section anchors and remaining stale installer versions.
- Document all three speech engines in the overview diagram and keep current documentation separate from historical test evidence and immutable dependency/source snapshots. See the [documentation check record](docs/verification/documentation-surfaces-20261020.md).

## v0.1.15 — 2026-10-20

Developer-tooling patch; core 0.1.15, unchanged optional fonts 0.1.6.

- Adopt the owner-required LOW effort for DeepSeek 1017, superseding off/none in current agent and contributor instructions. Preserve the exact model and native isolated headless Codex harness.
- Align the historical direct-Ollama runner's metadata and payload to `think: "low"`; remove its incompatible rejection of reasoning output. This runner remains prohibited as the TextFlowKit coding harness; the exact model allowlist and other safeguards remain in place.
- Update current README, user/integration manuals, installation instructions and website versions. Application transcription, cleanup and streaming behavior are unchanged; Whistle remains the default.
- Windows distribution follow-up: genuine v0.1.15 EXE rebuilt from its immutable tag and published wheel, with actual isolated-install/version/Whistle speech/UI acceptance. Installer, payload manifest, checksums and unchanged matching dependency-source/notice snapshots published together. Fonts remain 0.1.6; no new four-hour accuracy claim.

## v0.1.14 — 2026-10-19

Documentation-only patch; core 0.1.14, unchanged optional fonts 0.1.6.

- Persist the required native headless Codex workflow for all TextFlowKit DeepSeek coding: exact `deepseek-v4.1-flash:1017-cloud`, thinking off, isolated worktree/configuration/environment, supported Windows sandbox, and no silent fallback to the historical Python runner or Claude Code.
- Link the workflow from contributor and integration documentation and update current package/manual/website release references.
- No transcription, cleanup, streaming, dependency, or installer behavior change. The latest verified Windows installer remains v0.1.13 and retains its matching FFmpeg sources/notices. Native/live proof remains explicitly attributed to that earlier release; no new accuracy claim.
- Math comparison fixtures, local logs, model credentials and machine-local launcher/profile files are not included.

## v0.1.13 — 2026-10-19

Core: 0.1.13; unchanged optional fonts: 0.1.6. Whistle missing-speech fixes and a
native Windows installer distributed with matching FFmpeg sources and notices.

- Bound native Whistle decode inputs to 15 seconds with 11-second owned cores
  and up to two seconds of context per side. The former 30-second inputs
  reproducibly omitted passages recovered by shorter inputs. Window policy is
  part of resume identity; partial progress from the old plan requires a fresh
  run. The engine and pinned model remain Whistle.
- Check short gaps with a bounded, differently framed native decode. Add words
  only between two matching timed anchors; retain original words/repetitions,
  skip long pauses, and cap extra calls per core. Record recovery counts.
- Correct Whistle shared-context assembly when independently estimated word
  timestamps cause both neighboring cores to keep or discard the same occurrence.
  Match occurrences one-to-one; preserve real repeated speech and leave ambiguous
  collapsed repetitions untouched rather than using global text deduplication.
- Persist raw boundary context in Whistle progress schema 2 so resumed assembly
  matches uninterrupted output. Old schema-1 partial runs require a fresh start;
  completed jobs remain unchanged. Default engine remains Whistle.
- The native 30-second omission cases remain reproducible upstream; shorter
  input windows and anchored gap checks mitigate them in TextFlowKit without
  changing the engine or model weights.
- Verification on one four-hour public meeting (1310 native cores, 18 min 34 s
  native decode, 34,723 final words): the two long missing passages were
  restored (2 to 50 and 0 to 47 words), 62 short-gap checks accepted 7 for 53
  added words, resume after cores 350 and 700 reproduced exact word text and
  timings, and no invalid bounds or backwards starts remained. Focused suite:
  113 passed. Full suite: 2,097 passed, 18 skipped; skips are not passes. This is
  coverage evidence for one recording, not human WER or perfect accuracy; normal
  spelling errors remain. See
  [the verification note](docs/verification/whistle-coverage-20261019.md).

- Package matching FFmpeg sources, pinned build recipes, dependency archives and full notices alongside the Windows installer; record source hashes and preserve compatible DLL replacement. See [source verification](docs/verification/windows-sources-20261019.md).

## v0.1.12 — 2026-10-17

Core: 0.1.12; unchanged optional fonts: 0.1.6. The native Windows installer has been built and tested locally. The installer recipe ships in v0.1.12; the EXE remains local-only until unresolved corresponding-source compliance review is cleared. No public installer download link is available.

### Optional local UI proofreading (v0.1.12)

- Add explicit post-transcription local Ollama proofreading with an installed-tag
  picker; prefer `qwen3.5:9b` only when installed (size/performance fit unverified),
  otherwise require selection.
  No model downloads, cloud tags, paid provider, telemetry or new package dependency.
- Preserve original jobs and exports; persist a separate original-hash-bound AI
  variant with provider/model/time provenance, source segment IDs/timestamps, no
  inherited word alignment, comparison/original-only views and corrected TXT/copy.
- Bound background work and validate complete JSON/segment coverage atomically.
  One cleanup at a time; fail without partial output. Normal shutdown drains work;
  interrupted leases expire. AI corrections require human review.

- Validation history: the first cold full-UI attempt ended in a generic error; a
  later hot 39-segment run returned zero edits despite clear spelling errors.
  Final cold run (model explicitly unloaded first) completed all 39 segments in
  three chunks with four changed segments at 2026-10-16 17:39:52 UTC. The original
  SQLite JSON/source and IDs/timestamps matched; visible side-by-side review and
  corrected TXT save were verified, with the saved file hash matching the variant.
  This proves workflow completion for one run, not accuracy; “fundamental” and
  “victims” were plausible guesses not verified against audio. Review against audio.

### Native Windows installer

- Package the existing local browser UI in a native per-user installer for core
  v0.1.12. Built and tested locally; public EXE publication remains gated on
  corresponding-source compliance review. No public installer link is available.
- Bundle: private embeddable CPython, the UI and HTTP, export, and
  streaming dependencies, the pinned Whistle model and runtime, and ffmpeg,
  ffprobe, and Node. The installer is designed to need no separate Python, WSL,
  or Docker installation.
- Prebundled Whistle assets and media binaries mean local file jobs need no
  first-use asset download. URL media still needs network access. The default
  Whistle engine supports only these seven advertised languages: `en`, `de`,
  `fr`, `es`, `it`, `nl`, and `pl`.
- Per-user setup with a Start Menu entry and optional desktop shortcut; launch
  starts the loopback server and opens the existing UI in the browser. It normally
  needs no administrator rights, changes no global `PATH`, and has no automatic
  updates.
- Keep UI data outside the app install directory and preserve it on upgrade and
  uninstall. Stop server and wait before upgrade or uninstall; the candidate will
  block while it is running rather than force-kill active jobs. Stop server drains
  jobs, so cancel one first if it should not finish.
- Do not bundle optional `openai-whisper` or diarization dependencies; their
  documented optional paths remain available through the Python package. Bundled
  streaming dependencies do not change the opt-in default; physical browser
  microphone capture remains unverified.
- The installer bundle includes third-party license and notice files,
  including FFmpeg LGPL notices and corresponding-source information. Verify
  exact notices and source-distribution details against final binaries before
  publication.
- The candidate is unsigned, so Windows may show a SmartScreen warning.

## v0.1.11 — 2026-10-06

Optional native live streaming supports **Windows x86_64 only**. Core version:
0.1.11; unchanged optional fonts: 0.1.6. Existing standalone Whistle defaults,
file/URL jobs, CLI, and MCP behavior are unchanged. The public site remains static
documentation and serves no streams. Local source validation does not establish
published-package verification or cross-platform live support.


### Added

- **Public `StreamingSession` API.** Bounded PCM feed, typed incremental events,
  flush/finish, cancellation, canonical final `Transcript`, and context-manager
  cleanup. One owned native child per session binds directly to the separately
  pinned `libneedle3.dll`; no upstream Python SDK or telemetry module is loaded.
  Live sessions are ephemeral, with no replay or durable resume.
- **Live microphone streaming (opt-in, loopback-only).** When
  `TEXTFLOWKIT_STREAMING=1` is set, the developer HTTP app serves a `/stream`
  WebSocket and the local UI serves `/api/stream`, both transcribing audio as it
  arrives. A runnable, self-hosted browser example is at `/streaming-example`
  (and `/api/streaming-example` under the UI); its `AudioWorklet` resamples any
  device rate to 16 kHz mono with continuous phase. Off by default: with the
  switch unset the endpoint does not exist. The standalone `textflowkit` CLI is
  unaffected — it does not open a microphone or a streaming socket. Requires the
  new `streaming` extra (`pip install "textflowkit[streaming]"`), which adds the
  `websockets` dependency. Auth is fail-closed: the UI accepts only its own origin
  and capability, the standalone API requires the API token (even in developer
  mode), and a separate origin may stream only if the operator lists it in
  `TEXTFLOWKIT_STREAMING_ORIGINS` (a browser needs its own origin listed, including
  the standalone example's). See [docs/streaming.md](docs/streaming.md).
  **Bounded live runs verified on this machine's native engine** — 65 s and 300 s
  through the product core on Windows x86_64. A separate assembled UI WebSocket
  run fed prerecorded PCM at real time for 12 s. Browser lifecycle and resampling
  checks used fake audio; **physical microphone capture was not verified**. Only
  English was measured natively; the other six accepted languages and live
  Linux/macOS were not exercised. No public remote streaming is provided.

## v0.1.10 — 2026-10-06

**The local browser interface ships.** The loopback-only browser workspace
(`textflowkit-ui`) is now part of the core package rather than source-only, so a
regular install of this release carries the command. Core version: 0.1.10;
unchanged optional fonts: 0.1.6.

### Added

- **Local browser interface in the core package.** `textflowkit-ui` serves a
  point-and-click workspace on **this machine only**: it binds `127.0.0.1` (never
  off the machine, whatever `TEXTFLOWKIT_ALLOW_REMOTE` is set to) and opens a
  browser once serving. It mounts the existing developer HTTP app under `/api`
  **unchanged** — the same routes, submission contract, and job store — and adds
  only transport: a loopback session layer, a streamed upload, a fixed-format
  download, in-workspace playback, and a capability endpoint. It adds **no new
  dependency** beyond the existing `http` extra (FastAPI + uvicorn).
- **Durable jobs, cancel and resume in the workspace.** Jobs and checkpoints are
  stored durably under the per-user data directory unless
  `TEXTFLOWKIT_DB` / `TEXTFLOWKIT_WORK_ROOT` / `TEXTFLOWKIT_OUTPUT_ROOT` is set.
  A dropped file is streamed and capped at **2 GiB** by default
  (`TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES` overrides it). A running job can be
  cancelled, and a durable job resumed.
- **Windows desktop shortcut.** `textflowkit-ui --create-shortcut` creates a Start
  Menu entry that launches with `pythonw` (no console) and needs no `pywin32`.
  Under `pythonw` a startup failure is logged under the per-user data directory
  and shown in a message box rather than failing silently.
- **Controlled shutdown that drains.** **Stop server** asks this process's own
  uvicorn server to exit through its supported `should_exit` flag; the normal
  lifespan teardown **waits for current jobs to finish** before draining the
  worker pool rather than interrupting them, so a running job's saved progress
  survives and is resumable. It never sends a signal to a pid.

### Changed

- **Documentation surfaces.** README, user manual, install notes, developer
  manual, architecture guide, roadmap, release checklist, site deployment, the
  landing page, and the checked-in SVG diagrams now describe the local browser
  interface as a shipped feature and a fifth door, and remove the earlier
  "unreleased candidate / install from source only" wording.
- **No endpoint-breaking change.** The CLI, Python API, MCP server, and JSON HTTP
  adapter are unchanged in this release; the optional engines, diarization, and
  translation keep their documented dependency requirements.

### Verification boundary

Automated coverage runs the UI's routes, packaging, ownership/shutdown, and
security boundaries. A **native Windows** machine, in a real browser, proved the
workspace end to end (drop a file, submit, download, stop). That is one platform's
browser proof, **not** a live Linux or macOS UI run — those platforms are covered
by automated tests only, and cross-platform CI (Windows/Linux/macOS) is a separate
release gate. **No live optional-backend (translation or
diarization) run through the UI is claimed.** The engine and packaging changes of
v0.1.9 are unchanged by this release.

## v0.1.9 — 2026-10-05

**Whistle is now the default engine.** This release ships the Whistle integration
prepared on the `feat/whistle-primary` branch.

### Changed

- **Whistle is the default engine; openai-whisper becomes an explicit opt-in.**
  Whistle is a CPU-only native CLI that needs no PyTorch, so a fresh
  `pip install textflowkit` no longer pulls the torch stack. `openai-whisper`
  moves to a new optional `whisper` extra and is selected with `--engine whisper`
  (or `engine="whisper"` on the Python/MCP/HTTP surfaces); the `all` extra still
  includes it. A saved request or checkpoint from before this change keeps running
  on the engine it names, so legacy Whisper work is never silently migrated, and
  legacy engine aliases are preserved so older decoders keep working.
- **Engine selection is never silent.** An unknown engine name, an unsupported
  language, or a non-CPU device for Whistle is refused at request construction —
  before any media is fetched — and the message names `--engine whisper` as the
  GPU alternative. The engine is not switched to satisfy a request.

### Added

- **Whistle engine.** Seven advertised languages (`en`, `de`, `fr`, `es`, `it`,
  `nl`, `pl`); native Windows x86-64/arm64, Linux x86-64/arm64, and Apple Silicon
  (an Intel Mac is refused and pointed at the explicit Whisper engine); no WSL.
  One pinned native binary and one pinned model are downloaded and hash-verified
  on first use, outside the package. Long audio is split into 26-second cores with
  up to 2 seconds of context (each clip ≤ 30 s); a word is owned by the clip whose
  core contains its midpoint, so repeated phrases are preserved. Native
  `--audio-stream` is never used.
- **Durable per-block resume for Whistle.** A long run persists a partial
  checkpoint after each core; a resume validates the source and decoded-WAV
  hashes and the configuration, then re-runs only the unfinished blocks and adopts
  the rest. A partial body is Whistle-only. Reuses the existing single SQLite
  store and its one-owning-process model.
- **Prompt, bounded cancellation.** A running Whistle run polls for cancellation
  between clips and terminates its exact owned child process, bounded by a
  per-clip timeout and a bounded stdout cap. A single long `openai-whisper` model
  call is still cancellable only at its next stage boundary.
- **Telemetry off, unconditionally.** Every Whistle child process forces
  `NEEDLE_TELEMETRY=0`, `DO_NOT_TRACK=1`, and `CI=1`, overriding a parent that
  opted in, and there is no opt-in setting. The product adds no analytics, usage
  SDK, anonymous ids, or events. `TEXTFLOWKIT_OFFLINE` refuses downloads and
  `TEXTFLOWKIT_MODELS_DIR` chooses the asset directory; the pinned first-use
  asset/model download is not telemetry.

### Documentation

- README, user manual, install/adapter/architecture/roadmap/release docs and the
  website source now describe Whistle as the shipped default. ROCm (and the
  torch-pinning caveat) is scoped to the explicit `whisper` engine and
  diarization, never to Whistle.
- Self-contained checked-in SVG architecture drawings were added under
  `docs/assets` and embedded in the README, both manuals (`user-manual.md` and the
  renamed **Developer and integration manual**, `adapters.md`), the architecture
  guide, and the landing page.
- The live YouTube release smoke pins `--engine whisper --model tiny` so its
  measured default-clip word stays reproducible across the default-engine change.

### Verification boundary

Local evidence only: a 4-hour (14,407-second) CPU run produced 555
clips / 34,596 words / 2,875 segments in 731 seconds, offline with telemetry
forced off, and a separate two-process run proved durable block resume. These are
first local test numbers on one machine — not a universal performance claim, and
not a claim that Whistle's accuracy equals Whisper's. No human-scored
word-error rate is claimed.

## v0.1.8 — 2026-10-02

The 2026-10-02 audit-lite repair release. Core version: 0.1.8; unchanged optional fonts: 0.1.6. It carries the four fixes for the post-release audit-lite of 2026-10-01 (findings AL-001 – AL-004). Release artifacts and publication status are recorded on [GitHub Releases](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.8). Local source-candidate verification does not establish installed-package or individual harness compatibility.

### Engineering fixes

- **AL-001 — CLI owning-process startup recovery.** A CLI invocation that was
  interrupted left its PENDING/RUNNING row active; because the CLI submits with
  `background=False` it never started the recovering executor, so a later
  `--resume` read the orphan as "already active" and refused a job whose expensive
  work was saved. Recovery now runs once per owning process at the CLI's
  process/store boundary (`cli.main`), before single or batch resume selection,
  through the shared per-store owner. It is deliberately **not** an unconditional
  reap in every `submit_request(background=False)`: an embedding process can hold
  genuinely live work. A recovery write failure is reported as a clear error with
  a nonzero exit, not a false success or a raw traceback. The documented
  one-owning-process-per-database model is unchanged.
- **AL-002 — preserve the completed transcript across resume setup failures.** The
  source checkpoint serialized the local transcript (still `None`) before
  hydrating the previous transcript, so a directory/`mkdtemp` failure replaced a
  durable completed transcript with `null` while still listing `transcribe` as
  finished. Reuse is now hydrated and validated **before** any directory setup,
  and a replacement snapshot can no longer drop a still-finished transcript. A
  changed local source is still refused, stages are never falsely marked complete,
  and a healthy retry reuses the recognized work.

### Documentation corrections

- **AL-003 — MCP batch cookie capability.** The adapter guide said
  `submit_batch_media` has no `cookies_from_browser` parameter. It does: the tool
  accepts one shared value and forwards it to every request, and the production
  refusal is enforced by the same shared guard as the other surfaces. The matrix
  and paragraph (and the historical DOC-003 changelog text) were corrected, with a
  schema-consistency regression test that reads the real SDK-generated tool schema.
- **AL-004 — roadmap evidence.** The POSIX file-mode and fonts-reuse follow-ups
  are marked done against their exact receipts: current-main CI run 36961982098
  (12 OS/Python test jobs + three installed-wheel jobs + Ruff) verifies POSIX modes
  on Linux/macOS, and release-workflow run 36961162844 built the reused 0.1.6
  fonts, skipped the fonts upload, and published core to PyPI. The GitHub-release
  job skip, its manual recovery, and the PR #23 condition repair are recorded
  without claiming the original workflow was all green or the repaired automatic
  path has been live-tested. The v0.1.7 history and version are preserved.

## v0.1.7 — 2026-10-01

The 2026-10-01 audit repair release. Core version: 0.1.7; unchanged optional fonts: 0.1.6. Release artifacts and publication status are recorded on [GitHub Releases](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.7). Local source-candidate verification does not establish installed-package or individual harness compatibility.

### Engineering fixes

- **Confined decoder boundary.** With `TEXTFLOWKIT_INPUT_ROOT` set, a confined
  local input is staged and its decode is restricted to FFmpeg's self-contained
  demuxers (`-format_whitelist`), and the `ffprobe` duration probe carries the
  same whitelist. A playlist/manifest that would make the decoder open referenced
  files is refused. The restriction covers a resume that re-decodes; a stage with
  durable output is reused, not re-decoded. Owner runs without an input root stay
  unrestricted.
- **Atomic attempt ownership.** A worker pins the attempt generation it claimed;
  every progress write, checkpoint write, and terminal finalize is guarded on it,
  so a stale run cannot overwrite a newer attempt's label or verdict. The reopen
  of a terminal row for resume is an atomic claim pinned to the observed attempt —
  two callers racing one job id cannot both reopen it.
- **Atomic worker startup and cancellation.** Worker-thread startup is atomic, and
  the completion/cancellation race is decided in one place against the owned
  attempt: the row becomes CANCELLED only if that generation carries an accepted
  cancellation, DONE otherwise, so a finished DONE row can never still carry an
  accepted cancel.
- **DOCX identity.** Completed-output reuse for DOCX compares the archive bytes
  with only the local and central ZIP time/date pairs blanked; every other byte
  (member names and order, compression, flags, extra fields, comments, compressed
  content) must be equal, and the comparison never inflates a member.
- **Completed-stage reuse.** A run leaves a durable per-stage marker for each
  expensive stage it finishes; resume reuses only stages finished for the
  **requested configuration**, and a legacy coarse `postprocess` marker is honoured
  only for the optional stages that record actually requested. The rule keys on
  the marker, never on transcript metadata.
- **Aggregate export preflight.** The whole requested format set is validated
  (unsupported/duplicate names, then every dependency) and the aggregate rendered
  bytes are bounded **before any destination file is written**, for every surface
  that publishes output.
- **Translation completeness.** The translator's result length is checked against
  its input and every result is validated **before mutating any segment**: a
  blank result for a non-blank segment raises and leaves the transcript exactly as
  it was, rather than half-writing a set.
- **Per-item batch validation.** Each batch entry is constructed and admitted
  independently (HTTP and MCP): a non-object entry, a non-string/empty source, an
  unsupported format, or an unusable engine is **that** entry's error, carrying its
  original zero-based `index` and best-effort `source`, while the other entries
  still queue, in submitted order. There is no whole-batch engine preflight.
- **Terminal feedback.** A non-quiet CLI run streams live stage progress to
  **stderr, flushed** (stdout stays clean for `--stdout`); quiet suppresses it.
  Batch prints each item's result as it finishes. Job status exposes the recorded
  stage.

### Documentation corrections (DOC-001 – DOC-006)

- **DOC-001** — the user manual now states that canonical saved/exported JSON
  retains hidden segments and is an archive, not a redaction mechanism, and adds
  a worked safe-publication-copy example that filters the copy without mutating
  the stored transcript.
- **DOC-002** — the adapter guide and manual now distinguish response shapes:
  HTTP JSON returns a `transcript` object with page counts but no `next`, MCP
  returns a JSON string under `content` and adds a `next` instruction, and HTTP
  text reads carry no paging metadata. Time selection is described as
  overlapping rather than clipped.
- **DOC-003** — "same options" is replaced with a per-surface parameter matrix
  (HTTP `formats` array vs MCP comma-separated string; HTTP batch options are
  **per item**, MCP batch options are shared), and the batch contract now states
  that 202 means admission, not completion, with per-item indexed errors. MCP
  `sources` is documented as a list of **strings**: a non-string entry (including
  a nested object or a number) is that entry's own error, not a whole-call
  refusal, while a string remains valid.
  *Correction (2026-10-01, audit-lite AL-003):* the v0.1.7 text of this entry
  claimed the MCP batch tool exposes **no** `cookies_from_browser` parameter.
  That was wrong. `submit_batch_media` accepts `cookies_from_browser` and
  forwards the one shared value to every request it builds; the production
  refusal is enforced by the same shared guard as the other surfaces. The
  adapter guide was corrected to match the generated tool schema.
- **DOC-004** — a full production settings reference (names, defaults, units,
  required fields) is added to the adapter guide, and the production refusal of
  browser-cookie submissions is stated across the adapter guide, the sources
  guide, and `SECURITY.md`, including resume.
- **DOC-005** — the install guide chooses the diarization install path (preserve
  a working ROCm stack vs fresh CPU) **before** the first pip command, and the
  manual's ROCm deep link now resolves to the actual heading.
- **DOC-006** — new [architecture and lifecycle guide](docs/architecture.md)
  maps the core/CLI/MCP/HTTP doors, single-owning-process SQLite, startup
  recovery via the HTTP/MCP lifespans and the lazy executor, checkpoint reuse,
  and output publication; the adapter guide's "reaped at startup" wording is
  qualified to match. Linked from the README and `CONTRIBUTING.md`.

The production settings reference lives in
[docs/adapters.md](docs/adapters.md#production-settings-reference).

### Deferred audit follow-ups — later fixes

On 2026-10-01, the owner explicitly deferred the following findings from the
full five-role audit. They remain open; they are not completed fixes, new release
claims, or additions to the current core-bug repair goal. No release date is set.

See [Later fixes in the roadmap](docs/roadmap.md#later-fixes--2026-10-01-audit)
for the complete list and completion criteria. The current repair goal still
covers decoder confinement, job lifecycle, publication/resume, translation and
batch correctness, major terminal guidance/documentation, and its final gate.

This changelog starts with the v0.1.8 release notes, followed by the v0.1.7
release notes; published historical
release notes remain on [GitHub Releases](https://github.com/scottconverse/textflowkit/releases).
