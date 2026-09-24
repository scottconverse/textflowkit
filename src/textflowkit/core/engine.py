"""Speech-to-text engines.

The default engine is openai-whisper on PyTorch. On this project's reference
hardware (AMD Strix Halo) that means ROCm; on NVIDIA it means CUDA; with neither
it falls back to CPU. The engine interface is intentionally tiny so alternatives
can be added without touching the pipeline.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from textflowkit.core.model import Segment, Transcript, WordTiming


class Engine(Protocol):
    name: str

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
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
        self._lock = threading.RLock()

    def _load(self):
        with self._lock:
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
    ) -> Transcript:
        with self._lock:
            model = self._load()
            # verbose=None, not False: openai-whisper's verbose is tri-state and
            # False is its *chatty* mode (it enables the tqdm frame bar and
            # prints the detected language), which would leak into the streams
            # this shared core serves to the CLI, MCP, and HTTP adapters. None
            # disables the bar, the language line, and per-segment text.
            result = model.transcribe(
                str(audio_path),
                language=language,
                fp16=self.fp16,
                verbose=None,
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
                    words=[
                        WordTiming(
                            start=float(word["start"]),
                            end=float(word["end"]),
                            text=str(word["word"]).strip(),
                        )
                        for word in (raw.get("words") or [])
                        if isinstance(word, dict) and word.get("word")
                        and word.get("start") is not None and word.get("end") is not None
                    ],
                )
            )

        return Transcript(
            source=str(audio_path),
            language=result.get("language"),
            segments=segments,
            duration=None,  # Speech spans are not the length of the source audio.
            engine=self.name,
            metadata={"model": self.model_name, "device": self.device},
        )


_ENGINE_CACHE_LOCK = threading.Lock()


@lru_cache(maxsize=8)
def _cached_whisper(model: str, device: str | None, fp16: bool | None) -> WhisperEngine:
    return WhisperEngine(model=model, device=device, fp16=fp16)


def get_engine(name: str = "whisper", **kwargs: Any) -> Engine:
    if name in ("whisper", "openai-whisper", "default"):
        model = kwargs.pop("model", "small")
        device = kwargs.pop("device", None)
        fp16 = kwargs.pop("fp16", None)
        if kwargs:
            raise TypeError(f"unknown Whisper options: {', '.join(kwargs)}")
        with _ENGINE_CACHE_LOCK:
            return _cached_whisper(model, device, fp16)
    raise ValueError(f"unknown engine: {name}")
