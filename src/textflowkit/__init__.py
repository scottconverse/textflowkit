"""textflowkit - cross-platform media transcription toolkit."""

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

__version__ = "0.1.11"

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
