# Native Windows installer (v0.1.13; public EXE pending)

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
- No background update service, startup-at-login task, public HTTP listener, or
  Docker service is installed.

## Local AI cleanup validation (v0.1.12, carried forward to v0.1.13)

A final cold full-UI run, begun with the local model explicitly unloaded, completed
39 segments in three chunks with four changed segments. The in-app comparison and
corrected TXT save were verified, and the saved file matched the validated variant.
This is evidence of workflow completion for one run, not accuracy; plausible
corrections must be reviewed against audio. Earlier cold failure and a hot zero-edit
run are documented in the user manual. The v0.1.12 installer was built and tested locally; that historical build
evidence is not relabelled for v0.1.13. The installer recipe is published, and a
public EXE is pending issue #31: the coordinator will perform any installer
rebuild. No v0.1.13 installer has been built or tested yet, and no public
installer download link is available.

## Before public distribution

Public EXE distribution remains gated on corresponding-source compliance review
(issue #31, open). The candidate is unsigned;
Windows may show a SmartScreen warning. Do not tell users to disable protections.

Review THIRD-PARTY-NOTICES.txt and the exact source-materials.json provenance.
The LGPL ffmpeg shared build must be accompanied by its required license texts
and corresponding-source availability, including linked dependencies. Preserve
and offer the matching source/build materials with the eventual binary release;
a generic upstream homepage alone is not a complete source offer. Public release
links and version numbers must be updated only when that release is authorized.

The original Python package, CLI, MCP and HTTP entry points remain supported.
This installer does not bundle the optional Whisper or diarization stacks.
