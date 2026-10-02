# Changelog

## Unreleased

Fixes for the post-release audit-lite of 2026-10-01 (findings AL-001 – AL-004).
No version bump, no publication; the v0.1.7 tag and its notes remain immutable.

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

This changelog starts with the v0.1.7 release notes; published historical
release notes remain on [GitHub Releases](https://github.com/scottconverse/textflowkit/releases).
