# Changelog

## Unreleased

Accepted audit fixes. This is a **local candidate branch**
(`deepseek/fix-audit-20261001`) that has **not been merged anywhere** — not to
`main`, not to any release branch, and not published. It is unreleased work, not
a published release: it carries no release date, no tag, and no new version — the
current version stays 0.1.6. Nothing here claims a public release, an
installed-package receipt, a runtime-harness proof, or that any of it is
available from an index.

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
  **per item**, MCP batch options are shared and expose **no**
  `cookies_from_browser` parameter), and the batch contract now states that 202
  means admission, not completion, with per-item indexed errors. MCP
  `sources` is documented as a list of **strings**: a non-string entry (including
  a nested object or a number) is that entry's own error, not a whole-call
  refusal, while a string remains valid.
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

This changelog starts with unreleased work; published historical release notes
remain on [GitHub Releases](https://github.com/scottconverse/textflowkit/releases).
