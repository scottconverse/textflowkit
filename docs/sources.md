# Sources

textflowkit recognises the following platforms. Coverage ultimately depends on
`yt-dlp`; platforms change access rules without notice.

| Platform | Domains |
|---|---|
| YouTube | youtube.com, youtu.be, m.youtube.com, music.youtube.com |
| TikTok | tiktok.com, vm.tiktok.com, vt.tiktok.com |
| Facebook | facebook.com, fb.watch, fb.com, m.facebook.com |
| Instagram | instagram.com, instagr.am |
| Vimeo | vimeo.com, player.vimeo.com |
| Twitch | twitch.tv, clips.twitch.tv |
| Bilibili | bilibili.com, b23.tv |
| Rumble | rumble.com |
| Kick | kick.com |
| Zoom | zoom.us, zoom.com |
| Medal | medal.tv |
| Loom | loom.com |
| Dropbox | dropbox.com, dropboxusercontent.com |

Plus two non-platform inputs:

- **`local`** — any media file on disk
- **`direct`** — a direct media URL (`.mp4`, `.m4a`, `.mp3`, `.wav`, …)

## Adding a platform

Add its domains to `PLATFORMS` in `src/textflowkit/sources/detect.py`. If `yt-dlp`
already supports the site, that is usually the entire change. No pipeline or
renderer code is involved.

## Platform support depends on yt-dlp

Every platform in the table above is fetched through **yt-dlp**. That is a real
dependency on a third party, and it is the one part of this tool that can break
without any change on our side: sites change their media URLs, their access
rules, and their bot defences, and yt-dlp tracks those changes on its own
schedule.

**Posture, stated plainly:**

- **Pin a floor, not a ceiling.** `yt-dlp>=2025.1.1` is a minimum. Upper bounds
  are avoided because the fix for a broken site is usually *newer* yt-dlp, and a
  ceiling would block the fix.
- **Update yt-dlp first** when a site stops working. It is almost always the
  cause, and it is a one-line upgrade rather than a change here.
- **Keep the in-process fallback.** yt-dlp is used as a module when the CLI is
  unavailable or cancellation is wanted, so a missing console script is not a
  hard failure.
- **Run `textflowkit doctor`** before diagnosing anything else. It reports the
  yt-dlp version and how it is being invoked, the detected JavaScript runtime,
  ffmpeg, the optional extras, the compute device, and the configured roots.

```bash
textflowkit doctor
```

YouTube additionally needs a JavaScript runtime for signature/n-param
challenges; the detector enables whichever of deno/node/bun/quickjs is present
(see [install.md](install.md)).

**What is not attempted:** vendoring yt-dlp, or implementing per-site extractors.
Both would be a permanent maintenance burden to duplicate work upstream already
does better.

## URL safety (SSRF guard)

textflowkit **only fetches publicly reachable URLs.** Caller-supplied URLs are
checked before any network access:

- **Blocked hostnames:** `localhost`, `localhost.localdomain`, `ip6-localhost`,
  `metadata`, `metadata.google.internal`, and anything ending in `.localhost`.
- **Blocked networks:** `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`,
  `127.0.0.0/8`, `169.254.0.0/16` (includes `169.254.169.254` cloud metadata),
  `172.16.0.0/12`, `192.0.0.0/24`, `192.168.0.0/16`, `224.0.0.0/4`,
  `240.0.0.0/4`, `::1/128`, `fc00::/7`, `fe80::/10`, `ff00::/8`.
- **Hostnames are resolved**, and every address they map to is checked — so a
  public name pointing at a private address is also rejected. Bracketed IPv6
  literals are parsed correctly.

Unresolvable hostnames are **not** rejected here; the downloader reports them, and
they are not an SSRF path because they cannot be connected to.

This is a safety guard, not a content policy. It exists because an MCP tool or an
HTTP endpoint can be driven by a model or a remote client, and a fetcher aimed at
the local network is a network probe.

**Not blocked:** the RFC 2544 benchmarking range (`198.18.0.0/15`). Python's
`ipaddress` classifies it as private, but some local DNS filters resolve real
public hostnames there, so blocking it would reject legitimate sites.

## Access-controlled content

Some sources require authentication. Use `--cookies-from-browser <browser>` to pass
cookies through to `yt-dlp`.

Only use this for media **you are authorised to access**. See [LEGAL.md](../LEGAL.md).

