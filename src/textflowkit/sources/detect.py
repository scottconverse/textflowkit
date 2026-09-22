"""Platform detection and URL normalization.

The platform layer has exactly one job: figure out where media lives and how to
fetch it. Transcription never varies by platform.
"""

from __future__ import annotations

import ipaddress
import socket
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
    "medal": ("medal.tv",),
    "loom": ("loom.com",),
    "dropbox": ("dropbox.com", "dropboxusercontent.com"),
}

DIRECT_MEDIA_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".ogg", ".flac")


class UnsafeUrlError(ValueError):
    """Raised when a URL targets a loopback, link-local, or private address.

    This is an SSRF guard, not a content policy: transcription is meant for
    public media, and letting a caller aim the fetcher at the local network or a
    cloud metadata endpoint turns a tool call into a network probe.
    """


BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
    }
)


# Explicitly blocked networks - the real SSRF targets. Deliberately narrower than
# `ipaddress.is_private`, which also covers the RFC 2544 benchmarking range
# (198.18.0.0/15); some local DNS filters resolve public hostnames there, and
# treating that as private would reject legitimate sites.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",          # this-network
        "10.0.0.0/8",         # RFC 1918
        "100.64.0.0/10",      # CGNAT
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local (incl. 169.254.169.254 metadata)
        "172.16.0.0/12",      # RFC 1918
        "192.0.0.0/24",       # IETF protocol assignments
        "192.168.0.0/16",     # RFC 1918
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved
        "::1/128",            # IPv6 loopback
        "fc00::/7",           # IPv6 unique local
        "fe80::/10",          # IPv6 link-local
        "ff00::/8",           # IPv6 multicast
    )
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for addresses that must never be reachable from a user-supplied URL."""
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    return any(ip in net for net in BLOCKED_NETWORKS if net.version == ip.version)


def _host_of(url: str) -> str:
    """Lowercased host from a URL, handling bracketed IPv6 literals.

    `netloc` for `http://[::1]:8080/x` is `[::1]:8080`; naive splitting on ":"
    yields "[", which silently bypasses any IP check.
    """
    netloc = (urlparse(url).netloc or "").lower()
    netloc = netloc.split("@")[-1]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end != -1:
            return netloc[1:end]
    return netloc.split(":")[0]

def assert_url_is_fetchable(url: str) -> None:
    """Reject URLs aimed at local/private infrastructure.

    Blocks by literal IP and by hostname. Hostname blocking resolves first, so a
    name pointing at 127.0.0.1 or 169.254.169.254 is caught, and it also catches
    the well-known metadata hostnames directly.

    Raises `UnsafeUrlError` with an actionable message.
    """
    if not is_url(url):
        return

    host = _host_of(url)
    if not host:
        raise UnsafeUrlError(f"URL has no host: {url}")

    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise UnsafeUrlError(
            f"refusing to fetch a local address ('{host}'). textflowkit only "
            "fetches publicly reachable media URLs."
        )

    # Literal IP? Check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise UnsafeUrlError(
                f"refusing to fetch a non-public IP address ('{host}'). "
                "Loopback, link-local, and private ranges are blocked."
            )
        return

    # Hostname: resolve and check every address it maps to.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable hosts are left to the downloader to report; they are not
        # an SSRF path because they cannot be connected to.
        return

    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise UnsafeUrlError(
                f"refusing to fetch '{host}': it resolves to a non-public address "
                f"({addr}). Loopback, link-local, and private ranges are blocked."
            )

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
    host = host.removeprefix("www.")
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
        assert_url_is_fetchable(value)
        return SourceRef(kind="url", location=value, platform=detect_platform(value))
    p = Path(value).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {value}")
    if not p.is_file():
        raise ValueError(f"not a file: {value}")
    return SourceRef(kind="file", location=str(p.resolve()), platform="local")


