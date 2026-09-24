"""Platform detection and source resolution."""

from __future__ import annotations

from textflowkit.sources.detect import detect_platform, is_url, resolve_source


def test_url_detection():
    assert is_url("https://example.com/a")
    assert not is_url("C:/tmp/a.mp4")
    assert not is_url("just-text")


def test_platform_mapping():
    cases = {
        "https://www.youtube.com/watch?v=abc": "youtube",
        "https://youtu.be/abc": "youtube",
        "https://www.tiktok.com/@a/video/1": "tiktok",
        "https://www.facebook.com/reel/123": "facebook",
        "https://fb.watch/abc/": "facebook",
        "https://www.instagram.com/reel/abc/": "instagram",
        "https://vimeo.com/12345": "vimeo",
        "https://www.twitch.tv/videos/1": "twitch",
        "https://www.bilibili.com/video/BV1": "bilibili",
        "https://rumble.com/v1": "rumble",
        "https://kick.com/video/1": "kick",
        "https://zoom.us/rec/play/1": "zoom",
        "https://medal.tv/games/1": "medal",
        "https://www.loom.com/share/1": "loom",
        "https://www.dropbox.com/s/abc/v.mp4": "dropbox",
    }
    for url, expected in cases.items():
        assert detect_platform(url) == expected, url


def test_subdomain_matching():
    assert detect_platform("https://m.youtube.com/watch?v=1") == "youtube"


def test_direct_media_url():
    assert detect_platform("https://cdn.example.com/video.mp4") == "direct"


def test_local_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    ref = resolve_source(str(f))
    assert ref.kind == "file"
    assert ref.platform == "local"


def test_missing_file_raises():
    try:
        resolve_source("C:/definitely/not/here.mp4")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


# --- SSRF guard -----------------------------------------------------------

BLOCKED_URLS = [
    "http://127.0.0.1:8080/secret.mp4",              # loopback
    "http://169.254.169.254/latest/meta-data/",      # link-local / cloud metadata
    "http://localhost/admin",                        # loopback by name
    "http://localhost.localdomain/x",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://10.0.0.5/media.mp4",                     # RFC 1918
    "http://192.168.1.10/v.mp4",                     # RFC 1918
    "http://172.16.0.1/v.mp4",                       # RFC 1918
    "http://100.64.0.1/x",                           # CGNAT
    "http://0.0.0.0/v.mp4",                          # this-network
    "http://[::1]/v.mp4",                            # IPv6 loopback
    "http://[::1]:8080/x",                           # IPv6 loopback with port
    "http://[fe80::1]/x",                            # IPv6 link-local
]


def test_ssrf_guard_blocks_local_and_private_targets():
    from textflowkit.sources.detect import UnsafeUrlError

    for url in BLOCKED_URLS:
        try:
            resolve_source(url)
        except UnsafeUrlError:
            continue
        raise AssertionError(f"SSRF guard failed to block: {url}")


def test_bracketed_ipv6_is_parsed_not_bypassed():
    """A naive netloc split on ':' yields '[' for [::1] and skips the IP check."""
    from textflowkit.sources.detect import _host_of

    assert _host_of("http://[::1]:8080/x") == "::1"
    assert _host_of("http://[fe80::1]/x") == "fe80::1"
    assert _host_of("http://user:pw@[::1]:8080/x") == "::1"
    assert _host_of("https://example.com:443/x") == "example.com"


def test_ssrf_guard_allows_public_and_unresolvable_hosts():
    assert resolve_source("https://cdn.example.com/video.mp4").platform == "direct"
    assert resolve_source("https://vimeo.com/12345").platform == "vimeo"
    # Unresolvable hosts are not an SSRF path; the downloader reports them.
    assert resolve_source("https://unresolvable-xyzzy-12345.example/v.mp4").platform == "direct"


def test_blocked_ip_classification_directly():
    import ipaddress

    from textflowkit.sources.detect import _is_blocked_ip

    for addr in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.0.1", "::1", "fe80::1", "0.0.0.0"):
        assert _is_blocked_ip(ipaddress.ip_address(addr)), addr
    for addr in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert not _is_blocked_ip(ipaddress.ip_address(addr)), addr


