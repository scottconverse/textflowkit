"""Source layer: platform detection and media acquisition."""

from textflowkit.sources.acquire import AcquisitionError, extract_audio, fetch_media, require_tool
from textflowkit.sources.detect import PLATFORMS, SourceRef, detect_platform, is_url, resolve_source

__all__ = [
    "PLATFORMS",
    "SourceRef",
    "AcquisitionError",
    "detect_platform",
    "is_url",
    "resolve_source",
    "fetch_media",
    "extract_audio",
    "require_tool",
]
