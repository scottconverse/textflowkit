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


def engine_model_names(name: str) -> tuple[str, ...]:
    """The model names the selected engine will accept, in preference order.

    Read from the engine's own published list rather than copied here, so a
    version that adds or renames a size cannot drift from what a submission
    claims it will run. The engine is imported only for the default engine, and
    only this far: a name list is metadata, not weights, and no loader is called.
    """
    canonical = validate_engine(name)
    if canonical == "whisper":
        try:
            import whisper
        except ImportError as exc:
            raise RuntimeError(
                "openai-whisper is not installed. Install with: pip install openai-whisper"
            ) from exc
        return tuple(whisper.available_models())
    return _FASTER_WHISPER_MODELS


def validate_model(name: str, engine: str = "whisper") -> str:
    """Check a model name against what the selected engine publishes.

    A *name* is what this contract runs; a *path* to a checkpoint - a directory
    or a file, whether or not it exists here - is refused rather than stat-ed.
    A model that happens to be named the same as a file in the working directory
    is still just a name, so nothing here looks at the filesystem.

    Cheap: one import of the optional package's name list at most, never a load,
    so a typo is refused on every surface before a job record or queue slot
    exists. Callers that only want a name checked can ignore the return value.
    """
    text = str(name)
    if not text:
        raise ValueError("model is required")
    if Path(text).is_absolute() or "/" in text or "\\" in text:
        raise ValueError(
            f"model '{text}' is a path; pass a model name instead "
            "(openai-whisper publishes its own list)"
        )
    available = engine_model_names(engine)
    if text not in available:
        raise ValueError(
            f"unknown model '{text}' for engine '{validate_engine(engine)}'; "
            f"choose from {', '.join(available)}"
        )
    return text


#: faster-whisper's supported size names. Kept as one literal so a submission can
#: validate a name on a box where the optional extra is *not installed* - the
#: engine name is already settled by `validate_engine` on every box, and a typo
#: in the *engine's own* model names is a request error rather than a
#: missing-package failure. CTranslate2 model conversions use the same size
#: names as upstream Whisper; anything else is a local directory a caller
#: passes, which this contract refuses in favour of an explicit model.
_FASTER_WHISPER_MODELS: tuple[str, ...] = (
    "tiny.en", "tiny",
    "base.en", "base",
    "small.en", "small",
    "medium.en", "medium",
    "large-v1", "large-v2", "large-v3", "large",
    "large-v3-turbo", "turbo",
    "distil-large-v3", "distil-medium.en", "distil-small.en",
)


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


def require_engine(name: str) -> str:
    """Validate the name and require an optional engine's package to be importable.

    The same checks as :func:`ensure_engine_available`, except that a missing
    extra is reported as ``ValueError`` rather than ``RuntimeError``. Every
    surface already maps ``ValueError`` onto its own refusal - a CLI message and
    exit code, an MCP ``{"error": ...}``, an HTTP 422 - so the install line
    travels through the contract those surfaces already have, instead of a fifth
    error type that each of them would have to learn to catch separately.
    """
    try:
        return ensure_engine_available(name)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


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
