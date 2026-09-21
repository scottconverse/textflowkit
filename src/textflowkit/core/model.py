"""Canonical transcript data model.

Every source, engine, and renderer speaks this shape. Keeping one canonical
object is what lets the pipeline stay shared while platforms multiply.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Segment:
    """One timestamped span of speech."""

    start: float
    end: float
    text: str
    speaker: str | None = None
    translated_text: str | None = None
    hidden: bool = False

    def display_text(self) -> str:
        """Text to render: translated if present, else source."""
        return self.translated_text or self.text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
            speaker=data.get("speaker"),
            translated_text=data.get("translated_text"),
            hidden=bool(data.get("hidden", False)),
        )


@dataclass(slots=True)
class Transcript:
    """A complete transcription result."""

    source: str
    language: str | None = None
    segments: list[Segment] = field(default_factory=list)
    platform: str | None = None
    duration: float | None = None
    engine: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(s.display_text().strip() for s in self.segments if not s.hidden)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "language": self.language,
            "platform": self.platform,
            "duration": self.duration,
            "engine": self.engine,
            "metadata": self.metadata,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            source=data.get("source", ""),
            language=data.get("language"),
            platform=data.get("platform"),
            duration=data.get("duration"),
            engine=data.get("engine"),
            metadata=data.get("metadata", {}) or {},
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> Transcript:
        return cls.from_dict(json.loads(raw))

    def save_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load_json(cls, path: str | Path) -> Transcript:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
