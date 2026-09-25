# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately via
GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, or contact the maintainer directly.

Please include: affected version or commit, what you did, what happened, and what
you expected.

## Supported versions

| Version | Security-fix policy |
|---|---|
| Latest published release tag (see [GitHub releases](https://github.com/scottconverse/textflowkit/releases)) | Receives fixes through the next patch release |
| Earlier release tags, including earlier `0.1.x` tags | No backports |
| Unreleased `main` commits | Development only; not a supported release |

For pre-1.0 releases, security fixes land on `main` and are published as a new
release tag rather than backported to older tags. A tag remains the supported
published version when `main` advances; it is superseded when the next release
is published. Fixes and publication are best-effort; no response or patch-time
SLA is promised. See the [GitHub releases](https://github.com/scottconverse/textflowkit/releases)
for the current published tag. “Supported” describes the maintenance policy,
not a claim that a particular version is safe for production deployment.

## Threat model — what this project does and does not defend

`textflowkit` fetches media from the network and runs speech-to-text locally. Its
attack surface is the **input it accepts** and the **files it writes**. The JSON
HTTP adapter has an opt-in production profile with a shared Bearer token, not
user accounts or multi-tenancy.

### In scope (defended)

- **SSRF from a user-supplied URL.** Loopback, link-local (including
  `169.254.169.254`), private, CGNAT, multicast, and reserved ranges are rejected,
  by literal IP, by blocked hostname, and by resolving the hostname and checking
  every address it maps to. See `assert_url_is_fetchable` in
  `src/textflowkit/sources/detect.py`. yt-dlp's selected media/fragment URLs and
  returned redirect URLs are rechecked before/after requests. **These checks do
  not pin DNS at the socket connection.** Production URL input therefore
  requires an operator-provided SSRF-filtering egress proxy; without one URL
  jobs fail closed. Do not treat a generic unrestricted proxy as sufficient.
- **Explicitly configured path confinement.** By default, local inputs and
  explicit output destinations have the access of the account running the tool;
  the current working directory is only the default output destination, **not**
  a security boundary. Set `TEXTFLOWKIT_INPUT_ROOT` and
  `TEXTFLOWKIT_OUTPUT_ROOT` to confine less-trusted callers. With those roots
  set, paths outside them (including `..` and symlink escapes) are rejected.
  See `resolve_output_dir` and `resolve_input_path` in
  `src/textflowkit/core/paths.py`. Output paths are rechecked at file
  publication. Keep configured roots non-writable by untrusted local users to
  prevent races.

- **The decoder boundary for confined input.** A local file can be a
  *reference* to other files rather than media itself: an HLS/M3U playlist, an
  ffmpeg concat script, a DASH manifest. The demuxer opens every path such a
  file names. Confined local input is handle-verified and copied into isolated
  scratch, but only the top-level file is copied, so a playlist inside the root
  can name a file outside it — an absolute reference survives the copy
  unchanged, and a relative one resolves against the scratch directory, where
  `..` walks out. When an input root is set, every confined input is therefore
  decoded under FFmpeg's `-format_whitelist`, listing only the demuxers this
  product supports as *self-contained* starting formats:

      wav, mp3, mov,mp4,m4a,3gp,3g2,mj2, matroska,webm, ogg, flac, aac

  These are FFmpeg's own demuxer names (`ffmpeg -demuxers`), not the extension
  list. A file whose content is not one of them — including a playlist or
  manifest however it is named — is refused by FFmpeg itself, before any
  reference is followed. The same restriction is applied to the `ffprobe`
  duration check, which otherwise opens the staged file the same way.

  This is the guarantee, stated exactly:

  - **Confined local input cannot make FFmpeg open another file.** The demuxer
    that would follow a reference is not selectable, so there is no reference to
    check and none is followed. This is a decoder-format restriction, not a
    filesystem sandbox.
  - **It applies only to confined input** (an input root set) and only to the
    media decode. Without an input root there is no boundary to protect, and the
    documented workflow decodes what FFmpeg already decoded.
  - **It rests on the demuxer list above.** A file that this build classifies as
    one of those demuxers, but which in another FFmpeg build (or through a
    container's own external-data mechanism) opens other files, is not covered.
    The `-format_whitelist` names are checked against `ffmpeg -demuxers`; a
    build whose demuxers differ is not verified here.
  - **It is not a sandbox of FFmpeg or yt-dlp at large** (see "Not defended").

- **Leading-byte manifest refusal (defense in depth).** On top of the decoder
  whitelist, a confined input whose leading bytes are an HLS/M3U playlist
  (`#EXT`), an MPEG-DASH manifest (`<`), or an ffmpeg concat script
  (`ffconcat`) is refused before the staged copy is written, so a recognised
  manifest leaves nothing behind. This is an extra layer, deliberately *not*
  the boundary: it covers only the signatures in its list, and detection is by
  content rather than extension. The decoder whitelist above is what holds if
  this check is widened, narrowed, or removed.

### Not defended (by design — read before deploying)

- **Unauthenticated developer mode.** Binding beyond loopback is refused unless
  explicitly enabled. The JSON HTTP production profile requires a shared Bearer
  token, roots, durable storage, and limits, but is not a user-account system.
  Put it behind a TLS gateway for remote use. Its rate limiter is per process.
  Streamable-HTTP MCP is separate and not covered by this Bearer middleware.
- **No sandboxing of `ffmpeg` / `yt-dlp`.** They run as your user on input you
  supply. Media is untrusted data; treat a malformed file as you would any
  untrusted input to those tools. The confined-input decoder boundary above
  limits which *formats* a confined input may be, and so which files the demuxer
  can open; it does not confine the decoder process, its codecs, or anything
  else on the system.
- **Cookies are passed through, not stored.** `--cookies-from-browser` hands
  browser cookies to `yt-dlp` for content you are authorised to access. Using it
  to reach content you are not authorised to access is outside the intended use.
- **Transcript accuracy is not a security property.** Speech-to-text output
  contains errors and must not be relied on for legal, medical, safety-critical,
  or evidentiary purposes without human review.

See also [LEGAL.md](LEGAL.md) for the no-warranty terms.
