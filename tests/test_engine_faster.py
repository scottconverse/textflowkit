"""Opt-in `faster-whisper` engine: selection, mapping, locking, and CLI wiring.

Everything here is deterministic: the `faster_whisper` package is faked with a
module object installed in `sys.modules`, so no model is downloaded and no
network is touched. The real package is absent from the test environment, which
is exactly the state the missing-extra tests rely on - and they block the name
explicitly (`sys.modules[name] = None` makes `import name` raise ImportError)
so they do not silently pass for the wrong reason if it is ever installed.
"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from textflowkit import cli as cli_mod
from textflowkit.core.engine import get_engine
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript


def _block_import(monkeypatch, name: str = "faster_whisper") -> None:
    """Make ``import name`` raise ImportError regardless of the environment."""
    monkeypatch.setitem(sys.modules, name, None)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _RecordingLock:
    """An RLock that records how deeply it is held at chosen moments."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0
        self.max_depth = 0

    def acquire(self, *args, **kwargs):
        self._lock.acquire(*args, **kwargs)
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)

    def release(self) -> None:
        self.depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _fake_faster_whisper(observations: list, seen: dict, *, lock=None):
    """A stand-in `faster_whisper` module whose transcribe is a lazy generator.

    Upstream starts inference during iteration, so the generator appends an
    observation at every step; that is how the lock test sees when the work
    actually happens.
    """

    class FakeWord:
        def __init__(self, start, end, word):
            self.start = start
            self.end = end
            self.word = word
            self.probability = 0.9

    class FakeSegment:
        def __init__(self, start, end, text, words):
            self.start = start
            self.end = end
            self.text = text
            self.words = words

    class FakeInfo:
        language = "en"
        language_probability = 0.98

    class FakeModel:
        def __init__(self, name, device, compute_type):
            self.name = name
            self.device = device
            self.compute_type = compute_type
            seen["model_args"] = (name, device, compute_type)

        def transcribe(self, path, **kwargs):
            seen["transcribe_path"] = path
            seen["transcribe_kwargs"] = kwargs
            seen["exhausted"] = False

            def generator():
                try:
                    for raw in (
                        FakeSegment(0.0, 1.5, " Hello world", [
                            FakeWord(0.1, 0.6, " Hello"),
                            FakeWord(0.7, 1.2, " world"),
                        ]),
                        FakeSegment(1.5, 2.0, "   ", []),  # whitespace only: dropped
                        FakeSegment(2.0, 3.0, "Again", []),
                    ):
                        observations.append(
                            ("yield", lock.depth if lock is not None else None)
                        )
                        yield raw
                finally:
                    seen["exhausted"] = True

            return generator(), FakeInfo()

    return SimpleNamespace(WhisperModel=FakeModel)


# --------------------------------------------------------------------------
# Part A: the extra is declared, and stays optional
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11")
def test_faster_whisper_is_an_optional_extra_never_a_base_dependency():
    from pathlib import Path

    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]

    extra = project["optional-dependencies"]["faster-whisper"]
    assert any(requirement.startswith("faster-whisper") for requirement in extra)
    # The ROCm/openai-whisper path must not change: no faster-whisper anywhere
    # in the base install, and the base engine dependency is still there.
    assert not any("faster-whisper" in requirement for requirement in project["dependencies"])
    assert any("openai-whisper" in requirement for requirement in project["dependencies"])


# --------------------------------------------------------------------------
# Part A: selection and default preservation
# --------------------------------------------------------------------------


def test_faster_whisper_engine_is_selectable():
    engine = get_engine("faster-whisper", model="tiny", device="cpu")
    assert engine.name == "faster-whisper"
    assert engine.model_name == "tiny"


def test_unknown_engine_is_still_rejected():
    with pytest.raises(ValueError, match="unknown engine"):
        get_engine("no-such-engine")


def test_default_engine_is_unchanged(monkeypatch):
    """`whisper`/`openai-whisper`/`default` keep resolving to openai-whisper."""
    module = SimpleNamespace(load_model=lambda *a, **k: SimpleNamespace(transcribe=lambda *a, **k: {}))
    monkeypatch.setitem(sys.modules, "whisper", module)
    assert get_engine().name == "openai-whisper"
    assert get_engine("whisper").name == "openai-whisper"
    assert get_engine("default").name == "openai-whisper"
    assert get_engine("openai-whisper") is get_engine("default")


# --------------------------------------------------------------------------
# Part A: canonical mapping
# --------------------------------------------------------------------------


