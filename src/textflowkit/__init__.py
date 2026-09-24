"""textflowkit - cross-platform media transcription toolkit."""

from textflowkit.core.model import Segment, Transcript, WordTiming
from textflowkit.core.pipeline import PipelineError, transcribe

__version__ = "0.1.5"

__all__ = ["PipelineError", "Segment", "Transcript", "WordTiming", "__version__", "transcribe"]
