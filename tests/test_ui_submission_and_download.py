"""UI submission goes through the shared contract; downloads use a fixed set.

The UI must not reimplement the pipeline. These tests prove the UI's job routes
call the *same* ``core.submission`` the CLI/MCP/HTTP doors call - by stubbing the
runner and observing the recorded request - and that a download can only produce
one of a fixed allowlist of formats, with a correct content type and a filename
built from the job id (never from caller input).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from textflowkit.core.jobs import JobState, get_default_store
from textflowkit.ui.app import create_app

LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "out"))
    from textflowkit.core.executor import reset_default_executor
    from textflowkit.core.jobs import reset_default_store
    from textflowkit.core.startup import reset_startup_recovery

    reset_default_store()
    reset_default_executor()
    reset_startup_recovery()
    yield
    reset_default_store()
    reset_default_executor()
    reset_startup_recovery()


@pytest.fixture
def inline_submission(monkeypatch):
    """Run submissions inline and record each request; no media, no model."""
    from textflowkit.core import submission
    from textflowkit.core.model import Transcript

    ran: list[dict] = []

    def record(job, store, **kwargs):
        ran.append(dict(job.request or {}))
        store.update(
            job.id, state=JobState.DONE, progress="complete",
            transcript=Transcript(source=job.source, segments=[]).to_dict(),
        )

    monkeypatch.setattr(submission, "run_job", record)
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)
    return ran


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


def test_url_submission_records_shared_request(client, headers, inline_submission):
    resp = client.post(
        "/ui/jobs",
        json={"mode": "source", "source": "https://example.com/a.mp3",
              "formats": ["txt", "srt"], "language": "en"},
        headers=headers,
    )
    assert resp.status_code == 202
    assert len(inline_submission) == 1
    req = inline_submission[0]
    assert req["source"] == "https://example.com/a.mp3"
    assert req["formats"] == ["txt", "srt"]
    assert req["language"] == "en"
    assert req["engine"] == "whistle"  # default engine resolved by the contract


def test_defaults_are_whistle_auto_language(client, headers, inline_submission):
    client.post("/ui/jobs", json={"mode": "source", "source": "https://example.com/a.mp3"},
                headers=headers)
    req = inline_submission[0]
    assert req["engine"] == "whistle"
    assert req["language"] is None
    assert set(req["formats"]) == {"json", "srt", "txt"}


def test_upload_submission_uses_staged_path(client, headers, inline_submission, tmp_path):
    up = client.post(
        "/ui/uploads", content=b"RIFF" + b"\x00" * 50,
        headers={**headers, "x-textflowkit-filename": "a.wav",
                 "content-type": "application/octet-stream"},
    ).json()
    resp = client.post(
        "/ui/jobs",
        json={"mode": "upload", "upload_id": up["upload_id"], "formats": ["txt"]},
        headers=headers,
    )
    assert resp.status_code == 202
    assert inline_submission[0]["source"] == up["path"]


def test_recent_list_shows_original_upload_name(client, headers, inline_submission):
    """The recent-jobs label is the original filename, not the opaque staged path."""
    up = client.post(
        "/ui/uploads", content=b"RIFF" + b"\x00" * 50,
        headers={**headers, "x-textflowkit-filename": "Board Meeting.m4a",
                 "content-type": "application/octet-stream"},
    ).json()
    client.post(
        "/ui/jobs",
        json={"mode": "upload", "upload_id": up["upload_id"], "formats": ["txt"]},
        headers=headers,
    )
    row = client.get("/ui/jobs?limit=5").json()["jobs"][0]
    # `source` is unchanged (the staged path); `display_source` is the friendly name.
    assert row["source"] == up["path"]
    assert row["display_source"] == "Board Meeting.m4a"
    assert up["upload_id"] not in row["display_source"]


def test_recent_list_shows_url_and_basename_for_other_sources(client, headers, inline_submission):
    client.post("/ui/jobs", json={"mode": "source", "source": "https://example.com/a.mp3"},
                headers=headers)
    row = client.get("/ui/jobs?limit=5").json()["jobs"][0]
    assert row["display_source"] == "https://example.com/a.mp3"


def test_upload_id_traversal_refused(client, headers):
    resp = client.post(
        "/ui/jobs",
        json={"mode": "upload", "upload_id": "../../../Windows/System32/x.dll"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "staged upload" in resp.json()["error"]


def test_cookies_from_browser_refused(client, headers):
    resp = client.post(
        "/ui/jobs",
        json={"mode": "source", "source": "https://example.com/a.mp3",
              "cookies_from_browser": "firefox"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_bad_engine_refused_before_queue(client, headers, inline_submission):
    resp = client.post(
        "/ui/jobs",
        json={"mode": "source", "source": "https://example.com/a.mp3", "engine": "nope"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert inline_submission == []  # nothing was queued


def test_batch_reports_per_item_errors(client, headers, inline_submission):
    resp = client.post(
        "/ui/jobs/batch",
        json={"jobs": [
            {"mode": "source", "source": "https://example.com/a.mp3", "formats": ["txt"]},
            {"mode": "source", "source": "https://example.com/b.mp3", "engine": "bad"},
        ]},
        headers=headers,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["count"] == 2
    assert body["jobs"][0]["index"] == 0 and "job_id" in body["jobs"][0]
    assert body["jobs"][1]["index"] == 1 and "error" in body["jobs"][1]


# --- download --------------------------------------------------------------

def _done_job(client, text="hello world"):
    store = get_default_store()
    job = store.create("local:demo")
    store.update(job.id, state=JobState.DONE, transcript={
        "source": "demo", "language": "en",
        "segments": [{"start": 0.0, "end": 1.5, "text": text,
                      "speaker": None, "translated_text": None,
                      "hidden": False, "words": []}],
        "platform": None, "duration": None, "engine": "whistle", "metadata": {},
    })
    return job.id


@pytest.mark.parametrize("fmt,ctype", [
    ("txt", "text/plain"),
    ("md", "text/markdown"),
    ("srt", "application/x-subrip"),
    ("vtt", "text/vtt"),
    ("json", "application/json"),
])
def test_download_text_formats(client, fmt, ctype):
    job_id = _done_job(client)
    resp = client.get(f"/ui/jobs/{job_id}/download?format={fmt}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(ctype)
    assert resp.headers["content-disposition"] == f'attachment; filename="{job_id}.{fmt}"'


@pytest.mark.parametrize("fmt,ctype", [
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("pdf", "application/pdf"),
])
def test_download_binary_formats(client, fmt, ctype):
    job_id = _done_job(client)
    resp = client.get(f"/ui/jobs/{job_id}/download?format={fmt}")
    if resp.status_code == 422:
        pytest.skip("optional export extra not installed")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == ctype


def test_download_refuses_unknown_format(client):
    job_id = _done_job(client)
    resp = client.get(f"/ui/jobs/{job_id}/download?format=exe")
    assert resp.status_code == 422
    assert "not downloadable" in resp.json()["error"]


def test_download_refuses_path_shaped_format(client):
    job_id = _done_job(client)
    resp = client.get(f"/ui/jobs/{job_id}/download?format=../../etc/passwd")
    assert resp.status_code == 422


def test_download_unfinished_job_is_409(client):
    store = get_default_store()
    job = store.create("local:demo")  # PENDING
    resp = client.get(f"/ui/jobs/{job.id}/download?format=txt")
    assert resp.status_code == 409


def test_download_missing_job_is_404(client):
    assert client.get("/ui/jobs/nope/download?format=txt").status_code == 404


def test_download_filename_has_no_caller_input(client):
    """The disposition filename is the job id + ext, not anything sent."""
    job_id = _done_job(client)
    resp = client.get(f"/ui/jobs/{job_id}/download?format=txt")
    disp = resp.headers["content-disposition"]
    assert job_id in disp
    assert "\r" not in disp and "\n" not in disp
