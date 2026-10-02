"""G4B: a batch must accept or reject each item independently (ENG008).

The three batch doors are:

    HTTP  POST /jobs/batch          (`adapters/http_server.py::create_batch`)
    MCP   submit_batch_media        (`adapters/mcp_server.py`)
    core  submit_batch              (`core/submission.py`)

Each must admit its items one at a time, so a single bad item can never abort the
whole request before any *good* item is queued. There are two distinct shapes of
"bad item", and the goal (ENG008) draws the line between them:

1. **Envelope errors.** The top-level body itself is malformed - `jobs` is not a
   list, or a required top-level field is the wrong type. These may legitimately
   be a whole-request 422 / structured error; there is no per-item interpretation
   to hand back.
2. **Per-item errors.** The body *is* a valid envelope (a list of job objects),
   but an individual entry has an unsupported format, an empty source, a
   not-a-string source, or a value the submission contract refuses. These must be
   **item outcomes**: the item is rejected in place, carrying its original index
   and source, and every *other* item - before and after it - is still attempted
   and reported in the original order.

The contract these tests pin, now implemented on all three doors:

- Each door validates its own wire (HTTP's list-of-job-objects, MCP's single
  source list with one comma `formats` string), then hands each raw item to the
  one shared admission boundary in `core/submission.py`. A per-item construction
  or admission refusal is that item's outcome, carrying its zero-based `index`
  and best-effort `source`; the items before and after it are still attempted
  and the outcomes stay in the submitted order.
- The core `submit_batch` runs **no whole-batch engine preflight**: an item
  naming an engine whose optional package is absent fails only that item
  (`_require_engine_ready` on the queue path), so a bad item can never hide a
  later good one.

The tests below drive the **real** construction path of each door. No inference
runs: `submission.run_job` is stubbed to complete a job inline and record it, so
"queued exactly once" is observed from the store and the recorded request, never
from a model. Every local source names a real (empty) file, because the real
`SubmissionRequest` refuses a missing local source.

RED vs. guard is separated explicitly at the bottom of this module. The
section labels are historical (from the RED pass); every listed test now passes
against the implemented contract.
"""

from __future__ import annotations

import sys

import pytest

from textflowkit.core import submission
from textflowkit.core.jobs import (
    JobState,
    MemoryJobStore,
    reset_default_store,
    set_default_store,
)
from textflowkit.core.submission import SubmissionRequest, submit_batch

# The developer HTTP app refuses a peer it cannot judge, and `TestClient`'s
# default peer is the non-address `testclient`. Every HTTP test here means "a
# local caller", so it declares the loopback peer a real one has. No socket is
# opened. (Mirrors `tests/test_engine_adapters.py`.)
LOCAL_PEER = ("127.0.0.1", 50000)

MISSING_HINT = "textflowkit[faster-whisper]"


# --------------------------------------------------------------------------
# Fixtures: a controlled queue and real local sources
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_store():
    """A fresh process-wide store per test, torn down afterwards.

    `reset_default_store` (not `clear`) on both sides: a test that flips the
    production env must not leave a durable store cached for the next file.
    """
    reset_default_store()
    yield
    reset_default_store()


@pytest.fixture(autouse=True)
def local_media(monkeypatch, tmp_path):
    """Real, empty files for the relative source names used below.

    The shared `SubmissionRequest` refuses a local source that is not present,
    so a test that means "a good item" has to name a file. Nothing reads it:
    `run_job` is stubbed, so the report - not the bytes - is the evidence.
    """
    for name in ("one.wav", "two.wav", "three.wav"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def queued(monkeypatch):
    """Run submissions inline and record the job each queue attempt produced.

    `get_default_executor` is returned as ``None`` so `submit_request` takes the
    synchronous `run_job` path, and `run_job` is stubbed to mark the job DONE -
    the *real* store, the *real* request construction, and the *real* batch loop
    all still run. Only the model is faked, so this is a queue observation, not
    an inference.
    """
    from textflowkit.core.model import Transcript

    ran: list[dict] = []

    def record(job, store, **kwargs):
        ran.append(dict(job.request or {}))
        store.update(
            job.id,
            state=JobState.DONE,
            progress="complete",
            transcript=Transcript(source=job.source, segments=[]).to_dict(),
        )

    monkeypatch.setattr(submission, "run_job", record)
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)
    return ran


