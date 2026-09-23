"""The one layout for temporary source media and decoded audio.

Never derive decoded output from the media basename: a staged WAV and its
decoded WAV would otherwise be the same path. Every pipeline attempt gets its
own temporary root, and both normal and diarization-reacquisition paths use
this layout through the acquisition helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ScratchPaths:
    root: Path

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    def staged_local(self, suffix: str) -> Path:
        return self.media_dir / f"input{suffix.lower()}"

    @property
    def download_template(self) -> str:
        return str(self.media_dir / "%(id)s.%(ext)s")

    @property
    def decoded_audio(self) -> Path:
        return self.root / "audio" / "decoded.wav"
