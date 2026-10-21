# Native Windows installer (v0.1.18)

This packages the existing local browser interface. End users run the setup EXE;
they do not install Python, ffmpeg, Node, WSL or Docker. Start Menu / optional
desktop shortcuts run a private pythonw.exe and open the loopback-only workspace.
The installer does not replace or register a global Python or modify global PATH.

## Build prerequisites

- Native Windows x86_64, Python 3.13 x64 for the build, and build/hatchling.
- Inno Setup 6 (tested compiler version is recorded in the build receipt).
- HTTPS access when populating the asset/dependency cache. A complete cache can
  be supplied for an offline assembly. End-user file transcription is offline.

All cache, wheel, payload, test and installer output directories must be OUTSIDE
this checkout. Never put pytest basetemp or a packaging checkout inside the tree
being copied. The scripts refuse overlapping source/payload/output locations.

Before assembly, download the three small files declared as redistribution_notices, source_index and build_instructions in source-materials.json into the asset cache. Their exact v0.1.13 URLs, sizes and SHA-256 hashes are pinned there. Staging refuses missing or modified files; it does not download the multi-gigabyte source set into the installer. The source ZIPs remain separate release assets. See [source verification](../../docs/verification/windows-sources-20261019.md).

From a build environment at the repository root, for example:

```powershell
$build = Join-Path $env:TEMP 'textflowkit-windows-build'
python scripts/prepare_windows_payload.py --asset-cache "$build\assets" --out-dir "$build\payload"
python scripts/build_windows_installer.py --payload-dir "$build\payload" --out-dir "$build\installer" --iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

`--wheel-dir` can supply a locally built TextFlowKit wheel. `--dependency-wheels`
can supply the dependency wheelhouse without using an index. The builder uses
only binary dependencies, a private target directory, and the HTTP/export/
streaming extras; Torch, Whisper and pyannote are not included. Runtime assets
are size/SHA-256 checked against assets.lock.json before extraction. The manifest
records dependency versions, asset provenance and the complete payload hashes.
The installer compiler wrapper validates that manifest before compiling.

## Behavior and ownership

- Per-user Windows 10/11 x64 installation; normally no administrator rights.
- Existing Whistle engine/model and streaming DLL are included, not downloaded
  on first file transcription. Streaming remains opt-in, not a new default.
- User jobs/transcripts stay under the existing per-user data locations outside
  the installation directory. Upgrades and uninstall preserve that data.
- Stop the server before upgrade/uninstall. Named presence mutexes prevent file
  replacement while the desktop application is running; no jobs are force-killed.
- The private runtime is isolated from user/global site-packages. Bundled tools
  are added only to the application process PATH.
- The installer recipe removes core `textflowkit-*.dist-info` directories before
  copying the new payload, so upgrades leave one current core distribution.
  Fonts metadata, other dependencies, models and user data are outside this cleanup.
  This source fix does not change the already published v0.1.18 executable.
- No background update service, startup-at-login task, public HTTP listener, or
  Docker service is installed.

## Historical local AI cleanup validation (v0.1.12)

A final cold full-UI run, begun with the local model explicitly unloaded, completed
39 segments in three chunks with four changed segments. The in-app comparison and
corrected TXT save were verified, and the saved file matched the validated variant.
This is evidence of workflow completion for one run, not accuracy; plausible
corrections must be reviewed against audio. Earlier cold failure and a hot zero-edit
run are documented in the user manual. The v0.1.12 installer was built and tested locally; that historical build
evidence is not relabelled for v0.1.13. The v0.1.13 private packaged runtime passed
its native Whistle selftest and a real 30-second transcription. The unchanged
installer bootstrap/upgrade behavior retains its earlier acceptance evidence;
the current build has not been installed over the owner's running application.

## Published v0.1.18 installer acceptance

`TextFlowKit-0.1.18-windows-x64-setup.exe` was built from immutable tag v0.1.18,
commit `d2bbcfcf77d07f7254a6b2f0f4790d67a5857bd5`, and the exact published core
wheel. The publication [WINDOWS-README.md](https://github.com/scottconverse/textflowkit/releases/download/v0.1.18/WINDOWS-README.md)
records actual EXE installation into an isolated directory, installed version
0.1.18, native Whistle timed-speech selftest, privacy guards and browser workspace
launch (HTTP 200 and version 0.1.18). It records no owner binary upgrade for that
release at publication time; it does not attest to a later owner upgrade or
metadata repair. It does not enumerate installed comparison/navigation checks,
so those are not claimed here as newly verified.

The installer, payload manifest, [WINDOWS-SHA256SUMS](https://github.com/scottconverse/textflowkit/releases/download/v0.1.18/WINDOWS-SHA256SUMS)
and matching unchanged source/notice assets accompany the
[v0.1.18 release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.18).
Fonts remain 0.1.6; unchanged dependency source/notice snapshots retain their
original v0.1.13 filenames and hashes. The publication receipt and asset hashes
remain historical evidence; this documentation and recipe fix does not replace them.

## Historical v0.1.16 installer acceptance

The Windows distribution is `TextFlowKit-0.1.16-windows-x64-setup.exe`, built
from the immutable v0.1.16 tag and exact published core wheel while preserving
the verified dependency payload. The actual EXE passed
isolated installation, version readback, native Whistle speech selftest and local
browser UI launch, plus installed privacy guards. Its published build receipt is WINDOWS-README.md, with the full
payload manifest and WINDOWS-SHA256SUMS. Do not count an older installer receipt
as acceptance of this build or upgrade the owner's active installation for a test.

The unchanged dependency sources/notices retain their original v0.1.13 snapshot
filenames and hashes and accompany v0.1.16. The pins in `source-materials.json`
identify dependency snapshots, not the application version; do not relabel them.

## Distribution requirements

Public EXE distribution requires the matching source assets and notices below.
The installer is unsigned;
Windows may show a SmartScreen warning. Do not tell users to disable protections.

Review THIRD-PARTY-NOTICES.txt and the exact source-materials.json provenance.
The LGPL ffmpeg shared build must be accompanied by its required license texts
and corresponding-source availability, including linked dependencies. Preserve
and offer the matching source/build materials with the binary release;
a generic upstream homepage alone is not a complete source offer. Public release
links and version numbers must be updated only when that release is authorized.

The original Python package, CLI, MCP and HTTP entry points remain supported.
This installer does not bundle the optional Whisper or diarization stacks.
