"""The cancellation signal.

Lives in its own leaf module so lower layers (the source layer, which must abort
a download mid-flight) can re-raise it without importing the executor. Sources
cannot import `core.executor` - `core.pipeline` imports sources, so that would
be a cycle.

Anything that catches broad exceptions while doing cancellable work MUST re-raise
this first, or a cancellation silently becomes a failure.
"""

from __future__ import annotations


class CancelledError(Exception):
    """Raised when cancellation has been requested.

    Callers must not treat this as a failure - it is an orderly stop.
    """