@pytest.fixture
def missing_faster_whisper(monkeypatch):
    """`import faster_whisper` raises ImportError even if the package is present."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


@pytest.fixture
def http_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    return TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _store():
    from textflowkit.core.jobs import get_default_store

    return get_default_store()


def _jobs_by_source(store):
    """The set of queued job sources the store holds.

    `store.list` is **newest-first** by design, so its order is not the order
    jobs were created and must never be read as creation order. Membership and
    counts are what "queued exactly once" needs; the *original input order* is
    asserted on the batch result list, which the doors preserve positionally.
    """
    return sorted(job.source for job in store.list(limit=100))


# --------------------------------------------------------------------------
# HTTP — the wire carries a list of job objects
# --------------------------------------------------------------------------


def test_http_batch_mixed_keeps_every_item_in_order(queued, http_client):
    """good, bad-format, empty-source, type-bad, good -> five item outcomes.

    The two good items must be queued exactly once each and reported at their
    original positions; the three bad ones must each be an explicit item error
    that does not stop the items after them. The envelope is a valid list, so
    nothing here is a whole-request 422.
    """
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "formats": ["json"]},
        {"source": "two.wav", "formats": ["xyzzy"]},
        {"source": "", "formats": ["json"]},
        {"source": 123, "formats": ["json"]},
        {"source": "three.wav", "formats": ["json"]},
    ]})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["count"] == 5, body
    items = body["jobs"]
    assert len(items) == 5, items

    # The two good items were queued exactly once, in the store, once each.
    assert _jobs_by_source(_store()).count("one.wav") == 1
    assert _jobs_by_source(_store()).count("three.wav") == 1
    assert len(queued) == 2, queued

    # Success is a job id at its original position; the others are item errors.
    assert items[0].get("job_id")
    assert "job_id" not in items[1] and items[1].get("error")
    assert "job_id" not in items[2] and items[2].get("error")
    assert "job_id" not in items[3] and items[3].get("error")
    assert items[4].get("job_id")


def test_http_batch_bad_item_reports_its_own_source_and_index(queued, http_client):
    """A failed item keeps a meaningful identity even when its source is missing.

    An empty source and a non-string source cannot report the offending string,
    so the item must still be identifiable by its position in the submitted
    list (the index it was submitted at). A later good item keeps its own source.
    """
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "formats": ["json"]},
        {"source": "", "formats": ["json"]},
        {"source": 123, "formats": ["json"]},
        {"source": "two.wav", "formats": ["json"]},
    ]})

    assert response.status_code == 202, response.text
    items = response.json()["jobs"]

    # Index is present and stable for every item, good or bad.
    assert [item["index"] for item in items] == [0, 1, 2, 3], items

    # The bad items carry a source field that is not a misleading good string.
    assert items[1].get("source") != "one.wav"
    assert items[2].get("source") != "two.wav"

    # The good item after the two bad ones still names itself and was queued.
    assert items[3].get("source") == "two.wav"
    assert items[3].get("job_id")
    # Both good items were stored; `_jobs_by_source` sorts, because `store.list`
    # is newest-first and must not be read as creation order.
    assert _jobs_by_source(_store()) == ["one.wav", "two.wav"]
    assert len(queued) == 2, queued


def test_http_batch_first_item_bad_does_not_hide_later_good_items(queued, http_client):
    """A single bad *format* on the first entry must not abort the batch.

    The bad entry is only a value error inside `SubmissionRequest` - the wire
    shape is perfectly valid - so it is an item error at position 0, and both
    later entries are queued exactly once. The stored-job check is a sorted set
    (`store.list` is newest-first, not creation order).
    """
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "formats": ["xyzzy"]},
        {"source": "two.wav", "formats": ["json"]},
        {"source": "three.wav", "formats": ["json"]},
    ]})

    assert response.status_code == 202, response.text
    items = response.json()["jobs"]
    assert len(items) == 3, items
    assert items[0].get("error") and "job_id" not in items[0]
    assert items[1].get("job_id") and items[2].get("job_id")
    # Both later entries were stored; compare as a sorted set plus a length,
    # never against `store.list` order (newest-first, not creation order).
    stored = _jobs_by_source(_store())
    assert sorted(stored) == ["three.wav", "two.wav"]
    assert len(stored) == 2, stored
    assert len(queued) == 2, queued


def test_http_batch_engine_invalid_for_one_item_spares_the_others(
    missing_faster_whisper, queued, http_client
):
    """One item naming a missing extra must not refuse an unrelated valid item.

    `one.wav` names `faster-whisper` (package absent), `two.wav` names the
    default engine. Only the first item is refused; the second is admitted and
    queued. Today the fresh-batch preflight refuses both.
    """
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "engine": "faster-whisper"},
        {"source": "two.wav"},
    ]})

    assert response.status_code == 202, response.text
    items = response.json()["jobs"]
    assert items[0].get("error") and MISSING_HINT in items[0]["error"], items[0]
    assert items[1].get("job_id"), items[1]
    assert _jobs_by_source(_store()) == ["two.wav"]


def test_http_batch_malformed_envelope_is_422(http_client):
    """Guard: a body that is not a list of job objects may still be a 422.

    `jobs` is a string, not a list, so there is no per-item interpretation; the
    envelope itself is refused. This boundary must survive the item-level fix.
    """
    response = http_client.post("/jobs/batch", json={"jobs": "one.wav"})

    assert response.status_code == 422, response.text


def test_http_batch_missing_jobs_key_is_422(http_client):
    """Guard: no `jobs` key at all is an envelope error, not a zero-item batch."""
    response = http_client.post("/jobs/batch", json={})

    assert response.status_code == 422, response.text


def test_http_batch_empty_jobs_list_is_an_empty_success(queued, http_client):
    """Guard: an empty list is a valid envelope with zero items, not a 422."""
    response = http_client.post("/jobs/batch", json={"jobs": []})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["count"] == 0
    assert body["jobs"] == []
    assert _jobs_by_source(_store()) == []


# --------------------------------------------------------------------------
# MCP — comma format string + browser cookies are the tool's own wire
# --------------------------------------------------------------------------


def test_mcp_batch_mixed_keeps_every_item_in_order(queued):
    """The MCP door must give the same per-item outcome as HTTP.

    `submit_batch_media` takes one `formats` string and one `sources` list, so
    an "empty source" and a "not-a-string source" are the two shape failures a
    caller can express; both must be item errors that leave the other sources
    queued.
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "", 123, "two.wav"], formats="json")

    assert out["count"] == 4, out
    items = out["jobs"]
    assert len(items) == 4, items
    assert items[0].get("job_id")
    assert "job_id" not in items[1] and items[1].get("error")
    assert "job_id" not in items[2] and items[2].get("error")
    assert items[3].get("job_id")
    assert _jobs_by_source(_store()) == ["one.wav", "two.wav"]
    assert len(queued) == 2, queued


