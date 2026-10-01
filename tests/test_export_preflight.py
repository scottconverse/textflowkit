"""G3C RED: the export surfaces must preflight like the shared render path.

`textflowkit.render._render_requested` is the one place that renders *every*
requested format into memory, validates the normalized set (unknown and
duplicate formats refused before anything is rendered), and then enforces the
aggregate output-byte limit over the whole batch before a single destination is
touched. `write_all` and `ensure_outputs` both go through it.

The two adapter export doors do not. Each renders and writes **one format at a
time**:

    HTTP  POST /jobs/{id}/export     (`adapters/http_server.py`)
    MCP   export_transcript          (`adapters/mcp_server.py`)

with `[(f, render_bytes(tr, f, ...)) for f in fmt_list]` followed by a
per-file `atomic_write_bytes(..., replace=True)`. That has three consequences
these tests pin down as the *desired* behaviour (hence RED - they fail against
the current product and are the specification for the shared preflight):

1. **No aggregate limit.** The per-request output-byte limit is only enforced
   per file, inside `atomic_write_bytes` (`enforce_output_limit(len(data))`).
   Two files that each fit the budget but together exceed it are both written.
2. **Partial publication.** With the per-file check only, a batch whose later
   file is over budget fails *after* earlier files were already replaced -
   `replace=True` means an existing file is clobbered on the way to the failure.
3. **Unmapped failure.** A `ValueError`/`ImportError` from rendering, and the
   `ServiceConfigurationError` from the limit, are not mapped to the surfaces'
   error contract (HTTP 422, MCP `{"error": ...}`); the limit one currently
   escapes as an unhandled 500 / raised exception.

The tests below assert the aggregate-limit refusals and the error mapping using
a monkeypatched `render_bytes` that returns controlled byte counts. That is
deliberate: the *real* `enforce_output_limit` still runs against the *real*
configured budget - only the renderer is swapped so a test can shape a batch
whose parts fit and whose sum does not, without depending on the byte size of
any real encoding. Publication is never bypassed: the surfaces still call the
real `atomic_write_bytes`, which is what these tests check leaves the disk
untouched.

Nothing here claims transactional rollback for a *filesystem* failure
mid-publish (a full disk, a permission error on the third of five files). The
claim is narrower and exact: the aggregate-limit refusal, and the render /
validation failures, all happen **before** the first destination is written, so
for those cases no file is created and no existing file is replaced. Where a
limit is enforced per file today, a later file's limit failure must not have
already overwritten an earlier one - the refusal must precede publication of the
whole batch, which is the aggregate limit.
"""

from __future__ import annotations

import pytest

from textflowkit.core.jobs import (
    JobState,
    get_default_store,
    reset_default_store,
)

# The developer HTTP app refuses a peer it cannot judge, and `TestClient`'s
# default peer is the non-address `testclient`. Every HTTP test here means "a
# local caller", so it declares the loopback peer a real one has. No socket is
# opened. (Mirrors `tests/test_adapters.py`.)
LOCAL_PEER = ("127.0.0.1", 50000)


# --- shared fixtures ------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_store():
    """Leave no cached default store behind, in either environment.

    `reset_default_store` (not `clear`) is used on both sides: under a
    production env a `clear()` here would build and cache a durable store bound
    to a `tmp_path` that pytest then deletes, which is the cross-file leakage
    `test_adapters.py::test_default_store_agrees_with_the_current_environment`
    guards against.
    """
    reset_default_store()
    yield
    reset_default_store()


def _seed_done_job(store, n=4):
    """A finished job holding a small transcript, with no outputs on disk."""
    from textflowkit.core.model import Segment, Transcript

    job = store.create("src")
    tr = Transcript(
        source="src",
        language="en",
        segments=[Segment(i, i + 0.5, f"word{i} text") for i in range(n)],
    )
    store.update(job.id, state=JobState.DONE, transcript=tr.to_dict(), outputs=[])
    return job


@pytest.fixture
def production_output_budget(monkeypatch, tmp_path):
    """Production limits with a tiny output budget and a durable store.

    `enforce_output_limit` only enforces under the production profile, so the
    aggregate-limit tests run there. The HTTP middleware also calls
    `validate_production_config`, which needs the token, roots, and DB the
    profile requires; all of them point inside `tmp_path`.
    """
    from textflowkit.adapters import http_server
    from textflowkit.core.executor import reset_default_executor

    reset_default_executor()
    reset_default_store()
    input_root = tmp_path / "input"
    input_root.mkdir()
    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    monkeypatch.setenv("TEXTFLOWKIT_API_TOKEN", "a-long-test-token-12345")
    monkeypatch.setenv("TEXTFLOWKIT_INPUT_ROOT", str(input_root))
    monkeypatch.setenv("TEXTFLOWKIT_OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("TEXTFLOWKIT_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(tmp_path / "jobs.db"))
    # The budget every "parts fit, sum does not" test is built against.
    monkeypatch.setenv("TEXTFLOWKIT_MAX_OUTPUT_BYTES", "10")
    http_server._RATE_BUCKETS.clear()
    monkeypatch.setattr(http_server, "_next_expiry", float("inf"), raising=False)
    yield tmp_path
    reset_default_executor()
    reset_default_store()
    http_server._RATE_BUCKETS.clear()


