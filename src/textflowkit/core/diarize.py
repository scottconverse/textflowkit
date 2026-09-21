"""Speaker diarization.

The `Segment.speaker` field existed before anything could fill it, which is the
same "surface with no implementation" defect the audit caught on
`--speaker-labels`. This module fills it - and refuses to pretend:

- diarization is **opt-in**
- if the backend is missing or unconfigured, the job **fails with an actionable
  error**; it never silently returns a transcript with empty speakers
- the real backend (`pyannote.audio`) is an optional extra and its pretrained
  pipeline is gated behind a Hugging Face token, so the live path is not covered
  by CI. The *assignment* logic is fully tested with a stub.

Design: a diarizer answers "who spoke when" as a list of turns over the audio.
Assigning speakers to transcript segments is a separate, pure step, so it can be
tested without any model at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from textflowkit.core.model import Segment

ENV_HF_TOKEN = "HF_TOKEN"
ENV_PYANNOTE_MODEL = "TEXTFLOWKIT_PYANNOTE_MODEL"
DEFAULT_PYANNOTE_MODEL = "pyannote/speaker-diarization-3.1"


class DiarizationError(RuntimeError):
    """Raised when diarization was requested but could not be performed."""


@dataclass(slots=True)
class SpeakerTurn:
    """A span of audio attributed to one speaker."""

    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Diarizer(Protocol):
    """Identifies who spoke when."""

    name: str

    def diarize(self, audio_path: str | Path) -> list[SpeakerTurn]: ...


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Seconds of overlap between two spans. Zero when they do not intersect."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(
    segments: list[Segment],
    turns: list[SpeakerTurn],
    *,
    min_overlap: float = 0.0,
) -> int:
    """Label each segment with the speaker who overlaps it most.

    A segment is assigned to the turn with the greatest time overlap. Ties go to
    the earlier turn, so the result is deterministic rather than dependent on
    iteration order. A segment with no overlapping turn is left unlabelled
    rather than guessed at.

    Returns the number of segments that received a label.
    """
    if not turns:
        return 0

    labelled = 0
    for segment in segments:
        best: SpeakerTurn | None = None
        best_overlap = 0.0
        for turn in turns:
            amount = overlap(segment.start, segment.end, turn.start, turn.end)
            # Strictly greater wins. On an exact tie the earlier turn wins, so
            # the result depends on the turns themselves and not on the order
            # they happen to arrive in.
            if amount > best_overlap or (
                amount == best_overlap and best is not None and turn.start < best.start
            ):
                best = turn
                best_overlap = amount
        if best is not None and best_overlap > min_overlap:
            segment.speaker = best.speaker
            labelled += 1
    return labelled


class PyannoteDiarizer:
    """Diarization via `pyannote.audio`.

    Optional dependency, and the pretrained pipeline is gated: it needs a Hugging
    Face token with access granted to the model. Both conditions are checked up
    front so the failure names what is missing instead of surfacing as an opaque
    load error.
    """

    name = "pyannote"

    def __init__(self, model: str | None = None, token: str | None = None) -> None:
        self.model_name = model or os.environ.get(ENV_PYANNOTE_MODEL, DEFAULT_PYANNOTE_MODEL)
        self._token = token or os.environ.get(ENV_HF_TOKEN)
        self._pipeline = None

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DiarizationError(
                "diarization requires the optional 'diarize' extra. "
                "Install with: pip install 'textflowkit[diarize]'"
            ) from exc
        if not self._token:
            raise DiarizationError(
                f"diarization requires a Hugging Face token with access to "
                f"'{self.model_name}'. Set {ENV_HF_TOKEN}, or pass token=... . "
                "The model is gated, so access must also be granted on Hugging Face."
            )
        try:
            self._pipeline = Pipeline.from_pretrained(self.model_name, use_auth_token=self._token)
        except Exception as exc:  # provider errors vary
            raise DiarizationError(
                f"could not load diarization model '{self.model_name}': {exc}"
            ) from exc
        return self._pipeline

    def diarize(self, audio_path: str | Path) -> list[SpeakerTurn]:
        pipeline = self._load()
        try:
            annotation = pipeline(str(audio_path))
        except Exception as exc:
            raise DiarizationError(f"diarization failed: {exc}") from exc

        turns: list[SpeakerTurn] = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            turns.append(SpeakerTurn(start=turn.start, end=turn.end, speaker=str(speaker)))
        return turns


def get_diarizer(backend: str = "pyannote", **kwargs) -> Diarizer:
    if backend in ("pyannote", "default"):
        return PyannoteDiarizer(**kwargs)
    raise DiarizationError(f"unknown diarization backend: {backend}")
