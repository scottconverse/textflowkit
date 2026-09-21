"""Job model and store behaviour.

Every contract test runs against both implementations. `MemoryJobStore` is the
fast path and the test double; `SqliteJobStore` is what survives a restart. If
they diverge, a durable deployment would behave differently from the suite.
"""

from __future__ import annotations

import pytest

from textflowkit.core.jobs import (
    ENV_DB,
    INCOMPLETE_STATES,
    JobState,
    MemoryJobStore,
    get_default_store,
    reset_default_store,
    set_default_store,
)
from textflowkit.core.sqlite_store import SqliteJobStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        s = MemoryJobStore()
    else:
        s = SqliteJobStore(tmp_path / "jobs.db")
    yield s
    s.close()


# --- contract, both implementations ---------------------------------------

def test_create_and_get(store):
    job = store.create("https://example.com/v")
    assert job.state is JobState.PENDING
    assert store.get(job.id) is not None
    assert store.get("nope") is None


def test_update_sets_fields_and_timestamp(store):
    job = store.create("x")
    before = job.updated_at
    updated = store.update(job.id, state=JobState.RUNNING, progress="working")
    assert updated is not None
    assert updated.state is JobState.RUNNING
    assert updated.progress == "working"
    assert updated.updated_at >= before


def test_update_unknown_job_returns_none(store):
    assert store.update("missing", state=JobState.DONE) is None


def test_is_terminal(store):
    job = store.create("x")
    assert not job.is_terminal
    store.update(job.id, state=JobState.DONE)
    assert store.get(job.id).is_terminal


def test_list_newest_first_and_limit(store):
    ids = [store.create(f"src{i}").id for i in range(5)]
    listed = store.list(limit=3)
    assert [j.id for j in listed] == list(reversed(ids))[:3]


def test_list_filters_by_state(store):
    a = store.create("a")
    b = store.create("b")
    store.update(a.id, state=JobState.DONE)
    done = store.list(state=JobState.DONE)
    assert [j.id for j in done] == [a.id]
    assert b.id not in [j.id for j in done]


def test_eviction_prefers_terminal_jobs(tmp_path):
    for s in (
        MemoryJobStore(max_jobs=3),
        SqliteJobStore(tmp_path / "evict.db", max_jobs=3),
    ):
        jobs = [s.create(f"s{i}") for i in range(3)]
        s.update(jobs[0].id, state=JobState.DONE)
        new = s.create("newest")
        assert s.get(jobs[0].id) is None      # terminal evicted first
        assert s.get(new.id) is not None
        assert s.get(jobs[1].id) is not None  # running kept
        s.close()


def test_to_dict_shape(store):
    job = store.create("src")
    d = job.to_dict()
    for key in ("id", "source", "state", "created_at", "updated_at", "progress",
                "error", "outputs", "cancel_requested"):
        assert key in d
    assert "transcript" not in d
    job.transcript = {"source": "x", "segments": []}
    assert "transcript" in job.to_dict(include_transcript=True)


def test_clear(store):
    store.create("a")
    store.clear()
    assert store.list() == []


def test_list_order_is_independent_of_clock_resolution(store):
    """Regression: ordering must not depend on `created_at` granularity.

    On Windows with Python < 3.13, time.time() is coarse enough that jobs
    created in a tight loop share a timestamp. Sorting by that value made
    "newest first" arbitrary. Force the tie explicitly so this fails anywhere
    ordering depends on wall-clock resolution.
    """
    ids = [store.create(f"src{i}").id for i in range(5)]
    listed = store.list(limit=3)
    assert [j.id for j in listed] == list(reversed(ids))[:3]


def test_list_order_survives_eviction(tmp_path):
    for s in (
        MemoryJobStore(max_jobs=3),
        SqliteJobStore(tmp_path / "ev2.db", max_jobs=3),
    ):
        ids = [s.create(f"s{i}").id for i in range(3)]
        s.update(ids[0], state=JobState.DONE)   # terminal -> first evicted
        newest = s.create("newest").id
        listed = [j.id for j in s.list()]
        assert s.get(ids[0]) is None
        assert listed[0] == newest
        assert listed == [newest, ids[2], ids[1]]
        s.close()


def test_transcript_and_outputs_round_trip(store):
    """The durable store must survive JSON serialisation of rich fields."""
    job = store.create("src")
    transcript = {
        "source": "src",
        "language": "en",
        "segments": [{"start": 0.0, "end": 1.5, "text": "hi"}],
    }
    store.update(
        job.id,
        state=JobState.DONE,
        transcript=transcript,
        outputs=["/tmp/a.srt", "/tmp/a.vtt"],
    )
    got = store.get(job.id)
    assert got.transcript == transcript
    assert got.outputs == ["/tmp/a.srt", "/tmp/a.vtt"]


# --- reaping orphaned work ------------------------------------------------

def test_reap_incomplete_fails_orphaned_jobs(store):
    a = store.create("a")           # left PENDING
    b = store.create("b")
    store.update(b.id, state=JobState.RUNNING)
    c = store.create("c")
    store.update(c.id, state=JobState.DONE)

    reaped = store.reap_incomplete(reason="interrupted by restart")

    assert reaped == 2
    assert store.get(a.id).state is JobState.ERROR
    assert store.get(b.id).state is JobState.ERROR
    assert "restart" in store.get(a.id).error
    assert store.get(c.id).state is JobState.DONE   # finished work untouched


def test_reap_is_idempotent(store):
    store.create("a")
    assert store.reap_incomplete(reason="x") == 1
    assert store.reap_incomplete(reason="x") == 0


# --- durability -----------------------------------------------------------

def test_sqlite_survives_reopen(tmp_path):
    path = tmp_path / "durable.db"
    s1 = SqliteJobStore(path)
    job = s1.create("https://example.com/v")
    s1.update(job.id, state=JobState.DONE, transcript={"source": "x", "segments": []})
    s1.close()

    s2 = SqliteJobStore(path)
    again = s2.get(job.id)
    assert again is not None
    assert again.state is JobState.DONE
    assert again.transcript == {"source": "x", "segments": []}
    s2.close()


def test_memory_store_does_not_survive_replacement():
    s1 = MemoryJobStore()
    job = s1.create("x")
    s2 = MemoryJobStore()
    assert s2.get(job.id) is None


# --- default store selection ----------------------------------------------

def test_default_store_is_memory_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_DB, raising=False)
    reset_default_store()
    try:
        assert isinstance(get_default_store(), MemoryJobStore)
    finally:
        reset_default_store()


def test_default_store_is_sqlite_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_DB, str(tmp_path / "env.db"))
    reset_default_store()
    try:
        store = get_default_store()
        assert isinstance(store, SqliteJobStore)
        store.close()
    finally:
        reset_default_store()


def test_default_store_is_cached(monkeypatch):
    monkeypatch.delenv(ENV_DB, raising=False)
    reset_default_store()
    try:
        assert get_default_store() is get_default_store()
    finally:
        reset_default_store()


def test_set_default_store_overrides():
    custom = MemoryJobStore()
    set_default_store(custom)
    try:
        assert get_default_store() is custom
    finally:
        reset_default_store()


def test_incomplete_states_constant():
    assert INCOMPLETE_STATES == {JobState.PENDING, JobState.RUNNING}
