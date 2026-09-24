"""textflowkit - cross-platform media transcription toolkit."""

from textflowkit.core.model import Segment, Transcript
from textflowkit.core.pipeline import PipelineError, transcribe

__version__ = "0.1.4"

__all__ = ["PipelineError", "Segment", "Transcript", "__version__", "transcribe"]