def test_benchmarking_range_not_treated_as_private():
    """198.18.0.0/15 (RFC 2544) is classified private by Python but appears in
    local DNS filtering for real hostnames. Blocking it would reject valid sites."""
    import ipaddress

    from textflowkit.sources.detect import _is_blocked_ip

    assert not _is_blocked_ip(ipaddress.ip_address("198.18.2.214"))


# --- SSRF guard: IPv4-mapped IPv6 literals (::ffff:0:0/96) -----------------
#
# ::ffff:10.0.0.1 and 10.0.0.1 name the same destination: the IPv4 stack maps
# the former onto the latter. A blocklist that only compares same-version
# networks classifies the mapped form as public and lets it through.

MAPPED_BLOCKED_URLS = [
    "http://[::ffff:10.0.0.1]/v.mp4",                     # RFC 1918
    "http://[::ffff:172.16.0.1]/v.mp4",                   # RFC 1918
    "http://[::ffff:192.168.1.10]/v.mp4",                 # RFC 1918
    "http://[::ffff:100.64.0.1]/x",                       # CGNAT
    "http://[::ffff:169.254.169.254]/latest/meta-data/",  # link-local metadata
    "http://[::ffff:127.0.0.1]:8080/x",                   # loopback
    "http://[::ffff:0.0.0.1]/x",                          # this-network
    "http://[::ffff:192.0.0.1]/x",                        # IETF protocol assignments
    "http://[::ffff:240.0.0.1]/x",                        # reserved
]

MAPPED_BLOCKED_ADDRS = [url.split("[")[1].split("]")[0] for url in MAPPED_BLOCKED_URLS]


def test_ssrf_guard_blocks_ipv4_mapped_ipv6_literals():
    """A bracketed IPv4-mapped literal must be judged by the IPv4 policy."""
    from textflowkit.sources.detect import UnsafeUrlError

    for url in MAPPED_BLOCKED_URLS:
        try:
            resolve_source(url)
        except UnsafeUrlError:
            continue
        raise AssertionError(f"SSRF guard failed to block IPv4-mapped URL: {url}")


def test_mapped_ip_classification_directly():
    import ipaddress

    from textflowkit.sources.detect import _is_blocked_ip

    for addr in MAPPED_BLOCKED_ADDRS:
        assert _is_blocked_ip(ipaddress.ip_address(addr)), addr


def test_ssrf_guard_blocks_hostname_resolving_to_mapped_private_ip(monkeypatch):
    """The resolved-address path shares the classifier, so a hostname whose
    record is ::ffff:10.0.0.1 is the same bypass by another door.
    getaddrinfo is stubbed; no real name is resolved and no host is contacted."""
    import socket as _socket

    from textflowkit.sources import detect
    from textflowkit.sources.detect import UnsafeUrlError

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(_socket.AF_INET6, _socket.SOCK_STREAM, 6, "", ("::ffff:10.0.0.1", 0, 0, 0))]

    monkeypatch.setattr(detect.socket, "getaddrinfo", fake_getaddrinfo)
    try:
        resolve_source("https://mapped.internal.example/v.mp4")
    except UnsafeUrlError:
        return
    raise AssertionError("SSRF guard failed to block a host resolving to ::ffff:10.0.0.1")


def test_ssrf_guard_still_allows_public_mapped_addresses(monkeypatch):
    """Closing the bypass must not turn into 'block every ::ffff: address':
    a mapped public address stays reachable, and the RFC 2544 range the policy
    deliberately permits is still permitted once mapped."""
    import ipaddress
    import socket as _socket

    from textflowkit.sources import detect
    from textflowkit.sources.detect import _is_blocked_ip

    assert not _is_blocked_ip(ipaddress.ip_address("::ffff:8.8.8.8"))
    assert not _is_blocked_ip(ipaddress.ip_address("::ffff:198.18.0.1"))
    # Literal form needs no DNS.
    assert resolve_source("https://[::ffff:8.8.8.8]/v.mp4").kind == "url"

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(_socket.AF_INET6, _socket.SOCK_STREAM, 6, "", ("::ffff:8.8.8.8", 0, 0, 0))]

    monkeypatch.setattr(detect.socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_source("https://mapped.public.example/v.mp4").kind == "url"
