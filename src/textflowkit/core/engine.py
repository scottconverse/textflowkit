"""Speech-to-text engines.

The default engine is openai-whisper on PyTorch. On this project's reference
hardware (AMD Strix Halo) that means ROCm; on NVIDIA it means CUDA; with neither
it falls back to CPU. The engine interface is intentionally tiny so alternatives
can be added without touching the pipeline.

`faster-whisper` (CTranslate2) is an **opt-in** second engine for the machines
openai-whisper serves worst: Apple Silicon and CPU-only boxes. It is deliberately
not a ROCm replacement - CTranslate2's GPU path is CUDA, and on an AMD Windows
box it would have to be compiled from source with `-DWITH_HIP=ON`. So this engine
never guesses: with no device, or `cpu`, it runs CPU `int8`, and any other
device string is passed to upstream unchanged rather than being quietly rewritten
into something else. The default engine is untouched by any of this.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from textflowkit.core.model import Segment, Transcript, WordTiming

#: Engines a caller may name. Aliases below resolve to these.
ENGINE_CHOICES = ("whisper", "faster-whisper")

_ENGINE_ALIASES = {
    "whisper": "whisper",
    "openai-whisper": "whisper",
    "default": "whisper",
    "faster-whisper": "faster-whisper",
}

FASTER_WHISPER_EXTRA = "faster-whisper"

FASTER_WHISPER_MISSING = (
    "faster-whisper is not installed. It is an optional extra: install it with "
    "'pip install \"textflowkit[faster-whisper]\"', or directly with "
    "'pip install faster-whisper'. The default engine (openai-whisper) needs no extra."
)


def validate_engine(name: str) -> str:
    """Canonical engine name, or ``ValueError`` for one we do not have.

    Cheap and import-free, so a caller can reject a typo before paying for
    acquisition, a job record, or a model load.
    """
    canonical = _ENGINE_ALIASES.get(name)
    if canonical is None:
        raise ValueError(
            f"unknown engine '{name}'; choose from {', '.join(ENGINE_CHOICES)}"
        )
    return canonical


def ensure_engine_available(name: str) -> str:
    """Validate the name and check an optional engine's package is importable.

    Only the optional engine is import-checked; the default engine's import is
    left to its lazy loader so naming it costs nothing. A missing extra raises
    with the install line rather than a bare ImportError, so the caller can stop
    before acquiring media or loading anything.
    """
    canonical = validate_engine(name)
    if canonical == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(FASTER_WHISPER_MISSING) from exc
    return canonical


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


class FasterWhisperEngine:
    """CTranslate2-backed engine, opt-in for CPU and Apple Silicon.

    Two upstream behaviours drive the shape of this class:

    - `WhisperModel.transcribe` returns a **generator**, and inference happens
      while it is iterated. Iterating it after releasing the lock would let a
      second job run inference on the same model concurrently, which is exactly
      what the lock in `WhisperEngine` exists to prevent. The generator is
      therefore drained inside the lock.
    - Its decoding defaults are not openai-whisper's. Nothing here claims the
      two produce the same text, and no speed or accuracy number is asserted
      anywhere: the only honest claim is that the engine is wired up.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str = "small",
        device: str | None = None,
        compute_type: str | None = None,
    ):
        self.model_name = model
        # No device, or "cpu", means CPU int8 - the CPU/Mac case this engine
        # exists for. Anything else is the caller's explicit choice and is passed
        # to upstream as-is: on this project's AMD reference box `--device cuda`
        # reaches CTranslate2's CUDA path and fails there, loudly, instead of
        # being silently turned into a CPU run the caller did not ask for.
        self.device = device or "cpu"
        self.compute_type = compute_type or ("int8" if self.device == "cpu" else "float16")
        self._model: Any = None
        self._lock = threading.RLock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise RuntimeError(FASTER_WHISPER_MISSING) from exc
                try:
                    self._model = WhisperModel(
                        self.model_name, device=self.device, compute_type=self.compute_type
                    )
                except Exception as exc:  # upstream raises several types
                    raise RuntimeError(
                        f"faster-whisper could not load model '{self.model_name}' on "
                        f"device '{self.device}' with compute_type '{self.compute_type}': {exc}"
                    ) from exc
            return self._model

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
    ) -> Transcript:
        with self._lock:
            model = self._load()
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=language,
                word_timestamps=True,
            )
            # Inference runs as this generator is consumed, so it is drained
            # here, inside the lock, rather than returned or iterated later.
            drained = list(raw_segments)
            detected = getattr(info, "language", None)

        segments: list[Segment] = []
        for raw in drained:
            text = str(getattr(raw, "text", "") or "").strip()
            if not text:
                continue
            words: list[WordTiming] = []
            for word in getattr(raw, "words", None) or []:
                word_text = getattr(word, "word", None)
                start = getattr(word, "start", None)
                end = getattr(word, "end", None)
                if not word_text or start is None or end is None:
                    continue
                words.append(
                    WordTiming(start=float(start), end=float(end), text=str(word_text).strip())
                )
            segments.append(
                Segment(
                    start=float(getattr(raw, "start", 0.0)),
                    end=float(getattr(raw, "end", 0.0)),
                    text=text,
                    speaker=None,
                    words=words,
                )
            )

        return Transcript(
            source=str(audio_path),
            language=detected,
            segments=segments,
            duration=None,  # Speech spans are not the length of the source audio.
            engine=self.name,
            metadata={
                "model": self.model_name,
                "device": self.device,
                "compute_type": self.compute_type,
            },
        )


_ENGINE_CACHE_LOCK = threading.Lock()


@lru_cache(maxsize=8)
def _cached_whisper(model: str, device: str | None, fp16: bool | None) -> WhisperEngine:
    return WhisperEngine(model=model, device=device, fp16=fp16)


@lru_cache(maxsize=8)
def _cached_faster_whisper(
    model: str, device: str | None, compute_type: str | None
) -> FasterWhisperEngine:
    return FasterWhisperEngine(model=model, device=device, compute_type=compute_type)


def get_engine(name: str = "whisper", **kwargs: Any) -> Engine:
    """Resolve an engine by name, reusing one loaded model per configuration.

    The cache is what keeps a batch from loading one model per item; it is
    bounded and guarded by one lock, so concurrent callers share the instance
    instead of racing to build two.
    """
    if name in ("whisper", "openai-whisper", "default"):
        model = kwargs.pop("model", "small")
        device = kwargs.pop("device", None)
        fp16 = kwargs.pop("fp16", None)
        if kwargs:
            raise TypeError(f"unknown Whisper options: {', '.join(kwargs)}")
        with _ENGINE_CACHE_LOCK:
            return _cached_whisper(model, device, fp16)
    if name == "faster-whisper":
        model = kwargs.pop("model", "small")
        device = kwargs.pop("device", None)
        compute_type = kwargs.pop("compute_type", None)
        if kwargs:
            raise TypeError(f"unknown faster-whisper options: {', '.join(kwargs)}")
        with _ENGINE_CACHE_LOCK:
            return _cached_faster_whisper(model, device, compute_type)
    raise ValueError(
        f"unknown engine '{name}'; choose from {', '.join(ENGINE_CHOICES)}"
    )