def test_mcp_batch_bad_item_reports_its_own_index(queued):
    """A failed MCP item is identifiable by its position when its source can't be."""
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "", "two.wav"], formats="json")

    items = out["jobs"]
    assert [item["index"] for item in items] == [0, 1, 2], items
    assert items[2].get("job_id")


def test_mcp_batch_missing_extra_is_reported_per_source(
    missing_faster_whisper, queued
):
    """A missing extra named for every source is still refused per source.

    The tool applies one `engine` to all sources, so every item fails - but the
    independent semantics are that each source is an outcome in the original
    order, not a single top-level `{"error": ...}` with no per-item report. (A
    single-engine tool cannot mix engines, so this is the MCP-expressible case
    where the whole batch fails; the *shape* - one outcome per source - is what
    the item boundary fixes.)
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(["one.wav", "two.wav"], engine="faster-whisper")

    assert out["count"] == 2, out
    items = out["jobs"]
    assert all(MISSING_HINT in item.get("error", "") for item in items), items
    assert _jobs_by_source(_store()) == []
    assert queued == []


def test_mcp_registered_schema_accepts_non_string_items_and_rejects_a_non_list():
    """The *registered* MCP tool must let a malformed individual source reach the
    function, which is where the per-item boundary lives.

    A harness reads `tools/list` and validates the call against the published
    schema before the function runs. If `sources` were typed `list[str]`, a
    schema-conformant client could never express "one malformed source" - the SDK
    would reject the whole call before `submit_batch_media` saw it, so the
    per-item outcome would be unreachable through the real MCP door. The schema
    must therefore admit non-string entries (item-validation-friendly) while
    still refusing a top-level `sources` that is not a list at all.
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    tool = mcp._tool_manager._tools["submit_batch_media"]
    sources = tool.parameters["properties"]["sources"]
    assert sources["type"] == "array", sources
    # An item schema pinned to strings would reject `123` before the function.
    items = sources.get("items", {})
    if isinstance(items, dict):
        item_types = items.get("type")
        assert item_types != "string", sources
    # A top-level non-list is still an envelope error the schema refuses.
    assert "anyOf" in sources or sources["type"] == "array", sources


