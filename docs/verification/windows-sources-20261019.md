# Windows corresponding-source verification — 2026-10-19

Candidate: TextFlowKit 0.1.13; fonts 0.1.6 unchanged. Whistle remains the default.

## Exact source provenance

- Bundled FFmpeg: n9.0.2-27-g7e93014c5b, BtbN win64 LGPL shared build dated 2026-10-14.
- Build recipes: `63ac3265b0789ea16c7b1f8351cd2ad195a28722`.
- Upstream run: [38562468565](https://github.com/BtbN/FFmpeg-Builds/actions/runs/38562468565), variant job 115848789179.
- Recovered download-cache artifact SHA-256: `732a1b7af931fd1a98172ead14df81e05fd0a524fa06e0d994a57c9b740b709f`.
- Actual bundled `ffmpeg -buildconf` has 43 enabled external `--enable-lib*` flags. Every one maps to an exact upstream build-mounted recipe. No `--enable-gpl` or `--enable-nonfree` is enabled.
- All 86 source stages from that build are present, with matching byte hashes, within the retained 129-archive upstream cache superset. The set includes recursively downloaded library sources and librsvg's vendored Rust sources; the pinned recipe archive preserves patches and build definitions.

## Rust resolution and notices

273 checksum-verified registry crate sources accompany rav1e's original Cargo.lock.
The upstream recipe conservatively updates only `cc`. The original libc 0.2.172
and jobserver 0.1.33 satisfy the requirements across the date-bounded compatible
cc window. This is a dependency-graph inference using documented Cargo behavior,
not a recovered post-build lock or an executed historical resolver result.

The exact historical host compiler-helper version is unknown. A conservative
73-crate source supplement supplies every compatible cc candidate plus shlex 2
and find-msvc-tools candidates. One identical cc archive overlaps the original
set and is stored only once. Original sources and copyright notices are retained;
no attribution is fabricated. r-efi's full license is in AUTHORS. The
simd_helpers host proc-macro has no standalone upstream LICENSE at its recorded
revision; its original MIT metadata and full source are preserved. It is not a
linked runtime library. No blanket legal opinion or bit-for-bit rebuild claim is made.

## Verification and distribution

The coordinator independently read and hash-checked all 2,190 ZIP members:
479 unique source inputs and 1,711 notice files. Zero missing files, CRC errors,
or byte-hash mismatches. All 86 stage hashes match; all 43 enabled libraries map.
Three standalone source ZIPs and the notices ZIP each fit GitHub's asset limit.

The [v0.1.13 release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.13)
contains the source index, three source ZIPs, complete notice ZIP and build
instructions alongside the Windows installer. The installer embeds the small
index, instructions and notice ZIP, not the multi-gigabyte source set. Declared
notice inputs are size/SHA-256 verified; missing or modified inputs fail staging.
The affected installer/payload suite passed 62 tests and Ruff passed.

The v0.1.13 private packaged runtime passed its native Whistle CPU selftest and
a real 30-second transcription (nine segments, recovered exclusion/district
passage). Existing v0.1.12 bootstrap/installation behavior is unchanged and its
acceptance evidence is carried forward. No upgrade of the owner's running app
was performed. Final publication hashes and delivery are checked separately;
this file does not itself constitute an upload receipt. The installer remains
unsigned; users should not disable Windows security protections.
