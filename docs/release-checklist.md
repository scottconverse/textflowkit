# Release verification checklist

Deterministic GitHub CI and live YouTube evidence are **different gates**. The
live check runs on a Windows maintainer machine, not a GitHub-hosted runner.
The [hosted attempt on 2026-09-23](https://github.com/scottconverse/textflowkit/actions/runs/35805762808)
failed when YouTube required bot confirmation. A local Windows run passed, but
neither result verifies the other 12 recognized sites.

1. Start from a **committed, clean candidate checkout** on Windows. Use a Python
   environment with textflowkit, `yt-dlp`, Whisper, and ffmpeg/ffprobe installed;
   see [install notes](install.md). The script refuses uncommitted changes so
   its receipt identifies the code that ran. It uses Whisper `tiny` on CPU; no
   ROCm GPU setup is required. The script does not supply browser cookies;
   YouTube may still challenge any particular network or run.
2. Run `ruff check .` and `python -m pytest` on the candidate commit. Confirm
   the Windows/Linux/macOS Python matrix and installed-wheel smoke pass in the
   deterministic [CI workflow](../.github/workflows/ci.yml).
3. From the repository root in PowerShell, write a live receipt **outside** the
   checkout. With the documented `.venv` setup:

   ```powershell
   $receipt = Join-Path $env:USERPROFILE ("Documents\textflowkit-youtube-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")
   .\.venv\Scripts\python.exe scripts\smoke_live_youtube.py --receipt $receipt
   if ($LASTEXITCODE -ne 0) { throw "Live YouTube check failed; do not claim a pass" }
   Get-Content -LiteralPath $receipt
   ```

   The script runs the candidate checkout's source through the real CLI, URL
   acquisition, ffmpeg, Whisper, and JSON rendering. It requires
   `platform=youtube`, at least one nonempty
   timestamped segment, **and recognizable spoken content**. By default it
   requires the whole word `elephants`, which the documented default clip is
   observed to say in its first segment with `--model tiny`. Pointing `--url`
   at any other clip requires
   `--expect-text "<a word or short phrase you expect to hear>"`: the run is
   refused, before it touches Git, the network, or a model, because a word from
   the default clip proves nothing about a different one. The expected text is
   matched against the concatenated segment text as whole words; case,
   punctuation, and segment boundaries are ignored, but word boundaries are
   not, so `elephant` does not match `elephants` and neither matches
   `elephantiasis`. This asserts that this one URL yielded speech a human
   recognizes — it is not a transcript-accuracy certification, and it is not
   evidence about the other 12 recognized sites. The receipt records UTC time,
   candidate Git commit, Windows/Python runtime, URL, model/device, the
   expected text and whether it came from the default clip or the caller, the
   assertion's scope and result, segment count, and transcript SHA-256. It does
   **not** record the transcript. Its default timeout is 300 seconds; `--url`,
   `--model`, `--device`, `--expect-text`, and `--timeout` may be overridden
   deliberately and are reflected in the receipt. No success receipt is written
   on failure, including a content mismatch.
4. Keep the receipt with the release evidence, not in the public repository.
   If YouTube blocks access, the video changes, or transcription drifts, record
   the failure and investigate. Do **not** replace a failed live run with a
   green mocked URL test, an old receipt, or the blocked GitHub-hosted attempt.
   A new public fixture URL requires review and an updated script/checklist —
   in particular a new default URL needs a new observed default word, since
   `elephants` belongs to the current one.
   Receipts written before the content assertion existed — the v0.1.5 receipt
   among them — record platform, timestamps, and hashes only: a nonempty timed
   segment was accepted then. Read one as shape evidence for that commit, never
   as evidence of recognized speech, and do not carry it forward as a
   content-verified receipt for a later release. Only receipts that contain
   `content_assertion` make the content claim.
5. After merging, re-run the local check on the **merged, clean `main` commit**;
   a pre-merge branch receipt does not identify the release commit. Only if the
   current live receipt, deterministic merged-main matrix, and release asset
   hashes agree with that commit may release notes claim a
   live YouTube check. State its date/commit/receipt and distinguish this one
   URL from the other 12 recognized platforms. Do not describe GitHub-hosted
   YouTube automation as a passing gate.

The script deletes its temporary media and transcript after producing the
receipt. It does not upload cookies, media, or transcripts to GitHub.

## PyPI publication

After merged-main CI and the local release checks pass, push an annotated final
version tag (for example `v0.1.6` for a future release) on that verified
`main` commit. Do **not**
create or publish a GitHub release manually. The tag push triggers
[`publish-pypi.yml`](../.github/workflows/publish-pypi.yml). It verifies the
tag, the core version, the fonts version contract, and ancestry on `main` with
[`scripts/verify_release_versions.py`](../scripts/verify_release_versions.py),
and it requires both of the
README's release claims — the displayed **Current release** link and the
`## Status` opening claim — to name the version being published (see below); it
then requires a completed, successful `push` run of
[`ci.yml`](../.github/workflows/ci.yml) on `main` whose `head_sha` is the
exact tagged commit, using
[`scripts/verify_release_ci.py`](../scripts/verify_release_ci.py) with the
workflow's `GITHUB_TOKEN` and `actions: read`. A pull-request run, a run for a
different commit, a run still in progress, or a failed run all stop the build
before any distribution is built or uploaded, and an API error or an
unreadable response stops it too. Only then does it build the core
distributions, resolve the fonts package against PyPI (see the fonts version
contract below), publish the projects that need publishing — `textflowkit-fonts`
first, and only when its declared version is new, then `textflowkit` — using
PyPI Trusted Publishing, then hash the downloaded distributions with
[`scripts/write_release_manifest.py`](../scripts/write_release_manifest.py) and
creates a **draft** GitHub release with those exact artifacts plus the resulting
`SHA256SUMS` asset. Only after the assets are attached does it make that release
public. The step refuses to write a manifest unless it finds exactly two wheels
and two sdists, each project's pair at the version expected for that project, so
a missing, duplicated, or stray artifact stops the release instead of publishing
misleading hashes.
`SHA256SUMS` is a release **asset**, a sidecar of the attached files; it is not
a table inside the release notes, which GitHub generates. Only releases published
after this step was added have one: v0.1.5 and earlier releases predate it and
have no manifest to compare.
No upload token is passed to CI. Both PyPI projects must have trusted publishers
for owner `scottconverse`, repository `textflowkit`, workflow
`publish-pypi.yml`, and environment `pypi`. The GitHub `pypi` environment
should require maintainer approval before an upload job can proceed.

### The fonts version contract (issue #15)

The fonts companion package is versioned on its own contract. A core release
that changes nothing in the fonts package has no reason to republish it, so the
fonts package may carry its own version: older than the core version being
released when an earlier fonts publication is reused, or newer when this release
ships a fonts change of its own. `scripts/verify_release_versions.py` requires
the core version to be the release tag and the fonts version to satisfy the
requirement the core `export` extra places on `textflowkit-fonts` — the range
pip actually resolves against.

The build job then resolves the fonts package from the index **before it builds
anything**.
[`scripts/fetch_published_fonts.py`](../scripts/fetch_published_fonts.py) reads
the version declared in `packages/textflowkit-fonts/pyproject.toml` and asks
`https://pypi.org/pypi/textflowkit-fonts/<version>/json`:

- **A 404 for that exact version means it is new.** The release builds the fonts
  wheel and sdist as usual and the `publish-fonts` job uploads them.
- **A published version is reused, never rebuilt.** The workflow downloads the
  two files PyPI recorded — the wheel and the sdist — over HTTPS, verifies each
  against the SHA-256 and the size in PyPI's own response, places them in
  `dist/fonts`, and skips the fonts upload job. The filename already exists on
  PyPI, PyPI rejects a duplicate upload, and this workflow deliberately does not
  hide that with `skip-existing`. The GitHub release therefore still carries the
  four distributions and `SHA256SUMS`, but the fonts files are the published
  bytes rather than a fresh build.

  Skipping the fonts upload does **not** skip the rest of the release. The core
  upload job needs both the build job and the fonts job, and it carries its own
  condition because GitHub skips a job whose `needs:` job was skipped: it
  continues when the fonts job uploaded a new version, or when it was skipped
  and the resolution reported a reuse, and it refuses to continue for a failed
  or cancelled fonts upload, a failed build, a cancelled run, or a skipped
  fonts job that was not a reuse. So a core release can never be published
  against a fonts version that is not on PyPI. The GitHub release job has no
  condition of its own, so a core upload that failed or was skipped leaves the
  release uncreated.
- **Anything else stops the release before it builds or uploads.** A transient
  or 5xx answer, a response that is not JSON or has no file list, a release whose
  files are not exactly one wheel and one sdist for that version (a partial
  publication, a duplicate, an extra file), a file URL that is not HTTPS or does
  not name the file it serves, and a download that does not match the recorded
  digest all fail closed. A partial publication cannot be reused *and* cannot be
  published again, so the version has to be dealt with by hand.

Reuse also requires the tagged commit's fonts package to be the source that
produced those published files. The fetched sdist's payload is compared file by
file with `packages/textflowkit-fonts`, and any difference stops the release: the
checkout would otherwise claim content the published files do not have. A changed
fonts package needs a version bump in
`packages/textflowkit-fonts/pyproject.toml` so the change ships as its own fonts
release — the same rule `tests/test_release_surfaces.py` enforces for a fonts
version a release tag already published. One published member is not compared:
hatchling copies the repository root's `.gitignore` into the sdist root, so the
sdist carries a `.gitignore` the package directory does not have — the published
`textflowkit-fonts` 0.1.5 sdist is one — and that single member is builder
metadata that follows the repository root rather than this package. It is
exempt only while the package has no `.gitignore` of its own; a package
`.gitignore`, or any nested one, is compared like any other file.

The manifest script takes the two versions separately: `--version` pins the two
`textflowkit` artifacts (it is the release tag), and `--fonts-version` pins the
two `textflowkit-fonts` artifacts, defaulting to `--version` when it is not
given. The workflow passes both, reading the fonts version from the tagged
commit, so a reused fonts release is hashed as the bytes it actually published.
A caller that names only `--version` still requires the fonts package to match
it, and a caller that names neither still requires all four artifacts to carry
one version, exactly as before the fonts version could differ. Reuse is opt-in:
`--fonts-version` has to be passed deliberately, and on its own it is refused,
because pinning the fonts artifacts while leaving the core artifacts unpinned
says less than naming neither does.

Reusing an earlier fonts release means reusing the **original published bytes**:
the wheel and sdist already on PyPI, whose digests PyPI recorded. A rebuild is a
different file with different hashes, and PyPI rejects an upload whose filename
already exists, which is why the reuse path downloads and verifies instead of
building.

### The README release-claim guard

A distribution's long description is built from the README **at the tagged
commit** and frozen when PyPI accepts the upload. v0.1.5 was published with a
long description that still said "v0.1.4 release"; PyPI cannot rewrite an
uploaded release's metadata, so that text is immutable and the erratum in the
[user manual](user-manual.md) stands. It cannot be fixed retroactively — only a
later version carries a corrected description.

That bad text is the `## Status` section's opening paragraph
(`git show v0.1.5:README.md`), **not** the displayed release link: the README
makes the same version claim in two places, and they can disagree. Before the
tag is pushed, update both to the new version:

- the one primary
  `**Current release: [vX.Y.Z](https://github.com/scottconverse/textflowkit/releases/tag/vX.Y.Z).**`
  line — its displayed label **and** its target URL; and
- the `## Status` section's opening claim, `**vX.Y.Z release.** ...`, which must
  be that section's first paragraph and must be the only release claim in it.

The publish workflow's build job runs
[`scripts/verify_readme_release.py`](../scripts/verify_readme_release.py) with
the tag before it builds or uploads anything. It fails closed:

- a missing, duplicated, or malformed line or claim stops the release, as does a
  missing or duplicated `## Status` section, an empty one, a claim that is not
  its opening paragraph, and an unreadable README;
- each claim is checked against the version being published on its own. Getting
  the link right while `## Status` still names the old tag is the v0.1.5 defect
  and is a failure, and so is the reverse;
- naming the new version somewhere else in the README is not enough, and it
  never was: `v0.1.5` appears in several places on the page that must not ship.
  The release-surface test in `tests/test_release_surfaces.py` stays green on
  such a page, so this guard — not that substring check — is the gate.

The guard runs only in `publish-pypi.yml`. Ordinary pull-request CI must keep
passing before a release, and until a new version is staged the README
legitimately names the last published release; requiring a matching tag on
every `ci.yml` run would fail every unrelated pull request.

This guard does not verify the published PyPI page. It is a pre-release check on
the tagged commit's README, and v0.1.6 is the first release published with it in
place; the v0.1.5 page keeps the text it was uploaded with.

The automated gate is evidence about the exact tagged commit, and about
`ci.yml` only. It is not evidence about anything else:

- It says nothing about the live YouTube receipt. That remains the separate
  manual Windows gate in steps 1-5; a green hosted run never satisfies it, and
  steps 1-5 never substitute for the CI gate.
- It proves the tagged commit was a green `main` push, not that the tag is the
  current tip of `main`; the workflow still checks ancestry only. Tag the
  verified merged-main commit named by the live receipt.
- A GitHub-hosted run cannot test the other 12 recognized sites, so this gate
  does not extend any platform claim.
- If it stops a release, fix the cause and rerun the workflow; if the tag
  itself is wrong, delete and re-push it on the verified commit rather than
  moving an existing tag.

This is not a transaction across two PyPI projects and GitHub. If fonts upload
succeeds but core fails, **there is no public GitHub release**, but fonts are
already on PyPI. That partial state is recoverable the same way it is reached:
the fonts version is now published, so a rerun of the workflow reuses those two
files instead of building them, and the fonts upload job is skipped — but the
rerun only proceeds if the tagged commit's fonts package still matches the
published sdist, so a fonts change made while repairing the core failure needs a
new fonts version. Check whether any core files reached PyPI before retrying:
PyPI will reject duplicate filenames, and this workflow intentionally does not
silently skip them. Correct the cause and rerun only failed jobs when safe; if
some core files are present, reconcile their hashes against the original build
artifact and finish the missing files deliberately. If both PyPI uploads succeed
but draft creation or publishing fails, do not republish either PyPI project;
complete the GitHub draft and assets from the exact build artifact after hash
verification. Do not move the published version tag or claim a complete release
until all four PyPI files and all four GitHub assets match.

After publication, compare the `SHA256SUMS` release asset against the GitHub
release assets and the PyPI SHA-256 digests for **both** packages, and
clean-install the exact version with
`textflowkit[export,mcp,http]`. Run `doctor`, `selftest`, and an actual PDF export.
An install with `--no-deps` only proves distribution wiring, not transcription.

Never place an API token in this repository, a CI log, or a shell command line.
An upload is a separate public release action; passing CI alone does not
authorize it. Do not call a release complete if either package upload failed.
