# Windows installer v0.1.15 — 2026-10-20

Built from immutable tag v0.1.15, commit `fd8a6fab314e1545a16456d2565ecdede3812e83`, and the exact published core wheel. The existing tagged scripts validated every file in the staged payload and compiled it with Inno Setup 6.7.3. This is not a renamed older EXE.

## Acceptance performed

- Ran the actual EXE into an isolated acceptance directory, not over the owner installation. Setup exited successfully and registered version 0.1.15 at that directory. Original owner install registration was backed up and restored; its app files were not upgraded or restarted.
- Installed private Python and launch.py report 0.1.15.
- Installed launch.py --selftest passed native Whistle CPU compute, bundled real speech and timestamped output.
- Installed launch.py --no-browser served the browser workspace on an isolated loopback port; HTTP 200 and version 0.1.15 verified. Only the owned test server was terminated afterward. This is a launch check, not physical microphone or GUI-click testing.
- No new four-hour meeting accuracy, physical microphone, or existing-user upgrade-path claim. Reuse unaffected engine evidence with its original identity.

## Matching sources, notices and checksums

Pinned dependency binaries and their source/notice assets are byte-identical to the [previous verified source set](windows-sources-20261019.md). The three source ZIPs, full notices ZIP, source index and build instructions retain their v0.1.13 snapshot names/hashes and accompany the v0.1.15 release. These are dependency snapshots, not the installed application version. Prior verification covered 2,190 archive members; it is reused, not represented as a new source rebuild or repeat audit.

The [v0.1.15 release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.15) includes the new EXE, payload manifest, WINDOWS-SHA256SUMS and WINDOWS-README.md. The payload manifest binds core wheel/hash, release commit, exact dependency versions and every payload file. Application source is the immutable v0.1.15 source archive, not the later download-documentation commit. Existing Python package artifacts and release tag were not replaced.

The EXE is unsigned; keep Windows security protections enabled. No warranty is provided.