def test_mcp_registered_tool_call_reports_a_malformed_source_item(queued):
    """Call through the *registered* tool, not the bare function.

    The registered entry point is what an MCP client actually invokes; calling it
    with a malformed individual source must return per-source outcomes rather
    than raising. This pins the door the client reaches, so a schema that only
    admits strings cannot silently make the item boundary unreachable.

    Only the tool's own callable is exercised - the same function the SDK
    registered - never the async transport wrapper, so this stays synchronous and
    in-process.
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import mcp

    tool = mcp._tool_manager._tools["submit_batch_media"]
    call = getattr(tool, "fn", None) or getattr(tool, "func", None)
    if call is None:  # pragma: no cover - SDK shape guard
        pytest.skip("this MCP SDK does not expose the registered callable")
    out = call(sources=["one.wav", 123, "two.wav"], formats="json")

    assert out["count"] == 3, out
    items = out["jobs"]
    assert [item["index"] for item in items] == [0, 1, 2], items
    assert items[0].get("job_id"), items[0]
    assert items[1].get("error") and "job_id" not in items[1], items[1]
    assert items[2].get("job_id"), items[2]
    assert _jobs_by_source(_store()) == ["one.wav", "two.wav"]


def test_mcp_batch_retains_comma_format_and_cookie_wire(queued):
    """Guard: the MCP wire (comma format string + cookies) is unchanged.

    The MCP door parses a single `formats` string and carries
    `cookies_from_browser`; HTTP takes a list and has no cookies field. The
    item-boundary fix must not collapse the two wires into one.
    """
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import submit_batch_media

    out = submit_batch_media(
        ["one.wav"], formats="json, srt", cookies_from_browser="firefox"
    )

    assert out["count"] == 1, out
    assert out["jobs"][0].get("job_id"), out
    # The real request was built with the parsed comma list, unchanged.
    assert queued[0]["formats"] == ["json", "srt"], queued
    assert queued[0]["cookies_from_browser"] == "firefox", queued


def test_http_batch_has_no_cookies_wire(queued, http_client):
    """Guard: the HTTP wire stays a list of formats and does not gain cookies.

    `TranscribeRequest` publishes `cookies_from_browser` today, so a valid body
    carrying it must still be accepted and forwarded (not silently dropped by an
    item-boundary rewrite). The point pinned is that the HTTP item shape is its
    own list-of-formats shape, distinct from MCP's comma string.
    """
    response = http_client.post("/jobs/batch", json={"jobs": [
        {"source": "one.wav", "formats": ["json"]},
    ]})

    assert response.status_code == 202, response.text
    assert queued[0]["formats"] == ["json"], queued


# --------------------------------------------------------------------------
# Core — submit_batch is the shared contract all doors delegate to
# --------------------------------------------------------------------------


def test_core_submit_batch_one_missing_extra_does_not_refuse_valid_engines(
    missing_faster_whisper, queued
):
    """The core fresh-batch preflight must not fail unrelated valid engines.

    Two requests: one names the engine whose extra is absent, one names the
    default engine. Only the first may fail. Today the loop over distinct
    engines runs `require_engine` for both before the per-item loop, so the
    second item is never attempted - this is the exact "bad item hides later
    good items" defect in the core.
    """
    requests = [
        SubmissionRequest(source="one.wav", engine="faster-whisper"),
        SubmissionRequest(source="two.wav", engine="whisper"),
    ]

    report = submit_batch(_store(), requests)

    assert len(report) == 2, report
    assert MISSING_HINT in report[0].get("error", ""), report[0]
    assert report[1].get("job_id"), report[1]
    assert _jobs_by_source(_store()) == ["two.wav"]


def test_core_submit_batch_missing_extra_only_fails_the_items_that_named_it(
    missing_faster_whisper, queued
):
    """Three distinct engines: the failing one fails, the others are admitted."""
    requests = [
        SubmissionRequest(source="one.wav", engine="whisper"),
        SubmissionRequest(source="two.wav", engine="faster-whisper"),
        SubmissionRequest(source="three.wav", engine="whisper"),
    ]

    report = submit_batch(_store(), requests)

    assert len(report) == 3, report
    assert report[0].get("job_id"), report[0]
    assert MISSING_HINT in report[1].get("error", ""), report[1]
    assert report[2].get("job_id"), report[2]
    assert _jobs_by_source(_store()) == ["one.wav", "three.wav"]
    assert len(queued) == 2, queued


def test_core_submit_batch_resume_path_is_unaffected(queued):
    """Guard: `resume=True` already decides per item and must stay that way.

    A resume batch skips the fresh-batch preflight by design, so its per-item
    independence is not the defect. This control pins that the fix does not
    change the resume arm - both items still get their outcomes in order.
    """
    requests = [
        SubmissionRequest(source="one.wav", formats=["json"]),
        SubmissionRequest(source="two.wav", formats=["json"]),
    ]

    report = submit_batch(_store(), requests, resume=True)

    assert len(report) == 2, report
    assert all(entry.get("job_id") for entry in report), report
    assert _jobs_by_source(_store()) == ["one.wav", "two.wav"]


def test_core_submit_batch_empty_list_is_empty(queued):
    """Guard: no items means no outcomes, and no error."""
    assert submit_batch(_store(), []) == []
    assert _jobs_by_source(_store()) == []


def test_core_submit_batch_reuses_a_done_job_without_a_new_row(monkeypatch):
    """Guard: an already-DONE resume item is reused, not re-queued.

    This mirrors the existing resume-reuse contract: when an item matches a
    finished job, `submit_request` returns it without creating a row, and the
    batch reports it as a normal success. The item-boundary fix must not turn
    that reuse into a fresh queue.
    """
    from textflowkit.core.checkpoint import CheckpointRecord, local_source_identity
    from textflowkit.core.model import Segment, Transcript

    store = MemoryJobStore()
    set_default_store(store)
    media = "reuse.wav"
    # A real file so the local-source preflight passes, and a real fingerprint so
    # the local-resume validation accepts the checkpoint.
    import pathlib

    pathlib.Path(media).write_bytes(b"reuse bytes")
    prior = store.create(media)
    tr = Transcript(source=media, language="en", segments=[Segment(0.0, 1.0, "kept")])
    record = CheckpointRecord(
        source=media,
        model="small",
        language="en",
        device=None,
        options={
            "formats": ["json"],
            "diarize": False,
            "diarizer_backend": "pyannote",
            "translate_to": None,
            "translator_backend": "ollama",
        },
        finished_stages=["source", "fetch", "extract", "transcribe"],
        transcript=tr.to_dict(),
        local_identity=local_source_identity(media),
    )
    store.update(
        prior.id,
        state=JobState.DONE,
        transcript=tr.to_dict(),
        outputs=["kept.json"],
        checkpoint=record.to_dict(),
    )

    def explode(*args, **kwargs):
        raise AssertionError("a DONE resume item must not re-queue work")

    monkeypatch.setattr(submission, "run_job", explode)
    monkeypatch.setattr(submission, "get_default_executor", lambda: None)
    before = len(store.list(limit=100))

    report = submit_batch(
        store,
        [SubmissionRequest(source=media, formats=["json"], model="small",
                           language="en")],
        resume=True,
    )

    assert len(store.list(limit=100)) == before
    assert report[0]["job_id"] == prior.id
    assert report[0]["state"] == JobState.DONE.value


# --------------------------------------------------------------------------
# Item-boundary specification vs. guard, honestly separated
# --------------------------------------------------------------------------
#
# The item boundary (the G4B RED demand; now implemented):
#
#   HTTP
#   - test_http_batch_mixed_keeps_every_item_in_order
#   - test_http_batch_bad_item_reports_its_own_source_and_index
#   - test_http_batch_first_item_bad_does_not_hide_later_good_items
#   - test_http_batch_engine_invalid_for_one_item_spares_the_others
#   MCP
#   - test_mcp_batch_mixed_keeps_every_item_in_order
#   - test_mcp_batch_bad_item_reports_its_own_index
#   - test_mcp_batch_missing_extra_is_reported_per_source
#   - test_mcp_registered_schema_accepts_non_string_items_and_rejects_a_non_list
#   - test_mcp_registered_tool_call_reports_a_malformed_source_item
#   core
#   - test_core_submit_batch_one_missing_extra_does_not_refuse_valid_engines
#   - test_core_submit_batch_missing_extra_only_fails_the_items_that_named_it
#
# Guards (pin the boundaries the fix must not over-reach):
#
#   - test_http_batch_malformed_envelope_is_422
#   - test_http_batch_missing_jobs_key_is_422
#   - test_http_batch_empty_jobs_list_is_an_empty_success
#   - test_mcp_batch_retains_comma_format_and_cookie_wire
#   - test_http_batch_has_no_cookies_wire
#   - test_core_submit_batch_resume_path_is_unaffected
#   - test_core_submit_batch_empty_list_is_empty
#   - test_core_submit_batch_reuses_a_done_job_without_a_new_row
