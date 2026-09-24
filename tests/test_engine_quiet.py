"""Whisper's own chatter must not reach the user under --quiet or the adapters.

openai-whisper's ``verbose`` is tri-state, not a boolean. In the installed
version (20250625, ``whisper/transcribe.py``):

* ``disable=verbose is not False`` (line 264) — the tqdm frame bar is shown
  *only* when ``verbose is False``, so False is the chatty mode, not the quiet
  one;
* ``if verbose is not None`` (line 154) — the "Detected language" line is
  printed for both True and False;
* ``if verbose:`` (line 478) — per-segment text is printed only for True.

``verbose=None`` is therefore the actually-silent mode: no bar, no language
line, no text. The engine asked for ``False`` and leaked the first two into the
shared streams that the CLI, MCP, and HTTP adapters all write to, where the CLI
cannot suppress them (its own ``--quiet`` gate only covers its own prints).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from textflowkit import cli as cli_mod
from textflowkit.core.engine import get_engine
from textflowkit.core.jobs import JobState, MemoryJobStore


class _FakeWhisperModel:
    """Replays the installed whisper's verbose behaviour, leaks included.

    No model is downloaded and no audio is decoded: the real semantics being
    tested are the ``verbose`` predicates above, so the fake reproduces exactly
    those two branches and nothing else.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, path, *, language=None, fp16=None, verbose=None, word_timestamps=False):
        self.calls.append(
            {
                "path": str(path),
                "language": language,
                "verbose": verbose,
                "word_timestamps": word_timestamps,
            }
        )
        if verbose is not None:  # whisper/transcribe.py:154-156
            print("Detected language: English")
        if verbose is False:  # whisper/transcribe.py:264-265
            print("100%|##########| 100/100 [00:01<00:00, 99.9frames/s]", file=sys.stderr)
        return {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": " hello", "words": []}],
        }


def _install_fake_whisper(monkeypatch) -> _FakeWhisperModel:
    model = _FakeWhisperModel()
    monkeypatch.setitem(
        sys.modules, "whisper", SimpleNamespace(load_model=lambda name, device: model)
    )
    return model


def test_engine_asks_whisper_for_its_silent_mode(monkeypatch, tmp_path):
    """``verbose=False`` is whisper's progress-bar mode; None is silence."""
    model = _install_fake_whisper(monkeypatch)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")

    transcript = get_engine("whisper", model="silent-mode-only", device="cpu").transcribe(audio)

    assert transcript.segments[0].text == "hello"
    assert transcript.language == "en"
    verbose = model.calls[0]["verbose"]
    assert verbose is None, (
        "openai-whisper shows its frame progress bar when verbose is False "
        "(transcribe.py:264-265: disable=verbose is not False) and prints the "
        f"detected language when verbose is not None (154-156); got verbose={verbose!r}"
    )


def test_whisper_progress_does_not_leak_into_our_streams(monkeypatch, capsys, tmp_path):
    """Whatever whisper decides to print must not land in our stdout/stderr."""
    _install_fake_whisper(monkeypatch)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")

    get_engine("whisper", model="streams-only", device="cpu").transcribe(audio)

    captured = capsys.readouterr()
    assert captured.out == "", "engine leaked whisper status into stdout"
    assert captured.err == "", "engine leaked whisper progress into stderr"


def _stub_submission(monkeypatch, model_name: str):
    """Run the real engine where submit_request would have, then finish the job.

    This keeps the CLI boundary honest: the leak being tested is emitted by the
    engine inside the process the command runs in, so ``--quiet`` is the only
    thing standing between it and the user's terminal.
    """
    store = MemoryJobStore()

    def fake_store():
        return store

    def fake_submit(store_arg, request, **kwargs):
        job = store_arg.create(request.source, request=request.to_dict())
        transcript = get_engine("whisper", model=model_name, device="cpu").transcribe(request.source)
        return store_arg.update(job.id, state=JobState.DONE, transcript=transcript.to_dict())

    monkeypatch.setattr(cli_mod, "get_default_store", fake_store)
    monkeypatch.setattr(cli_mod, "submit_request", fake_submit)


def test_quiet_transcribe_does_not_show_whisper_progress(monkeypatch, capsys):
    model = _install_fake_whisper(monkeypatch)
    _stub_submission(monkeypatch, "quiet-cli-only")

    rc = cli_mod.main(["transcribe", "media.wav", "--quiet"])

    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert model.calls[0]["verbose"] is None
    assert captured.out == "", "--quiet still showed whisper status on stdout"
    assert captured.err == "", "--quiet still showed whisper progress on stderr"
