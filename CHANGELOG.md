# Changelog

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