def _patch_render_sizes(monkeypatch, sizes: dict[str, int]):
    """Make `render_bytes` return `sizes[fmt]` bytes, real limit still enforced.

    The renderer is the only thing replaced. `len()` of the returned batch is
    what `enforce_output_limit` sees, so the production byte budget is still the
    real one from the environment.
    """
    from textflowkit.adapters import http_server as http_mod
    from textflowkit.adapters import mcp_server as mcp_mod

    def fake_render_bytes(transcript, fmt, *, title=None):
        fmt = fmt.lower().lstrip(".")
        return b"x" * sizes[fmt]

    monkeypatch.setattr(http_mod, "render_bytes", fake_render_bytes)
    monkeypatch.setattr(mcp_mod, "render_bytes", fake_render_bytes)


def _http_client():
    from fastapi.testclient import TestClient

    from textflowkit.adapters.http_server import app

    return TestClient(app, base_url="http://127.0.0.1", client=LOCAL_PEER)


def _auth_headers():
    return {"Authorization": "Bearer a-long-test-token-12345"}


# --- HTTP: aggregate output-byte limit refused before publication ----------

def test_http_export_refuses_aggregate_over_budget_before_writing(
    production_output_budget, monkeypatch
):
    """Two files that fit the budget individually, whose sum does not: 422.

    Budget is 10 bytes. Each of three formats renders to 4 bytes (under budget),
    the batch sums to 12 (over). The per-file `enforce_output_limit` inside
    `atomic_write_bytes` never fires, so today every file is written; the batch
    limit must refuse the whole request first.
    """
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    _patch_render_sizes(monkeypatch, {"srt": 4, "txt": 4, "vtt": 4})
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "txt", "vtt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert not (out_dir / f"{job.id}.srt").exists()
    assert not (out_dir / f"{job.id}.txt").exists()
    assert not (out_dir / f"{job.id}.vtt").exists()


def test_http_export_aggregate_over_budget_replaces_no_existing_file(
    production_output_budget, monkeypatch
):
    """An already-published file must survive an over-budget batch.

    The export writes with `replace=True`, so a batch that is refused only
    *after* an earlier file was published would silently change a file the
    caller never asked to change. Seed the destination with known bytes; a
    refused aggregate must leave it exactly as it was.
    """
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    job = _seed_done_job(get_default_store())

    prior = out_dir / f"{job.id}.srt"
    prior.write_bytes(b"ORIGINAL")

    _patch_render_sizes(monkeypatch, {"srt": 4, "txt": 4, "vtt": 4})
    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "txt", "vtt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert prior.read_bytes() == b"ORIGINAL", "a refused batch replaced an existing file"


def test_http_export_under_budget_is_allowed(production_output_budget, monkeypatch):
    """Control: a batch within the aggregate budget is written, no refusal.

    Same three formats, but each renders to 2 bytes for a 6-byte batch, under
    the 10-byte budget. This must succeed and publish every file - the limit
    must not become a blanket refusal.
    """
    out_root = production_output_budget / "output"
    _patch_render_sizes(monkeypatch, {"srt": 2, "txt": 2, "vtt": 2})

    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "txt", "vtt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    names = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in r.json()["written"]}
    assert names == {f"{job.id}.srt", f"{job.id}.txt", f"{job.id}.vtt"}
    out_dir = out_root / "out"
    for name in names:
        assert (out_dir / name).read_bytes() == b"xx"


# --- HTTP: render / validation failures map to 422 before publication ------

def test_http_export_render_valueerror_maps_to_422(production_output_budget, monkeypatch):
    """A `ValueError` raised while rendering is a 422, not a 500 - and no file."""
    from textflowkit.adapters import http_server as http_mod

    def boom(transcript, fmt, *, title=None):
        raise ValueError("render failed for this transcript")

    monkeypatch.setattr(http_mod, "render_bytes", boom)
    out_root = production_output_budget / "output"
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert "render failed" in r.text
    assert not (out_root / "out" / f"{job.id}.srt").exists()


