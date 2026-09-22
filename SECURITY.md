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
attack surface is the **input it accepts** and the **files it writes**, not a
hosted service. There is no server-side multi-tenancy and no user account system.

### In scope (defended)

- **SSRF from a user-supplied URL.** Loopback, link-local (including
  `169.254.169.254`), private, CGNAT, multicast, and reserved ranges are rejected,
  by literal IP, by blocked hostname, and by resolving the hostname and checking
  every address it maps to. See `assert_url_is_fetchable` in
  `src/textflowkit/sources/detect.py`.
- **Path traversal via an output directory.** Callers (a CLI user, an HTTP client,
  or a model) can supply an output directory. It is confined to an allowed root only
when one is configured —
  `TEXTFLOWKIT_OUTPUT_ROOT`, defaulting to the current working directory. `..`
  escapes, absolute paths outside the root, and symlinks that escape are rejected.
  See `resolve_output_dir` in `src/textflowkit/core/paths.py`.

### Not defended (by design — read before deploying)

- **No authentication on the HTTP adapter.** `textflowkit-http` ships no auth.
  Auth belongs to the deployment, not to a transcript library, so this is not
  "fixed" by adding a half-built login.
  What *is* guarded: binding beyond loopback is **refused at startup** unless you
  pass `--allow-remote` or set `TEXTFLOWKIT_ALLOW_REMOTE=1`. The failure mode is a
  clear error rather than a silently exposed service. If you enable it, put your
  own gateway and auth in front.
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
