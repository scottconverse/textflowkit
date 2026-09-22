"""Batch jobs should reuse loaded Whisper weights without concurrent model use."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from textflowkit.core.engine import get_engine


def test_whisper_model_loads_once_for_repeated_jobs(monkeypatch, tmp_path):
    loads = []

    class FakeModel:
        def transcribe(self, path, **kwargs):
            return {"language": "en", "segments": [{"start": 0, "end": 1, "text": "hello"}]}

    monkeypatch.setitem(
        sys.modules, "whisper",
        SimpleNamespace(load_model=lambda name, device: loads.append((name, device)) or FakeModel()),
    )
    first = get_engine("whisper", model="cache-test-only", device="cpu")
    second = get_engine("whisper", model="cache-test-only", device="cpu")
    assert first is second
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")
    assert first.transcribe(audio).segments[0].text == "hello"
    assert second.transcribe(audio).segments[0].text == "hello"
    assert loads == [("cache-test-only", "cpu")]
