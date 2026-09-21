"""Source layer: platform detection and media acquisition."""

from textflowkit.sources.acquire import AcquisitionError, extract_audio, fetch_media, require_tool
from textflowkit.sources.detect import PLATFORMS, SourceRef, detect_platform, is_url, resolve_source

__all__ = [
    "PLATFORMS",
    "AcquisitionError",
    "SourceRef",
    "detect_platform",
    "extract_audio",
    "fetch_media",
    "is_url",
    "require_tool",
    "resolve_source",
]
