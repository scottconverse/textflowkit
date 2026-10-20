# Windows installer v0.1.16 — 2026-10-20

Built from immutable tag v0.1.16, commit `442e3cf66643b71ad7bf8e6750f7e406fb58e1ba`, and the exact published core wheel. The existing tagged scripts validated every file in the staged payload and compiled it with Inno Setup 6.7.3. This is not a renamed older EXE.

## Acceptance performed

- Ran the actual EXE into an isolated acceptance directory, not over the owner installation. Setup exited successfully and registered version 0.1.16 at that directory. Original owner install registration was backed up and restored; its app files were not upgraded or restarted.
- Installed private Python and launch.py report 0.1.16.
- Installed launch.py --selftest passed native Whistle CPU compute, bundled real speech and timestamped output.
- Installed launch.py --no-browser served the browser workspace on an isolated loopback port; HTTP 200 and version 0.1.16 verified. Only the owned test server was terminated afterward. This is a launch check, not physical microphone or GUI-click testing.
- No new four-hour meeting accuracy, physical microphone, or existing-user upgrade-path claim. Reuse unaffected engine evidence with its original identity.

## Matching sources, notices and checksums

Pinned dependency binaries and their source/notice assets are byte-identical to the [previous verified source set](windows-sources-20261019.md). The three source ZIPs, full notices ZIP, source index and build instructions retain their v0.1.13 snapshot names/hashes and accompany the v0.1.16 release. These are dependency snapshots, not the installed application version. Prior verification covered 2,190 archive members; it is reused, not represented as a new source rebuild or repeat audit.

The [v0.1.16 release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.16) includes the new EXE, payload manifest, WINDOWS-SHA256SUMS and WINDOWS-README.md. The payload manifest binds core wheel/hash, release commit, exact dependency versions and every payload file. Application source is the immutable v0.1.16 source archive, not the later download-documentation commit. Existing Python package artifacts and release tag were not replaced.

The EXE is unsigned; keep Windows security protections enabled. No warranty is provided.

## No-telemetry and published-artifact acceptance

- Installed privacy environment guards passed; unchanged real dependency/download checks retain their original receipts.
- Clean PyPI `textflowkit[export,mcp,http,streaming]==0.1.16` install passed real Whistle speech transcription and JSON/SRT/TXT/PDF export.
- All four Python artifacts have identical SHA-256 digests on PyPI and GitHub.
- All 2,529 unchanged dependency/bootstrap payload files match the previous verified payload.
- New EXE SHA-256: `5e32ebd1d33ab9207158f95dc373be7a338427027b192710e9cc1c65bc3e9d94` (118,941,184 bytes). See release WINDOWS-SHA256SUMS for every matching source/notice asset.

This receipt is a documentation-only follow-up; it does not replace the immutable release tag, wheel or EXE.
