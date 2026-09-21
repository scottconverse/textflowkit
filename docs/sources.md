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

## Access-controlled content

Some sources require authentication. Use `--cookies-from-browser <browser>` to pass
cookies through to `yt-dlp`.

Only use this for media **you are authorised to access**. See [LEGAL.md](../LEGAL.md).
