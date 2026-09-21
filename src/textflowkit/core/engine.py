"""Speech-to-text engines.

The default engine is openai-whisper on PyTorch. On this project's reference
hardware (AMD Strix Halo) that means ROCm; on NVIDIA it means CUDA; with neither
it falls back to CPU. The engine interface is intentionally tiny so alternatives
can be added without touching the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from textflowkit.core.model import Segment, Transcript


class Engine(Protocol):
    name: str

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        speaker_labels: bool = False,
    ) -> Transcript: ...


def _pick_device(prefer: str | None = None) -> str:
    if prefer:
        return prefer
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class WhisperEngine:
    """openai-whisper backed engine."""

    name = "openai-whisper"

    def __init__(self, model: str = "small", device: str | None = None, fp16: bool | None = None):
        self.model_name = model
        self.device = _pick_device(device)
        if fp16 is None:
            fp16 = self.device != "cpu"
        self.fp16 = fp16
        self._model: Any = None

    def _load(self):
        if self._model is None:
            try:
                import whisper
            except ImportError as exc:
                raise RuntimeError(
                    "openai-whisper is not installed. Install with: pip install openai-whisper"
                ) from exc
            self._model = whisper.load_model(self.model_name, device=self.device)
        return self._model

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        speaker_labels: bool = False,
    ) -> Transcript:
        model = self._load()
        result = model.transcribe(
            str(audio_path),
            language=language,
            fp16=self.fp16,
            verbose=False,
            word_timestamps=True,
        )

        segments: list[Segment] = []
        for raw in result.get("segments", []) or []:
            text = str(raw.get("text", "")).strip()
            if not text:
                continue
            segments.append(
                Segment(
                    start=float(raw.get("start", 0.0)),
                    end=float(raw.get("end", 0.0)),
                    text=text,
                    speaker=None,
                )
            )

        duration = segments[-1].end if segments else None
        return Transcript(
            source=str(audio_path),
            language=result.get("language"),
            segments=segments,
            duration=duration,
            engine=self.name,
            metadata={"model": self.model_name, "device": self.device},
        )


def get_engine(name: str = "whisper", **kwargs: Any) -> Engine:
    if name in ("whisper", "openai-whisper", "default"):
        return WhisperEngine(**kwargs)
    raise ValueError(f"unknown engine: {name}")
