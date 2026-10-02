"""G5A RED — MCP status/transcript guidance must stop polling at a terminal state.

The defect this file pins, from the audit's UX-002 (Major):

    A controlled cancelled job returned ``state: cancelled``,
    ``progress: cancelled``, but ``next: Still working. Poll again.`` A read
    request for the same job returned ``job is not finished (state: cancelled)``
    and instructed polling until done. ... A client that trusts ``state`` can
    stop correctly; a client following the supplied MCP instruction may wait
    indefinitely.

Fresh source (read end to end for this pass, ``adapters/mcp_server.py``):

- ``get_job_status`` — DONE gets a "read the transcript" hint, ERROR gets "the
  job failed", and *every other state* — PENDING, RUNNING, **and CANCELLED** —
  falls into the ``else`` that says ``"Still working. Poll again."``
- ``get_transcript`` — every state that is not DONE returns
  ``{"error": "job is not finished (state: ...)", "next": "Poll get_job_status
  until state is 'done'."}``, which is wrong for the terminal ERROR and
  CANCELLED states: there is no further poll that can ever reach DONE.

The policy these tests demand, matching the audit's fix path:

- **pending/running** (active) — keep telling the client to poll again; no
  premature claim that the model can be interrupted.
- **done** — direct retrieval.
- **error** — terminal: stop polling, inspect the error before retrying.
- **cancelled** — terminal: stop polling, and offer the *relevant* explicit
  recovery (resume this job if supported, or resubmit the media). No promise of
  an immediate model interrupt — the audit's cancellation docs say cancellation
  is cooperative at stage boundaries, so the wording must not imply an instant
  stop.

The assertions are deliberately *shape* checks (a guidance field that names the
required action), not frozen-string equality, so the exact copy can be chosen in
the fix. Where a phrase is asserted it is a small, stable keyword the policy
itself names.

These drive the **real** tool functions over an isolated in-memory store, the
same way ``tests/test_job_progress.py`` does for status. No model, no network.
This suite is EDIT-ONLY and RED-first: the CANCELLED/ERROR cases fail against the
current guidance.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the MCP adapter")

from textflowkit.adapters import mcp_server
from textflowkit.core.jobs import JobState, MemoryJobStore
from textflowkit.core.model import Segment, Transcript


@pytest.fixture
def store(monkeypatch):
    """An isolated in-memory store bound to the MCP adapter's store lookup."""
    memory = MemoryJobStore()
    monkeypatch.setattr(mcp_server, "get_default_store", lambda: memory)
    return memory


def _seed(store: MemoryJobStore, state: JobState, *, error: str | None = None):
    job = store.create("https://example.com/x")
    fields = {"state": state, "progress": state.value}
    if error is not None:
        fields["error"] = error
    if state is JobState.DONE:
        fields["transcript"] = Transcript(
            source=job.source, language="en",
            segments=[Segment(0.0, 1.0, "done text")],
        ).to_dict()
    store.update(job.id, **fields)
    return job


def _guidance(payload: dict) -> str:
    """The action text a client would follow: 'next', else 'error'."""
    return str(payload.get("next") or payload.get("error") or "").lower()


# --- get_job_status ----------------------------------------------------------


def test_status_pending_says_poll_again(store):
    """Active work: the client is correctly told to keep polling."""
    job = _seed(store, JobState.PENDING)

    payload = mcp_server.get_job_status(job.id)

    assert payload["state"] == "pending"
    assert "poll" in _guidance(payload)


def test_status_running_says_poll_again(store):
    job = _seed(store, JobState.RUNNING)

    payload = mcp_server.get_job_status(job.id)

    assert payload["state"] == "running"
    assert "poll" in _guidance(payload)


def test_status_done_directs_retrieval_not_polling(store):
    """Terminal success: read the transcript; do not keep polling."""
    job = _seed(store, JobState.DONE)

    payload = mcp_server.get_job_status(job.id)

    assert payload["state"] == "done"
    guidance = _guidance(payload)
    assert "get_transcript" in guidance or "read" in guidance
    assert "poll again" not in guidance


def test_status_cancelled_stops_polling(store):
    """The headline UX-002 defect: a cancelled job must not say 'still working'."""
    job = _seed(store, JobState.CANCELLED)

    payload = mcp_server.get_job_status(job.id)

    assert payload["state"] == "cancelled"
    guidance = _guidance(payload)
    assert "still working" not in guidance, (
        f"a terminally cancelled job still tells the client to keep polling: {payload!r}"
    )
    assert "poll again" not in guidance, payload
    assert "cancel" in guidance


def test_status_cancelled_offers_an_explicit_recovery(store):
    """Cancellation is terminal, so the guidance must name what to do next.

    Resume-if-supported or resubmit is the audit's stated recovery. The test
    accepts either, and does not demand an instant-interrupt promise.
    """
    job = _seed(store, JobState.CANCELLED)

    guidance = _guidance(mcp_server.get_job_status(job.id))

    assert "resume" in guidance or "resubmit" in guidance or "submit" in guidance, (
        f"a cancelled job offers no explicit recovery action: {guidance!r}"
    )


def test_status_error_stops_polling_and_points_at_the_error(store):
    """A failed job is terminal too: stop polling and inspect the error."""
    job = _seed(store, JobState.ERROR, error="transcription failed: boom")

    payload = mcp_server.get_job_status(job.id)

    assert payload["state"] == "error"
    guidance = _guidance(payload)
    assert "still working" not in guidance, payload
    assert "poll again" not in guidance, payload


# --- get_transcript ----------------------------------------------------------


def test_transcript_cancelled_does_not_instruct_polling_until_done(store):
    """Reading a cancelled job must not advise polling until state is done."""
    job = _seed(store, JobState.CANCELLED)

    payload = mcp_server.get_transcript(job.id)

    assert "error" in payload, payload
    guidance = _guidance(payload)
    assert "poll get_job_status until state is 'done'" not in guidance, (
        f"a cancelled read still tells the client to poll until done: {payload!r}"
    )
    assert "cancel" in guidance


def test_transcript_error_does_not_instruct_polling_until_done(store):
    """A terminally failed job cannot reach done by polling; say so."""
    job = _seed(store, JobState.ERROR, error="transcription failed: boom")

    payload = mcp_server.get_transcript(job.id)

    assert "error" in payload, payload
    guidance = _guidance(payload)
    assert "poll get_job_status until state is 'done'" not in guidance, payload
    assert "fail" in guidance or "error" in guidance


def test_transcript_pending_still_advises_polling(store):
    """Active work keeps the poll-again guidance — not every state is terminal."""
    job = _seed(store, JobState.PENDING)

    payload = mcp_server.get_transcript(job.id)

    assert "error" in payload, payload
    assert "poll" in _guidance(payload)


def test_transcript_done_returns_content_not_an_error(store):
    """Control: a done job reads normally; the guard must not over-reach."""
    job = _seed(store, JobState.DONE)

    payload = mcp_server.get_transcript(job.id)

    assert "error" not in payload, payload
    assert "done text" in payload["content"]


# --- the submission hints do not promise an instant interrupt ----------------


def test_transcribe_media_hint_does_not_promise_an_immediate_interrupt(store):
    """The submission 'next' must not claim the model stops the moment you ask.

    Cancellation is documented as cooperative at stage boundaries, so the copy
    must not tell a client the run halts instantly.
    """
    import inspect

    source = inspect.getsource(mcp_server.transcribe_media)
    lowered = source.lower()
    for claim in ("immediately stops", "stops immediately", "instantly"):
        assert claim not in lowered, f"transcribe_media promises {claim!r}"
