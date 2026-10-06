"""Streamed raw-body uploads: limits, owned paths, and partial cleanup.

The upload path is where an untrusted body meets the filesystem, so these tests
pin the safety properties directly: the size cap is enforced both by declared
length and while streaming, a refused or aborted upload leaves no partial file
behind, and the destination path is always one this endpoint generated (never a
name from the request).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from textflowkit.ui import paths, uploads
from textflowkit.ui.app import create_app

LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    """Point the UI's storage at a temp dir so uploads never touch the real one."""
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.delenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", raising=False)


@pytest.fixture
def client():
    app = create_app(host="127.0.0.1", port=8756)
    return TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)


@pytest.fixture
def headers(client):
    token = re.search(
        r'name="textflowkit-capability" content="([^"]+)"', client.get("/").text
    ).group(1)
    return {"x-textflowkit-ui-capability": token}


def upload(client, headers, content, filename="clip.mp3"):
    return client.post(
        "/ui/uploads",
        content=content,
        headers={**headers, "x-textflowkit-filename": filename,
                 "content-type": "application/octet-stream"},
    )


def staged_uploads() -> list:
    """The files currently in the uploads dir (empty if it does not exist yet)."""
    directory = paths.default_uploads_dir()
    return sorted(directory.iterdir()) if directory.exists() else []


def test_basic_upload_writes_owned_file(client, headers):
    resp = upload(client, headers, b"RIFF" + b"\x00" * 200)
    assert resp.status_code == 200
    body = resp.json()
    assert body["bytes"] == 204
    assert body["upload_id"].endswith(".mp3")
    staged = paths.default_uploads_dir() / body["upload_id"]
    assert staged.is_file()
    assert staged.read_bytes().startswith(b"RIFF")


def test_upload_requires_capability(client):
    resp = client.post("/ui/uploads", content=b"data",
                       headers={"content-type": "application/octet-stream"})
    assert resp.status_code == 403


def test_declared_length_over_limit_refused_before_writing(client, headers, monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", "100")
    resp = upload(client, headers, b"x" * 500)
    assert resp.status_code == 413
    assert resp.json()["code"] == "too_large"
    # Nothing was written.
    assert staged_uploads() == []


def test_streamed_length_enforced_without_content_length(client, headers, monkeypatch):
    """A chunked body (no Content-Length) is still capped while streaming."""
    monkeypatch.setenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", "1000")

    def chunks():
        for _ in range(50):
            yield b"y" * 100  # 5000 bytes total

    resp = client.post(
        "/ui/uploads", content=chunks(),
        headers={**headers, "x-textflowkit-filename": "a.bin",
                 "content-type": "application/octet-stream"},
    )
    assert resp.status_code == 413
    assert staged_uploads() == []


def test_empty_upload_refused_and_no_file_left(client, headers):
    resp = upload(client, headers, b"")
    assert resp.status_code == 400
    assert staged_uploads() == []


def test_hostile_extension_is_dropped(client, headers):
    resp = upload(client, headers, b"data", filename="evil.p/../../x")
    assert resp.status_code == 200
    name = resp.json()["upload_id"]
    # Only a safe, single suffix character set survives; no path separators.
    assert "/" not in name and "\\" not in name
    assert name.count(".") <= 1


def test_owned_path_is_generated_not_caller_chosen(client, headers):
    """Two uploads with the same filename get distinct, server-generated names."""
    a = upload(client, headers, b"one", filename="same.mp3").json()["upload_id"]
    b = upload(client, headers, b"two", filename="same.mp3").json()["upload_id"]
    assert a != b
    assert (paths.default_uploads_dir() / a).read_bytes() == b"one"
    assert (paths.default_uploads_dir() / b).read_bytes() == b"two"


def test_max_upload_bytes_env_parsing(monkeypatch):
    monkeypatch.setenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", "4096")
    assert paths.max_upload_bytes() == 4096
    monkeypatch.setenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", "not-a-number")
    with pytest.raises(ValueError):
        paths.max_upload_bytes()
    monkeypatch.setenv("TEXTFLOWKIT_UI_MAX_UPLOAD_BYTES", "0")
    with pytest.raises(ValueError):
        paths.max_upload_bytes()


# --- original-name sidecar (recent-jobs labels) ---------------------------


def test_unicode_filename_round_trips_through_the_header(client, headers):
    """A non-Latin-1 filename is percent-encoded by the UI and decoded here.

    HTTP header values are Latin-1, so ``fetch`` (and httpx) reject a raw
    ``meeting-中文.wav`` before it leaves the browser. The UI sends
    ``encodeURIComponent(file.name)``; the server decodes it, so the staged
    suffix and the display label both carry the real name.
    """
    from urllib.parse import quote

    name = "meeting-中文.wav"
    body = upload(client, headers, b"RIFF" + b"\x00" * 40, filename=quote(name, safe="")).json()
    # The staged suffix comes from the decoded name (".wav"), not a stray "%".
    assert body["upload_id"].endswith(".wav")
    staged = paths.default_uploads_dir() / body["upload_id"]
    meta = uploads.read_upload_meta(staged)
    assert meta["name"] == name
    assert uploads.original_name_for_source(str(staged)) == name


def test_legacy_plain_ascii_filename_is_unchanged(client, headers):
    """A plain name (no percent-escapes) passes through decoding untouched."""
    body = upload(client, headers, b"data", filename="Team Standup.m4a").json()
    staged = paths.default_uploads_dir() / body["upload_id"]
    assert uploads.read_upload_meta(staged)["name"] == "Team Standup.m4a"


def test_upload_keeps_original_name_in_a_sidecar(client, headers):
    """The staged file is a random token; the original name is kept beside it."""
    body = upload(client, headers, b"RIFF" + b"\x00" * 40, filename="Team Standup.m4a").json()
    staged = paths.default_uploads_dir() / body["upload_id"]
    meta = uploads.read_upload_meta(staged)
    assert meta["name"] == "Team Standup.m4a"
    assert meta["bytes"] == 44


def test_original_name_survives_restart(client, headers):
    """The label is durable: it is read from disk, not held in memory."""
    body = upload(client, headers, b"x" * 10, filename="meeting.mp3").json()
    staged = paths.default_uploads_dir() / body["upload_id"]
    # A fresh lookup (as a restarted process would do) still finds the name.
    assert uploads.original_name_for_source(str(staged)) == "meeting.mp3"


def test_original_name_for_source_rejects_non_uploads(tmp_path):
    """A URL, a plain path, or a file outside the uploads dir yields no name."""
    assert uploads.original_name_for_source("https://example.com/a.mp3") is None
    assert uploads.original_name_for_source("C:/somewhere/else/clip.wav") is None
    assert uploads.original_name_for_source("") is None
    assert uploads.original_name_for_source(None) is None


def test_hostile_original_name_is_a_label_not_a_path(client, headers):
    """A traversal in the header is stripped to a bare label; the staged name is
    still server-generated and the label is never used as a path."""
    body = upload(client, headers, b"data", filename="../../etc/passwd").json()
    staged = paths.default_uploads_dir() / body["upload_id"]
    name = uploads.read_upload_meta(staged)["name"]
    assert "/" not in name and "\\" not in name
    assert name == "passwd"