def test_faster_whisper_maps_to_the_canonical_transcript(monkeypatch, tmp_path):
    observations: list = []
    seen: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper(observations, seen))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")

    transcript = get_engine("faster-whisper", model="small", device="cpu").transcribe(
        audio, language="en"
    )

    assert isinstance(transcript, Transcript)
    assert transcript.engine == "faster-whisper"
    assert transcript.language == "en"
    assert transcript.duration is None
    assert transcript.metadata["model"] == "small"
    assert transcript.metadata["device"] == "cpu"
    assert transcript.metadata["compute_type"] == "int8"
    assert [s.text for s in transcript.segments] == ["Hello world", "Again"]
    assert [(w.start, w.end, w.text) for w in transcript.segments[0].words] == [
        (0.1, 0.6, "Hello"),
        (0.7, 1.2, "world"),
    ]
    assert seen["transcribe_kwargs"]["word_timestamps"] is True
    assert seen["transcribe_path"] == str(audio)


def test_faster_whisper_defaults_to_cpu_int8(monkeypatch):
    seen: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], seen))
    engine = get_engine("faster-whisper", model="small")
    assert engine.device == "cpu"
    assert engine.compute_type == "int8"
    engine._load()
    assert seen["model_args"] == ("small", "cpu", "int8")


def test_explicit_cuda_is_passed_through_as_cuda(monkeypatch):
    """An explicit device reaches upstream as cuda - never silently remapped.

    CTranslate2's GPU path is CUDA, not ROCm, so an AMD box fails loudly at
    load; the point of this test is that the choice is not quietly rewritten
    into a CPU run the caller did not ask for.
    """
    seen: dict = {}
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], seen))
    engine = get_engine("faster-whisper", model="small", device="cuda")
    assert engine.device == "cuda"
    engine._load()
    assert seen["model_args"] == ("small", "cuda", "float16")


# --------------------------------------------------------------------------
# Part A: locking and eager consumption
# --------------------------------------------------------------------------


def test_generator_is_consumed_inside_the_model_lock(monkeypatch, tmp_path):
    """Upstream infers during iteration, so iteration must not outlive the lock."""
    observations: list = []
    seen: dict = {}
    lock = _RecordingLock()
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper(observations, seen, lock=lock))
    engine = get_engine("faster-whisper", model="lock-test-only", device="cpu")
    engine._lock = lock
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x")

    transcript = engine.transcribe(audio)

    assert len(observations) == 3
    # Every step of the generator ran while the engine held its lock...
    assert [depth for _kind, depth in observations] == [1, 1, 1]
    # ...and it was exhausted before transcribe returned, not left lazy.
    assert seen["exhausted"] is True
    assert lock.depth == 0
    assert len(transcript.segments) == 2


def test_faster_whisper_model_is_cached_like_the_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], {}))
    first = get_engine("faster-whisper", model="cache-test-only", device="cpu")
    second = get_engine("faster-whisper", model="cache-test-only", device="cpu")
    assert first is second


# --------------------------------------------------------------------------
# Part A: missing extra
# --------------------------------------------------------------------------


MISSING_HINT = "textflowkit[faster-whisper]"


def test_missing_extra_fails_at_load_with_an_install_message(monkeypatch, tmp_path):
    _block_import(monkeypatch)
    # A model name no other test uses: the engine cache is keyed on the
    # configuration, so a cached instance would already have a loaded model and
    # never reach the import.
    engine = get_engine("faster-whisper", model="missing-extra-only", device="cpu")
    with pytest.raises(RuntimeError) as excinfo:
        engine.transcribe(tmp_path / "audio.wav")
    assert MISSING_HINT in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


def test_cli_fails_before_acquiring_media_when_the_extra_is_missing(monkeypatch, capsys, tmp_path):
    """The CLI must refuse before downloading, decoding, or loading a model."""
    from textflowkit.core import pipeline

    _block_import(monkeypatch)
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())

    def explode(*args, **kwargs):
        raise AssertionError("media acquisition or inference ran before the engine check")

    monkeypatch.setattr(pipeline, "fetch_media", explode)
    monkeypatch.setattr(pipeline, "stage_confined_local_media", explode)
    monkeypatch.setattr(pipeline, "extract_audio", explode)
    monkeypatch.setattr(pipeline, "get_engine", explode)

    source = tmp_path / "meeting.wav"
    source.write_bytes(b"x")
    rc = cli_mod.main(["transcribe", str(source), "--engine", "faster-whisper"])
    captured = capsys.readouterr()

    assert rc == 1
    assert MISSING_HINT in captured.err
    assert "pip install" in captured.err


