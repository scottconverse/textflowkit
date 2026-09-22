# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately via
GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, or contact the maintainer directly.

Please include: affected version or commit, what you did, what happened, and what
you expected.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (current) | yes |
| < 0.1 | no |

Pre-1.0: only the latest commit on `main` is supported.

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
- **Path traversal via an output directory.** Callers (a CLI user, an HTTP client,
  or a model) can supply an output directory. It is confined to an allowed root only
when one is configured —
  `TEXTFLOWKIT_OUTPUT_ROOT`, defaulting to the current working directory. `..`
  escapes, absolute paths outside the root, and symlinks that escape are rejected.
  See `resolve_output_dir` in `src/textflowkit/core/paths.py`.
  Confined local input is copied from a path-verified open handle into isolated
  scratch before ffmpeg. Output paths are rechecked at file publication. Keep
  the configured roots non-writable by untrusted local users to prevent races.

### Not defended (by design — read before deploying)

- **Unauthenticated developer mode.** Binding beyond loopback is refused unless
  explicitly enabled. The JSON HTTP production profile requires a shared Bearer
  token, roots, durable storage, and limits, but is not a user-account system.
  Put it behind a TLS gateway for remote use. Its rate limiter is per process.
  Streamable-HTTP MCP is separate and not covered by this Bearer middleware.
- **No sandboxing of `ffmpeg` / `yt-dlp`.** They run as your user on input you
  supply. Media is untrusted data; treat a malformed file as you would any
  untrusted input to those tools.
- **Cookies are passed through, not stored.** `--cookies-from-browser` hands
  browser cookies to `yt-dlp` for content you are authorised to access. Using it
  to reach content you are not authorised to access is outside the intended use.
- **Transcript accuracy is not a security property.** Speech-to-text output
  contains errors and must not be relied on for legal, medical, safety-critical,
  or evidentiary purposes without human review.

See also [LEGAL.md](LEGAL.md) for the no-warranty terms.
