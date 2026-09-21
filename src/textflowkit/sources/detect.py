"""Platform detection and URL normalization.

The platform layer has exactly one job: figure out where media lives and how to
fetch it. Transcription never varies by platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PLATFORMS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"),
    "tiktok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "facebook": ("facebook.com", "fb.watch", "fb.com", "m.facebook.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "vimeo": ("vimeo.com", "player.vimeo.com"),
    "twitch": ("twitch.tv", "clips.twitch.tv"),
    "bilibili": ("bilibili.com", "b23.tv"),
    "rumble": ("rumble.com",),
    "kick": ("kick.com",),
    "zoom": ("zoom.us", "zoom.com"),
    "medal": ("medal.tv", "medal.tv"),
    "loom": ("loom.com",),
    "dropbox": ("dropbox.com", "dropboxusercontent.com"),
}

DIRECT_MEDIA_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".ogg", ".flac")


@dataclass(slots=True)
class SourceRef:
    """A resolved input: either a local file or a remote URL."""

    kind: str          # "file" | "url"
    location: str      # absolute path or URL
    platform: str      # e.g. "youtube", "local", "direct"


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def detect_platform(value: str) -> str:
    """Identify the platform for a URL, or 'local' for a filesystem path."""
    if not is_url(value):
        return "local"
    host = (urlparse(value).netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for platform, domains in PLATFORMS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return platform
    if host and host.split("/")[-1].lower().endswith(DIRECT_MEDIA_SUFFIXES):
        return "direct"
    path = urlparse(value).path.lower()
    if path.endswith(DIRECT_MEDIA_SUFFIXES):
        return "direct"
    return "direct"


def resolve_source(value: str) -> SourceRef:
    """Turn user input into a SourceRef, validating local paths."""
    if is_url(value):
        return SourceRef(kind="url", location=value, platform=detect_platform(value))
    p = Path(value).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {value}")
    if not p.is_file():
        raise ValueError(f"not a file: {value}")
    return SourceRef(kind="file", location=str(p.resolve()), platform="local")
