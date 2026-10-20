# Documentation consistency check — 2026-10-20

Scope: current v0.1.15 interfaces and published Windows installer. This is a
reference/contract check, not a new media accuracy test or a new binary release.

## Surfaces checked

README, user and developer/integration manuals, install/source/architecture/
streaming guides, website HTML and four architecture diagrams, release and
contribution instructions, security/legal guidance, Windows packaging guide,
fonts/fixture documentation and runtime UI help assets were inventoried.
Historical release notes and verification receipts remain historical.

Against the actual CLI parser, public Python signatures, HTTP OpenAPI schema,
MCP decorators/signatures, source registry, environment-setting names and
package metadata, the check covered:

- 31 existing tracked Markdown/HTML/SVG documentation files (before this record).
- 161 local/repository-main links and section references.
- 34 CLI examples parsed without running their media jobs.
- Five Python examples parsed; `transcribe` keyword arguments checked against
  its real signature, not executed as media jobs.
- All ten current MCP tool names; HTTP methods/routes and submission fields.
- Core version 0.1.15 and independently versioned fonts 0.1.6.

The corrected inventory reported no unresolved link, parser, keyword or
unknown-environment-variable findings. This is scoped mechanical coverage,
not a proof that every prose statement or external service is always correct.

## Corrections

- Architecture and packaging guides now describe the published v0.1.15 installer
  and link its actual isolated-install acceptance record.
- HTTP PowerShell submission uses default Whistle instead of naming `small`
  without selecting Whisper; it polls to completion and reports terminal errors.
- GPU selftest examples explicitly select `--engine whisper`, not CPU Whistle.
- Re-export references use `POST /jobs/{id}/export`, not `POST /export`.
- Broken Whistle section links point to section 8; README verification links
  are absolute for use outside GitHub.
- The overview diagram and accessible descriptions include faster-whisper;
  current streaming labels and cleanup availability reference v0.1.15 while
  preserving their introduction dates and historical validation results.
- Source hostname exclusions include `ip6-loopback`; fixture documentation
  states the actual known-word selftest condition.
- Contributor engine guidance distinguishes default Whistle from optional
  PyTorch/openai-whisper.

## Publication boundaries

No tag, Python artifact, installer, dependency source snapshot or checksum is
rewritten by this documentation follow-up. PyPI's README and the manuals inside
already downloaded artifacts are immutable release snapshots, not live copies
of `main`. Use the current [website](https://www.textflowkit.org/),
[user manual](../user-manual.md), [integration manual](../adapters.md) and
[GitHub release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.15)
for corrected current instructions. Original v0.1.13 dependency-source filenames
remain correct for the unchanged dependencies bundled with installer v0.1.15.

Whistle remains the default. No new provider/harness, physical microphone,
container, GPU or four-hour accuracy verification is claimed here.
