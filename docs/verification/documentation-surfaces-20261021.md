# v0.1.19 documentation and metadata preparation — 2026-10-21

Scope: core version metadata and current README, install/user/integration manuals, architecture, streaming, site/diagram, changelog, roadmap, release checklist and Windows packaging instructions. No production behavior/dependency changes; fonts remain 0.1.6, Whistle remains the default and no-telemetry controls are unchanged. Historical receipts and immutable dependency-source identities are retained.

## Evidence boundaries

The supplied remediation completion report identifies clean baseline `2a5e747d316f93aec8fefed86628a1b369cfa03d`: 2189 passed, 18 skipped, Ruff and a fresh non-editable wheel smoke covering CLI, MCP, HTTP and served UI. Its core 0.1.18 wheel was built at `4dd8bcc`, with only test changes afterward. These are reused results, not checks repeated for this metadata/docs unit or proof of a published v0.1.19 artifact.

The current checklist requires required PR/exact-main CI, tagged core publication/hash verification, an installer built from that immutable tag and exact published wheel, actual installation/upgrade/browser acceptance, saved-data preservation, owner app verification and live-site verification. This dated documentation unit did not perform those delivery steps. Current release/download links use durable labels and direct readers to versioned installer acceptance, candidate identity, checksum and matching-source assets; a link alone does not establish acceptance. The v0.1.18 installer publication receipt remains historical; real screen-reader, physical microphone, new accuracy and live optional-model checks are not claimed.

## Affected local verification

Executed locally:

- `python scripts/verify_release_versions.py --tag v0.1.19`: passed; core 0.1.19, fonts 0.1.6 satisfy the unchanged export requirement.
- `python scripts/verify_readme_release.py --version v0.1.19`: passed.
- The six affected release-surface/version/README/manifest/workflow-layout test files listed in the current checklist: **126 passed**, exit 0; no skips or failures. The quiet progress log contains 126 completed tests.
- `python -m ruff check --no-cache .`: passed.
- `git diff --check`: passed.

Checks used the source candidate with external temp/basetemp and isolated `LOCALAPPDATA`; inherited `TEXTFLOWKIT_*` settings were cleared in the check process. Ruff ran without its cache. No full-suite rerun, install, build, push, merge, tag or publication occurred in this unit. Exact commands/logs and local commit are recorded in the coordinator output `outputs/release-v019-20261021/docs-preparation.md` outside the checkout.
