"""Disable telemetry in optional dependencies before they cache their settings."""

from __future__ import annotations

import os
import sys

_TELEMETRY_ENV = {
    "PYANNOTE_METRICS_ENABLED": "false",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}


def disable_dependency_telemetry() -> None:
    """Force dependency telemetry off, overriding inherited opt-ins.

    Hugging Face Hub reads its opt-out constants at import time. If it was
    already imported (for example, by an embedding application), update the
    cached constant without importing the optional package ourselves.
    """
    os.environ.update(_TELEMETRY_ENV)

    hub_constants = sys.modules.get("huggingface_hub.constants")
    if hub_constants is not None:
        hub_constants.HF_HUB_DISABLE_TELEMETRY = True


disable_dependency_telemetry()