def test_http_export_missing_dependency_importerror_maps_to_422(
    production_output_budget, monkeypatch
):
    """A missing export extra surfaces as a 422 the caller can read."""
    from textflowkit.adapters import http_server as http_mod

    def missing(transcript, fmt, *, title=None):
        raise ImportError("The 'export' extra is required: pip install 'textflowkit[export]'")

    monkeypatch.setattr(http_mod, "render_bytes", missing)
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["docx"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert "export" in r.text


def test_http_export_unknown_format_rejected_before_rendering(
    production_output_budget, monkeypatch
):
    """An unknown format is refused before any render or publication."""
    calls: list[str] = []
    from textflowkit.adapters import http_server as http_mod

    def recording(transcript, fmt, *, title=None):
        calls.append(fmt)
        return b"x"

    monkeypatch.setattr(http_mod, "render_bytes", recording)
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "xyzzy"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert "xyzzy" in r.text
    assert calls == [], "rendering ran before the unknown format was rejected"


def test_http_export_duplicate_format_rejected_before_rendering(
    production_output_budget, monkeypatch
):
    """A duplicate format is refused before any render or publication."""
    calls: list[str] = []
    from textflowkit.adapters import http_server as http_mod

    def recording(transcript, fmt, *, title=None):
        calls.append(fmt)
        return b"x"

    monkeypatch.setattr(http_mod, "render_bytes", recording)
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt", "srt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert calls == [], "rendering ran before the duplicate format was rejected"


# --- HTTP: defaults, empty, explicit replacement --------------------------

def test_http_export_omitted_formats_uses_the_declared_default(
    production_output_budget, monkeypatch
):
    """No `formats` writes exactly the format set the HTTP door declares.

    That set is the adapter's own wire default (`srt, vtt, txt, json`), which is
    deliberately spelled at the door and is *not* the shared `DEFAULT_FORMATS`
    the submission path records. The shared-preflight work must not silently
    change this wire contract, so the default is pinned here as the set the
    endpoint documents.
    """
    http_default = ("srt", "vtt", "txt", "json")
    _patch_render_sizes(monkeypatch, {fmt: 1 for fmt in http_default})
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    names = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in r.json()["written"]}
    assert names == {f"{job.id}.{fmt}" for fmt in http_default}


def test_http_export_empty_formats_uses_the_declared_default(
    production_output_budget, monkeypatch
):
    """An explicitly empty `formats` means the HTTP door's default set."""
    http_default = ("srt", "vtt", "txt", "json")
    _patch_render_sizes(monkeypatch, {fmt: 1 for fmt in http_default})
    job = _seed_done_job(get_default_store())

    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": [], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    names = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in r.json()["written"]}
    assert names == {f"{job.id}.{fmt}" for fmt in http_default}


def test_http_export_explicitly_replaces_an_existing_file(
    production_output_budget, monkeypatch
):
    """Control: a successful export replaces the destination, by contract.

    The refusal on a limit failure must not be confused with the normal replace
    semantics: when the batch is allowed, `replace=True` overwrites the file.
    """
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    job = _seed_done_job(get_default_store())
    target = out_dir / f"{job.id}.srt"
    target.write_bytes(b"OLD")

    _patch_render_sizes(monkeypatch, {"srt": 3})
    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    assert target.read_bytes() == b"xxx"


def test_http_export_render_failure_replaces_no_existing_file(
    production_output_budget, monkeypatch
):
    """A render failure must not have overwritten an already-published file."""
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    job = _seed_done_job(get_default_store())
    prior = out_dir / f"{job.id}.srt"
    prior.write_bytes(b"ORIGINAL")

    from textflowkit.adapters import http_server as http_mod

    def boom(transcript, fmt, *, title=None):
        raise ValueError("render failed for this transcript")

    monkeypatch.setattr(http_mod, "render_bytes", boom)
    r = _http_client().post(
        f"/jobs/{job.id}/export",
        params={"formats": ["srt"], "output_dir": "out"},
        headers=_auth_headers(),
    )

    assert r.status_code == 422, r.text
    assert prior.read_bytes() == b"ORIGINAL"


# --- MCP: the same contract over the tool surface --------------------------

def _call_export_transcript(job_id, output_dir, formats):
    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import export_transcript

    return export_transcript(job_id, output_dir=output_dir, formats=formats)


