"""Real CLI/pipeline boundaries missed by the v0.1.1 release suite."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
import wave
from pathlib import Path

import pytest

from textflowkit import cli
from textflowkit.core.executor import reset_default_executor
from textflowkit.core.jobs import reset_default_store
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.sqlite_store import SqliteJobStore
from textflowkit.sources.acquire import require_tool

# Developer HTTP refuses a peer it cannot judge, and `TestClient`'s default peer
# is the non-address `testclient`; the in-process callers below declare the
# loopback peer a real local caller has. No socket is opened.
LOCAL_PEER = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def _release_default_store():
    """Do not leave a store built under this file's TEXTFLOWKIT_DB cached.

    `cli_boundary` points TEXTFLOWKIT_DB at a temporary database but
    monkeypatches only `cli.get_default_store`; the MCP and HTTP adapters call
    the process-wide `get_default_store`/`get_default_executor` directly. A test
    that touches them caches a SqliteJobStore bound to a directory pytest then
    deletes, and every later test file inherits it. Reset both - the executor
    holds its own reference to the store it was built with.
    """
    yield
    reset_default_executor()
    reset_default_store()


def _wav(path: Path, seconds: int = 1) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(bytes(16000 * 2 * seconds))


def _media(path: Path) -> None:
    ffmpeg = require_tool("ffmpeg")
    if path.suffix == ".mp3":
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
               "sine=frequency=440:duration=1", "-c:a", "mp3", str(path)]
    elif path.suffix == ".mp4":
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
               "color=c=black:s=16x16:d=1", "-f", "lavfi", "-i",
               "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", "aac",
               "-shortest", str(path)]
    else:
        raise ValueError(path)
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


class _RecordingEngine:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio: Path, *, language: str | None = None) -> Transcript:
        self.calls += 1
        with wave.open(str(audio), "rb") as src:
            duration = src.getnframes() / src.getframerate()
        return Transcript(source=str(audio), language=language or "en", duration=duration,
                          segments=[Segment(0.0, duration, f"duration={duration:.1f}")])


@pytest.fixture
def cli_boundary(tmp_path, monkeypatch):
    from textflowkit.core import pipeline

    root = tmp_path / "input"
    output = tmp_path / "output"
    root.mkdir()
    output.mkdir()
    store = SqliteJobStore(tmp_path / "jobs.db")
    engine = _RecordingEngine()
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(output))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(cli, "get_default_store", lambda: store)
    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: engine)
    yield root, output, store, engine
    store.close()


@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".mp4"])
def test_confined_cli_transcribes_real_media(suffix, cli_boundary, capsys):
    root, output, _store, engine = cli_boundary
    media = root / f"clip{suffix}"
    _wav(media) if suffix == ".wav" else _media(media)

    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--model", "tiny", "--device", "cpu", "--quiet"])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert engine.calls == 1
    result = Path(captured.out.strip())
    assert result.is_file() and result.parent == output
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["segments"][0]["text"].startswith("duration=1")


def test_direct_pipeline_export_preflight_runs_before_media_work(cli_boundary, monkeypatch):
    from textflowkit.core import pipeline

    root, output, _store, engine = cli_boundary
    media = root / "clip.wav"
    _wav(media)

    def unavailable(formats):
        assert formats == ["pdf"]
        raise ValueError("PDF export requires the export extra")

    monkeypatch.setattr(pipeline, "validate_export_requirements", unavailable)
    with pytest.raises(ValueError, match="PDF export"):
        pipeline.transcribe(str(media), formats=["pdf"], output_dir=output)
    assert engine.calls == 0


def test_completed_local_resume_requires_existing_unchanged_file(cli_boundary, capsys):
    root, output, store, engine = cli_boundary
    media = root / "clip.wav"
    _wav(media)
    args = ["transcribe", str(media), "--formats", "json", "--output-dir",
            str(output), "--model", "tiny", "--device", "cpu", "--quiet"]
    assert cli.main(args) == 0
    first = capsys.readouterr().out.strip()
    assert cli.main([*args, "--resume"]) == 0
    assert capsys.readouterr().out.strip() == first
    assert engine.calls == 1

    # Same-size edits must also be detected; size/mtime alone is insufficient.
    with media.open("r+b") as changed:
        changed.seek(100)
        changed.write(b"\x01")
    assert cli.main([*args, "--resume"]) != 0
    assert "changed" in capsys.readouterr().err.lower()
    assert engine.calls == 1

    _wav(media)
    assert cli.main([*args, "--resume"]) == 0
    capsys.readouterr()
    assert engine.calls == 1

    _wav(media, seconds=3)
    assert cli.main([*args, "--resume"]) != 0
    assert "changed" in capsys.readouterr().err.lower()
    assert engine.calls == 1

    media.unlink()
    assert cli.main([*args, "--resume"]) != 0
    assert "missing" in capsys.readouterr().err.lower() or "no such file" in capsys.readouterr().err.lower()
    assert engine.calls == 1
    assert len(store.list()) == 1


@pytest.mark.parametrize("change", ["replace", "delete"])
def test_local_resume_rejects_changed_or_deleted_media_independent_of_wav_fix(
    cli_boundary, monkeypatch, capsys, change,
):
    root, output, _store, engine = cli_boundary
    monkeypatch.delenv("TEXTFLOWKIT_INPUT_ROOT")
    media = root / "mutable.mp3"
    _media(media)
    args = ["transcribe", str(media), "--formats", "json", "--output-dir",
            str(output), "--model", "tiny", "--quiet"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert engine.calls == 1
    if change == "delete":
        media.unlink()
    else:
        ffmpeg = require_tool("ffmpeg")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=880:duration=1", "-c:a", "mp3", str(media)],
                       check=True, capture_output=True, timeout=30)
    assert cli.main([*args, "--resume"]) == 1
    err = capsys.readouterr().err.lower()
    assert ("missing" if change == "delete" else "changed") in err
    assert engine.calls == 1


def test_http_and_mcp_resume_share_local_identity_validation(
    cli_boundary, monkeypatch, capsys,
):
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server, mcp_server

    root, output, store, engine = cli_boundary
    media = root / "shared.wav"
    _wav(media)
    assert cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                     str(output), "--model", "tiny", "--quiet"]) == 0
    capsys.readouterr()
    assert engine.calls == 1
    job = store.list(limit=1)[0]
    monkeypatch.setattr(http_server, "get_default_store", lambda: store)
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: store)
    assert mcp_server.resume_job(job.id)["state"] == "done"
    assert TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(f"/jobs/{job.id}/resume").status_code == 202

    _wav(media, seconds=2)
    assert "changed" in mcp_server.resume_job(job.id)["error"]
    http = TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(f"/jobs/{job.id}/resume")
    assert http.status_code == 409
    assert "changed" in http.json()["detail"]
    assert engine.calls == 1


def test_mcp_resume_honors_tightened_input_root(cli_boundary, monkeypatch):
    """A8: resume re-checks the *current* input root, not the saved one.

    A job requested while a wider root was configured serializes that root into
    its durable request. After an operator tightens TEXTFLOWKIT_INPUT_ROOT, MCP
    resume must refuse the out-of-boundary source instead of reinstating the
    saved root; HTTP already does. A source inside the new root still resumes.
    """
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server, mcp_server
    from textflowkit.core import submission
    from textflowkit.core.jobs import JobState

    root, output, store, engine = cli_boundary
    wide = root / "wide"
    narrow = root / "narrow"
    wide.mkdir()
    narrow.mkdir()
    outside = wide / "old.wav"
    inside = narrow / "new.wav"
    _wav(outside)
    _wav(inside)

    def request_for(media, media_root):
        return submission.SubmissionRequest(
            source=str(media), formats=["json"], output_dir=str(output),
            model="tiny", device="cpu", input_root=str(media_root),
        )

    # Saved while the wider root was configured, then interrupted and reaped.
    saved = submission.submit_request(store, request_for(outside, wide), background=False)
    assert store.get(saved.id).request["input_root"] == str(wide)
    store.update(saved.id, state=JobState.RUNNING, progress="transcribing")
    assert store.reap_incomplete(reason="simulated restart") == 1
    assert store.get(saved.id).state is JobState.ERROR
    assert engine.calls == 1
    rendered = sorted(p.name for p in output.glob("*.json"))
    assert len(rendered) == 1

    monkeypatch.setattr(http_server, "get_default_store", lambda: store)
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: store)
    # The operator tightens the boundary: `wide/old.wav` is now outside it.
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(narrow))

    mcp_result = mcp_server.resume_job(saved.id)
    # Nothing may be re-run or re-published against the stale wider root.
    assert engine.calls == 1
    assert store.get(saved.id).state is JobState.ERROR
    assert sorted(p.name for p in output.glob("*.json")) == rendered
    assert "error" in mcp_result, mcp_result
    assert "outside the allowed input root" in mcp_result["error"]

    http = TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(
        f"/jobs/{saved.id}/resume"
    )
    assert http.status_code == 409
    assert "outside the allowed input root" in http.json()["detail"]

    # A source inside the tightened root still resumes on both surfaces.
    allowed = submission.submit_request(store, request_for(inside, narrow), background=False)
    assert engine.calls == 2
    assert mcp_server.resume_job(allowed.id)["state"] == "done"
    assert TestClient(http_server.app, base_url="http://127.0.0.1", client=LOCAL_PEER).post(
        f"/jobs/{allowed.id}/resume"
    ).status_code == 202
    assert engine.calls == 2


def test_local_diarization_resume_reacquires_wav_without_collision(
    cli_boundary, monkeypatch, capsys,
):
    from textflowkit.core import pipeline
    from textflowkit.core.diarize import SpeakerTurn

    root, output, store, engine = cli_boundary
    media = root / "speaker.wav"
    _wav(media)
    assert cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                     str(output), "--model", "tiny", "--quiet"]) == 0
    capsys.readouterr()
    assert engine.calls == 1
    checkpoint = store.list(limit=1)[0].checkpoint

    class Diarizer:
        name = "fixture"

        def diarize(self, audio):
            assert Path(audio).is_file()
            return [SpeakerTurn(0.0, 1.0, "SPEAKER_00")]

    monkeypatch.setattr(pipeline, "get_engine", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Whisper must not rerun on diarization resume")))
    monkeypatch.setattr(pipeline, "get_diarizer", lambda *a, **k: Diarizer())
    result = pipeline.transcribe(str(media), model="tiny", formats=["json"],
                                 input_root=root, output_dir=output, diarize=True,
                                 resume_checkpoint=checkpoint)
    assert result.transcript.segments[0].speaker == "SPEAKER_00"


def test_known_overlong_media_stops_before_decode(cli_boundary, monkeypatch, capsys):
    from textflowkit.core import pipeline

    root, output, _store, engine = cli_boundary
    media = root / "long.mp3"
    ffmpeg = require_tool("ffmpeg")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:a", "mp3", str(media)],
                   check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_DURATION_SECONDS", "1")
    calls = []
    real_extract = pipeline.extract_audio

    def counted_extract(*args, **kwargs):
        calls.append(1)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(pipeline, "extract_audio", counted_extract)
    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--quiet"])
    assert rc == 1
    assert "duration exceeds" in capsys.readouterr().err
    assert calls == []
    assert engine.calls == 0


def test_unknown_duration_is_stopped_by_decode_byte_boundary(
    cli_boundary, monkeypatch, capsys,
):
    from textflowkit.core import service

    root, output, _store, engine = cli_boundary
    media = root / "unknown-duration.mp3"
    ffmpeg = require_tool("ffmpeg")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:a", "mp3", str(media)],
                   check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_DURATION_SECONDS", "1")
    real_probe = service._probe_duration
    monkeypatch.setattr(service, "_probe_duration", lambda path: None if path == media
                        else real_probe(path))
    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--quiet"])
    assert rc == 1
    assert "duration exceeds" in capsys.readouterr().err
    assert engine.calls == 0


def test_expanding_decode_stops_at_output_cap_and_cleans(cli_boundary, monkeypatch, capsys):
    from textflowkit.core import pipeline

    root, output, _store, engine = cli_boundary
    media = root / "compressed.mp3"
    ffmpeg = require_tool("ffmpeg")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:a", "mp3", str(media)],
                   check=True, capture_output=True, timeout=30)
    cap = media.stat().st_size + 1000
    assert cap < 3 * 16000 * 2
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_MEDIA_BYTES", str(cap))
    monkeypatch.setenv("TEXTFLOWKIT_MAX_DURATION_SECONDS", "10")
    scratch = []
    original_mkdtemp = pipeline.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(original_mkdtemp(*args, **kwargs))
        scratch.append(path)
        return str(path)

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", tracked_mkdtemp)
    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--quiet"])
    assert rc == 1
    assert "decoded output exceeds" in capsys.readouterr().err
    assert engine.calls == 0
    assert not list(output.glob("*.json"))
    assert scratch and all(not path.exists() for path in scratch)


def test_scratch_cleanup_retries_transient_windows_lock(tmp_path, monkeypatch):
    from textflowkit.core import pipeline

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "decoded.wav").write_bytes(b"fixture")
    real_rmtree = pipeline.shutil.rmtree
    attempts = []

    def transient_lock(path, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("simulated transient decoder lock")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline.shutil, "rmtree", transient_lock)
    pipeline._cleanup_scratch(scratch)
    assert len(attempts) == 3
    assert not scratch.exists()


def _sleeping_decoder(monkeypatch):
    from textflowkit.sources import acquire

    real_popen = subprocess.Popen
    real_run = subprocess.run
    children = []

    def launch(_cmd, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(3)"], **kwargs)
        children.append(child)
        return child

    def old_run(_cmd, **kwargs):
        if _cmd[0] == "taskkill":
            return real_run(_cmd, **kwargs)
        # The v0.1.1 implementation used subprocess.run with no timeout. Give
        # the isolated baseline the same harmless three-second child, so its
        # regression fails for behavior rather than a missing mock attribute.
        child = real_popen([sys.executable, "-c", "import time; time.sleep(3)"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        children.append(child)
        child.communicate()
        return types.SimpleNamespace(returncode=child.returncode, stdout="", stderr="")

    monkeypatch.setattr(acquire, "subprocess", types.SimpleNamespace(
        Popen=launch, run=old_run, PIPE=subprocess.PIPE,
        TimeoutExpired=subprocess.TimeoutExpired,
    ))
    return children


def test_ffmpeg_timeout_kills_child_and_cleans(cli_boundary, monkeypatch, capsys):
    from textflowkit.core import pipeline

    root, output, _store, engine = cli_boundary
    media = root / "clip.wav"
    _wav(media)
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS", "1")
    children = _sleeping_decoder(monkeypatch)
    scratch = []
    original_mkdtemp = pipeline.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(original_mkdtemp(*args, **kwargs))
        scratch.append(path)
        return str(path)

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", tracked_mkdtemp)
    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--quiet"])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err
    assert engine.calls == 0
    assert children and all(child.poll() is not None for child in children)
    assert scratch and all(not path.exists() for path in scratch)


def test_ffmpeg_timeout_error_names_setting_in_default_profile(cli_boundary, monkeypatch, capsys):
    """The decode timeout also fires for a local CLI job in the default profile.

    An operator who hits it needs the setting name and the duration in effect,
    not just "timed out", because the fix is to raise that one variable.
    """
    root, output, _store, engine = cli_boundary
    media = root / "clip.wav"
    _wav(media)
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "developer")
    monkeypatch.setenv("TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS", "1")
    children = _sleeping_decoder(monkeypatch)
    rc = cli.main(["transcribe", str(media), "--formats", "json", "--output-dir",
                   str(output), "--quiet"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "timed out" in err
    assert "TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS" in err
    assert "after 1s" in err
    assert "default 600" in err
    assert engine.calls == 0
    assert children and all(child.poll() is not None for child in children)


def test_cancellation_kills_decoder_child_and_cleans(cli_boundary, monkeypatch):
    from textflowkit.core import pipeline
    from textflowkit.core.cancel import CancelledError

    root, output, _store, _engine = cli_boundary
    media = root / "clip.wav"
    _wav(media)
    children = _sleeping_decoder(monkeypatch)
    scratch = []
    original_mkdtemp = pipeline.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(original_mkdtemp(*args, **kwargs))
        scratch.append(path)
        return str(path)

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", tracked_mkdtemp)

    def cancel_when_decoding():
        if children:
            raise CancelledError()

    started = time.monotonic()
    with pytest.raises(CancelledError):
        pipeline.transcribe(str(media), formats=["json"], output_dir=output,
                            input_root=root, check_cancel=cancel_when_decoding)
    assert time.monotonic() - started < 2.5, "cancellation waited for the decoder to finish"
    assert children and all(child.poll() is not None for child in children)
    assert scratch and all(not path.exists() for path in scratch)


def test_download_byte_cap_aborts_during_transfer(cli_boundary, monkeypatch, capsys):
    from textflowkit.core import pipeline
    from textflowkit.sources import acquire
    from textflowkit.sources.detect import SourceRef

    _root, output, _store, _engine = cli_boundary
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_EGRESS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("TEXTFLOWKIT_MAX_MEDIA_BYTES", "100")
    monkeypatch.setattr(pipeline, "resolve_source", lambda url: SourceRef(
        kind="url", location=url, platform="direct",
    ))
    monkeypatch.setattr(acquire, "_validate_fetch_url", lambda url: None)
    written = []

    class FakeYDL:
        # A real YoutubeDL builds a request director lazily; the redirect guard
        # refuses a build whose HTTP handlers cannot be verified, so the double
        # exposes one with no transport behind it.
        _request_director = types.SimpleNamespace(handlers={})

        def __init__(self, opts):
            self.opts = opts
            self.urlopen = lambda req: None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "fixture", "url": "https://example.com/media.mp3"}

        def process_info(self, info):
            path = Path(self.opts["outtmpl"].replace("%(id)s", "fixture")
                        .replace("%(ext)s", "mp3"))
            with path.open("wb") as out:
                for n in range(4):
                    out.write(bytes(64))
                    out.flush()
                    written.append((n + 1) * 64)
                    for hook in self.opts["progress_hooks"]:
                        hook({"status": "downloading", "downloaded_bytes": (n + 1) * 64,
                              "filename": str(path)})

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    rc = cli.main(["transcribe", "https://example.com/media.mp3", "--formats", "json",
                   "--output-dir", str(output), "--quiet"])
    assert rc == 1
    assert "download exceeds" in capsys.readouterr().err
    assert written == [64, 128], "transfer continued after first cap crossing"
