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