def test_cli_rejects_an_unknown_engine_before_acquiring_media(monkeypatch, capsys, tmp_path):
    from textflowkit.core import pipeline

    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())
    monkeypatch.setattr(pipeline, "fetch_media",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("acquired")))

    source = tmp_path / "meeting.wav"
    source.write_bytes(b"x")
    rc = cli_mod.main(["transcribe", str(source), "--engine", "gpt-9-whisper"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "unknown engine" in captured.err
    assert "faster-whisper" in captured.err


def test_batch_rejects_a_missing_extra_before_submitting(monkeypatch, capsys, tmp_path):
    _block_import(monkeypatch)
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())
    monkeypatch.setattr(cli_mod, "run_batch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("submitted")))

    source = tmp_path / "meeting.wav"
    source.write_bytes(b"x")
    rc = cli_mod.main(["batch", str(source), "--engine", "faster-whisper"])
    captured = capsys.readouterr()

    assert rc == 1
    assert MISSING_HINT in captured.err


# --------------------------------------------------------------------------
# Part A: CLI / batch pass the chosen engine through the shared request
# --------------------------------------------------------------------------


def _run_transcribe_capturing_request(monkeypatch, capsys, tmp_path, argv_extra):
    from textflowkit.core import runner
    from textflowkit.core.pipeline import TranscribeResult

    store = MemoryJobStore()
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: store)

    def fake_transcribe(source, **kwargs):
        return TranscribeResult(
            transcript=Transcript(source=source, language="en",
                                  segments=[Segment(0, 1, "hello")], engine=kwargs["engine"]),
            outputs=[],
        )

    monkeypatch.setattr(runner, "transcribe", fake_transcribe)
    out_dir = tmp_path / "out"
    rc = cli_mod.main([
        "transcribe", str(tmp_path / "meeting.wav"), "--quiet",
        "--output-dir", str(out_dir), "--formats", "json", *argv_extra,
    ])
    return rc, store


def test_cli_transcribe_passes_the_engine_into_the_request(monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], {}))
    rc, store = _run_transcribe_capturing_request(
        monkeypatch, capsys, tmp_path, ["--engine", "faster-whisper"]
    )
    assert rc == 0
    jobs = store.list()
    assert len(jobs) == 1
    assert jobs[0].request["engine"] == "faster-whisper"


def test_cli_transcribe_defaults_to_the_unchanged_engine(monkeypatch, capsys, tmp_path):
    rc, store = _run_transcribe_capturing_request(monkeypatch, capsys, tmp_path, [])
    assert rc == 0
    jobs = store.list()
    assert jobs[0].request["engine"] == "whisper"


def test_cli_batch_passes_the_engine_into_the_request(monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], {}))
    monkeypatch.setattr(cli_mod, "get_default_store", lambda: MemoryJobStore())
    captured: dict = {}

    def fake_run_batch(sources, *, store, **kwargs):
        captured.update(kwargs)
        from textflowkit.core.batch import BatchReport

        return BatchReport(items=[])

    monkeypatch.setattr(cli_mod, "run_batch", fake_run_batch)

    rc = cli_mod.main(["batch", "one", "--engine", "faster-whisper", "--quiet"])

    assert rc == 0
    assert captured["engine"] == "faster-whisper"


def test_batch_request_carries_the_engine_into_the_job_record(monkeypatch):
    from textflowkit.core import submission
    from textflowkit.core.batch import run_batch

    # U43: `submit_request` now preflights the optional extra before a job is
    # created, so naming faster-whisper without it is refused rather than run.
    # The engine still has to survive into the record, which is what this test
    # is about - so the package is faked, not absent.
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        _fake_faster_whisper([], {}))
    store = MemoryJobStore()
    seen: list = []
    monkeypatch.setattr(submission, "run_job",
                        lambda job, s, **kwargs: seen.append(job.request))

    report = run_batch(["one"], store=store, formats=["json"], output_dir=".",
                       engine="faster-whisper")

    assert report.total == 1
    assert [r["engine"] for r in seen] == ["faster-whisper"]


# --------------------------------------------------------------------------
# Part A: engine choice is part of resume identity
# --------------------------------------------------------------------------


def test_engine_choice_is_part_of_resume_identity(monkeypatch):
    """A faster-whisper checkpoint must never be reused for a whisper request."""
    from textflowkit.core import submission
    from textflowkit.core.checkpoint import checkpoint_for_request, write_checkpoint

    source = "https://example.invalid/media"
    store = MemoryJobStore()
    monkeypatch.setattr(submission, "run_job", lambda *a, **k: None)

    seed = submission.SubmissionRequest(
        source=source, model="small", engine="faster-whisper")
    job = store.create(source, request=seed.to_dict())
    checkpoint = checkpoint_for_request(
        source=source, model="small", engine="faster-whisper", options=seed.options())
    checkpoint.finished_stages = ["source", "fetch", "extract", "transcribe"]
    checkpoint.transcript = Transcript(
        source=source, segments=[Segment(0, 1, "hi")]).to_dict()
    write_checkpoint(store, job.id, checkpoint)
    store.update(job.id, state=JobState.DONE,
                 transcript=Transcript(source=source, segments=[Segment(0, 1, "hi")]).to_dict())

    same = submission.submit_request(
        store,
        submission.SubmissionRequest(source=source, model="small", engine="faster-whisper"),
        background=False, resume=True,
    )
    assert same.id == job.id

    other = submission.submit_request(
        store,
        submission.SubmissionRequest(source=source, model="small", engine="whisper"),
        background=False, resume=True,
    )
    assert other.id != job.id
