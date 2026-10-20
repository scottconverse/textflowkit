"""textflowkit - cross-platform media transcription toolkit."""

# Disable dependency telemetry before importing modules that may cache it.
from textflowkit.core import privacy as _privacy  # noqa: F401
from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.core.pipeline import PipelineError, transcribe
from textflowkit.core.streaming import (
    StreamEvent,
    StreamEventKind,
    StreamingError,
    StreamingProtocolError,
    StreamingQueueFullError,
    StreamingResourceError,
    StreamingSession,
    StreamWord,
)
from textflowkit.core.streaming_events import (
    SessionLimitError,
    SessionStateError,
    WorkerProcessError,
)

__version__ = "0.1.16"

__all__ = [
    "PipelineError",
    "Segment",
    "SessionLimitError",
    "SessionStateError",
    "StreamEvent",
    "StreamEventKind",
    "StreamWord",
    "StreamingError",
    "StreamingProtocolError",
    "StreamingQueueFullError",
    "StreamingResourceError",
    "StreamingSession",
    "Transcript",
    "WordTiming",
    "WorkerProcessError",
    "__version__",
    "transcribe",
]
