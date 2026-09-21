"""Job model and store behaviour."""

from __future__ import annotations

from textflowkit.core.jobs import JobState, JobStore, get_default_store


def test_create_and_get():
    store = JobStore()
    job = store.create("https://example.com/v")
    assert job.state is JobState.PENDING
    assert store.get(job.id) is job
    assert store.get("nope") is None


def test_update_sets_fields_and_timestamp():
    store = JobStore()
    job = store.create("x")
    before = job.updated_at
    updated = store.update(job.id, state=JobState.RUNNING, progress="working")
    assert updated is not None
    assert updated.state is JobState.RUNNING
    assert updated.progress == "working"
    assert updated.updated_at >= before


def test_update_unknown_job_returns_none():
    assert JobStore().update("missing", state=JobState.DONE) is None


def test_is_terminal():
    store = JobStore()
    job = store.create("x")
    assert not job.is_terminal
    store.update(job.id, state=JobState.DONE)
    assert job.is_terminal


def test_list_newest_first_and_limit():
    store = JobStore()
    ids = [store.create(f"src{i}").id for i in range(5)]
    listed = store.list(limit=3)
    assert [j.id for j in listed] == list(reversed(ids))[:3]


def test_list_filters_by_state():
    store = JobStore()
    a = store.create("a")
    b = store.create("b")
    store.update(a.id, state=JobState.DONE)
    done = store.list(state=JobState.DONE)
    assert [j.id for j in done] == [a.id]
    assert b.id not in [j.id for j in done]


def test_eviction_prefers_terminal_jobs():
    store = JobStore(max_jobs=3)
    jobs = [store.create(f"s{i}") for i in range(3)]
    store.update(jobs[0].id, state=JobState.DONE)
    new = store.create("newest")
    assert store.get(jobs[0].id) is None      # terminal evicted first
    assert store.get(new.id) is new
    assert store.get(jobs[1].id) is not None  # running kept


def test_to_dict_shape():
    store = JobStore()
    job = store.create("src")
    d = job.to_dict()
    for key in ("id", "source", "state", "created_at", "updated_at", "progress", "error", "outputs"):
        assert key in d
    assert "transcript" not in d
    job.transcript = {"source": "x", "segments": []}
    assert "transcript" in job.to_dict(include_transcript=True)


def test_default_store_is_singleton():
    assert get_default_store() is get_default_store()


def test_clear():
    store = JobStore()
    store.create("a")
    store.clear()
    assert store.list() == []


def test_list_order_is_independent_of_clock_resolution():
    """Regression: ordering must not depend on `created_at` granularity.

    On Windows with Python < 3.13, time.time() is coarse enough that jobs
    created in a tight loop share a timestamp. Sorting by that value made
    "newest first" arbitrary. Force the tie explicitly so this fails anywhere
    the ordering depends on wall-clock resolution.
    """
    store = JobStore()
    ids = [store.create(f"src{i}").id for i in range(5)]
    for jid in ids:
        store.update(jid)  # touch updated_at; created_at stays as-is
    # Collapse every created_at to one value - the pathological case.
    frozen = 1_000_000.0
    for jid in ids:
        store._jobs[jid].created_at = frozen

    listed = store.list(limit=3)
    assert [j.id for j in listed] == list(reversed(ids))[:3]


def test_list_order_survives_eviction():
    """After eviction the surviving order must still be newest-first."""
    store = JobStore(max_jobs=3)
    ids = [store.create(f"s{i}").id for i in range(3)]
    store.update(ids[0], state=JobState.DONE)   # terminal -> first to be evicted
    newest = store.create("newest").id

    listed = [j.id for j in store.list()]
    assert store.get(ids[0]) is None
    assert listed[0] == newest
    assert listed == [newest, ids[2], ids[1]]