def test_mcp_export_refuses_aggregate_over_budget_before_writing(
    production_output_budget, monkeypatch
):
    """MCP aggregate over budget is a structured error, and no file is created.

    The MCP surface never raises `ServiceConfigurationError` at the caller; its
    contract is `{"error": ...}`. Today the per-file limit inside
    `atomic_write_bytes` escapes as a raised exception once the *second* file is
    attempted - after the first was already written.
    """
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    _patch_render_sizes(monkeypatch, {"srt": 4, "txt": 4, "vtt": 4})
    job = _seed_done_job(get_default_store())

    result = _call_export_transcript(job.id, "out", "srt,txt,vtt")

    assert isinstance(result, dict) and "error" in result, result
    assert "written" not in result
    assert not (out_dir / f"{job.id}.srt").exists()
    assert not (out_dir / f"{job.id}.txt").exists()
    assert not (out_dir / f"{job.id}.vtt").exists()


def test_mcp_export_aggregate_over_budget_replaces_no_existing_file(
    production_output_budget, monkeypatch
):
    """An already-published MCP export file survives an over-budget batch."""
    out_root = production_output_budget / "output"
    out_dir = out_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    job = _seed_done_job(get_default_store())
    prior = out_dir / f"{job.id}.srt"
    prior.write_bytes(b"ORIGINAL")

    _patch_render_sizes(monkeypatch, {"srt": 4, "txt": 4, "vtt": 4})
    result = _call_export_transcript(job.id, "out", "srt,txt,vtt")

    assert isinstance(result, dict) and "error" in result, result
    assert prior.read_bytes() == b"ORIGINAL"


def test_mcp_export_under_budget_is_allowed(production_output_budget, monkeypatch):
    """Control: an MCP batch within budget writes every file."""
    _patch_render_sizes(monkeypatch, {"srt": 2, "txt": 2, "vtt": 2})
    job = _seed_done_job(get_default_store())

    result = _call_export_transcript(job.id, "out", "srt,txt,vtt")

    assert "error" not in result, result
    names = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in result["written"]}
    assert names == {f"{job.id}.srt", f"{job.id}.txt", f"{job.id}.vtt"}


def test_mcp_export_render_valueerror_is_a_structured_error(
    production_output_budget, monkeypatch
):
    """A render `ValueError` becomes `{"error": ...}`, never a raise."""
    from textflowkit.adapters import mcp_server as mcp_mod

    def boom(transcript, fmt, *, title=None):
        raise ValueError("render failed for this transcript")

    monkeypatch.setattr(mcp_mod, "render_bytes", boom)
    job = _seed_done_job(get_default_store())

    result = _call_export_transcript(job.id, "out", "srt")

    assert "error" in result and "render failed" in result["error"]


def test_mcp_export_missing_dependency_is_a_structured_error(
    production_output_budget, monkeypatch
):
    """A missing DOCX/PDF extra is a structured error the caller can act on.

    The binary formats are export-only and their dependency is optional; asking
    for one without the extra must return `{"error": ...}` naming the install,
    not raise an `ImportError` out of the tool.
    """
    from textflowkit.adapters import mcp_server as mcp_mod

    def missing(transcript, fmt, *, title=None):
        raise ImportError("The 'export' extra is required: pip install 'textflowkit[export]'")

    monkeypatch.setattr(mcp_mod, "render_bytes", missing)
    job = _seed_done_job(get_default_store())

    result = _call_export_transcript(job.id, "out", "docx")

    assert "error" in result
    assert "export" in result["error"]


def test_mcp_export_duplicate_format_is_a_structured_error(production_output_budget):
    """A duplicate format is a structured error, before any render."""
    job = _seed_done_job(get_default_store())
    result = _call_export_transcript(job.id, "out", "srt,srt")
    assert "error" in result
    assert "duplicate" in result["error"]


def test_mcp_export_unknown_format_is_a_structured_error(production_output_budget):
    """An unknown format is a structured error, before any render."""
    job = _seed_done_job(get_default_store())
    result = _call_export_transcript(job.id, "out", "srt,xyzzy")
    assert "error" in result
    assert "xyzzy" in result["error"]


def test_mcp_export_omitted_formats_uses_the_declared_default(
    production_output_budget, monkeypatch
):
    """The MCP default format string writes the set the tool documents.

    The MCP door spells its own default (`srt,vtt,txt,json`), which is *not* the
    shared `DEFAULT_FORMATS` the submission path records. The shared-preflight
    work must not silently change this wire contract, so the default is pinned
    here as the set the tool documents.
    """
    mcp_default = ("srt", "vtt", "txt", "json")
    _patch_render_sizes(monkeypatch, {fmt: 1 for fmt in mcp_default})
    job = _seed_done_job(get_default_store())

    pytest.importorskip("mcp")
    from textflowkit.adapters.mcp_server import export_transcript

    result = export_transcript(job.id, output_dir="out")
    assert "error" not in result, result
    names = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in result["written"]}
    assert names == {f"{job.id}.{fmt}" for fmt in mcp_default}
