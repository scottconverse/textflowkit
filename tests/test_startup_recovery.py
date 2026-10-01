"""Startup recovery: orphaned PENDING/RUNNING rows are failed exactly once.

A durable store can hold jobs left mid-flight by a process that died. After a
restart there is no worker for them, so leaving them alone reports a job that
will never finish (the audit's QA-001: crash, restart, poll /health and status
only, and the job reads RUNNING forever). The recovery that fails those rows must
run **once per owning process, before reads are served** - not on every GET, not
on a tool call, and not as an import side effect - and it must be *visible* when
it cannot write, rather than serving a false "ready".

These tests drive the real recovery owner and the real adapter lifecycles. Only
``transcribe`` is stubbed where a run is needed; no model, network, or ffmpeg is
reached. Every wait is bounded and every executor/process is torn down.
"""

from __future__ import annotations

import textwrap
import threading
import time
from pathlib import Path

import pytest

from textflowkit.core.executor import JobExecutor, reset_default_executor
from textflowkit.core.jobs import (
    JobState,
    get_default_store,
    reset_default_store,
)
from textflowkit.core.sqlite_store import SqliteJobStore

try:  # added by this unit's fix; absent at the RED baseline
    from textflowkit.core.startup import (
        StartupRecovery,
        StartupRecoveryError,
        recover_startup,
        recovery_for,
    )
except ImportError:  # pragma: no cover - baseline only
    StartupRecovery = None  # type: ignore[assignment]
    StartupRecoveryError = None  # type: ignore[assignment]
    recover_startup = None  # type: ignore[assignment]
    recovery_for = None  # type: ignore[assignment]


def _seed_interrupted(path: Path) -> tuple[str, str]:
    """Write a durable store holding one RUNNING and one PENDING row.

    These model the two orphan shapes a crash leaves: a job that had started
    (RUNNING) and one only queued (PENDING). Both have no worker after restart.
    """
    store = SqliteJobStore(path)
    running = store.create("running-row")
    store.update(running.id, state=JobState.RUNNING, progress="transcribing", attempt=2)
    pending = store.create("pending-row")
    store.close()
    return running.id, pending.id


class _WriteBrokenSqlite(SqliteJobStore):
    """A durable store whose *writes* fail while reads stay live.

    Models the fault the audit's "do not falsely serve ready" clause is about: on
    startup the recovery must rewrite orphaned rows, and if that write cannot
    land, startup must fail visibly instead of reporting a healthy server whose
    orphaned jobs still read RUNNING.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.writes_broken = True

    def update(self, job_id: str, **fields):
        if self.writes_broken:
            raise OSError("startup recovery cannot write")
        return super().update(job_id, **fields)

    def claim(self, job_id: str, **kwargs):
        if self.writes_broken:
            raise OSError("startup recovery cannot write")
        return super().claim(job_id, **kwargs)


# --- the recovery owner itself ---------------------------------------------


def test_recovery_fails_orphaned_rows_once(tmp_path):
    """One call reaps both orphan shapes and reports what it did."""
    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)

    store = SqliteJobStore(path)
    try:
        recovery = StartupRecovery(store)
        assert recovery.run() is True, "first recovery must perform the reap"
        assert store.get(running_id).state is JobState.ERROR
        assert store.get(pending_id).state is JobState.ERROR
        assert "restart" in (store.get(running_id).error or "")
        # A second call on the same owner must not reap again (idempotent).
        assert recovery.run() is False, "recovery ran twice for one owner"
    finally:
        store.close()


def test_recovery_is_idempotent_across_calls(tmp_path):
    """Repeated startup on one owner never re-reaps already-terminal rows."""
    path = tmp_path / "jobs.db"
    running_id, _ = _seed_interrupted(path)
    store = SqliteJobStore(path)
    try:
        recovery = StartupRecovery(store)
        recovery.run()
        # A fresh terminal row created after recovery must survive a repeat call.
        done = store.create("later")
        store.update(done.id, state=JobState.DONE)
        assert recovery.run() is False
        assert store.get(done.id).state is JobState.DONE
        assert store.get(running_id).state is JobState.ERROR
    finally:
        store.close()


def test_recovery_does_not_reap_live_jobs_on_concurrent_startup(tmp_path, monkeypatch):
    """Two threads racing startup must reap the orphans exactly once.

    A live job the pool is running (owned by this process) must never be reaped
    by a concurrent startup call; only the genuinely orphaned rows are failed,
    and only once.
    """
    from textflowkit.core import runner
    from textflowkit.core.model import Transcript
    from textflowkit.core.pipeline import TranscribeResult

    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)

    def _ok(source, **kw):
        return TranscribeResult(
            transcript=Transcript(source=source, language="en", segments=[]),
            outputs=[],
        )

    monkeypatch.setattr(runner, "transcribe", _ok)

    store = SqliteJobStore(path)
    ex = JobExecutor(store, max_concurrency=1)
    try:
        results: list[bool] = []
        lock = threading.Lock()

        def _start():
            r = recovery_for(store).run()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_start) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert results.count(True) == 1, f"recovery ran {results.count(True)} times"

        assert store.get(running_id).state is JobState.ERROR
        assert store.get(pending_id).state is JobState.ERROR
    finally:
        ex.shutdown()
        store.close()
        assert not any(w.is_alive() for w in ex._workers)


def test_startup_fails_visibly_when_recovery_cannot_write(tmp_path):
    """A recovery that cannot persist must raise, not report ready."""
    path = tmp_path / "jobs.db"
    _seed_interrupted(path)

    store = _WriteBrokenSqlite(str(path))
    try:
        recovery = StartupRecovery(store)
        assert StartupRecoveryError is not None, "no startup-recovery error type"
        with pytest.raises(StartupRecoveryError):
            recovery.run()
        # The rows are still RUNNING/PENDING - not silently reported healthy.
    finally:
        store.writes_broken = False
        store.close()


# --- the shared owner wired into the executor ------------------------------


def test_executor_start_runs_recovery_before_any_submit(tmp_path):
    """Executor.start() must reap orphans before accepting work."""
    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)

    store = SqliteJobStore(path)
    ex = JobExecutor(store, max_concurrency=1)
    try:
        ex.start()
        assert store.get(running_id).state is JobState.ERROR
        assert store.get(pending_id).state is JobState.ERROR
    finally:
        ex.shutdown()
        store.close()


def test_recover_startup_uses_the_default_owner_once(tmp_path, monkeypatch):
    """The module-level entry point reaps the process-default store once."""
    path = tmp_path / "jobs.db"
    running_id, _ = _seed_interrupted(path)

    monkeypatch.setenv("TEXTFLOWKIT_DB", str(path))
    reset_default_store()
    reset_default_executor()
    try:
        assert recover_startup() is True
        assert get_default_store().get(running_id).state is JobState.ERROR
        assert recover_startup() is False, "process-wide recovery ran twice"
    finally:
        reset_default_executor()
        reset_default_store()


# --- adapter lifecycle: reads are not served before recovery ----------------


def test_http_lifespan_recovers_before_serving_reads(tmp_path, monkeypatch):
    """Starting the HTTP app must reap orphans before /jobs reads are served.

    RED: the app has no lifespan, so a crash/restart followed by health/status
    polling leaves the RUNNING row RUNNING forever. GREEN: entering the app's
    lifespan runs recovery, so the first read already reports ERROR.
    """
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server

    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)

    monkeypatch.setenv("TEXTFLOWKIT_DB", str(path))
    reset_default_store()
    reset_default_executor()
    try:
        with TestClient(
            http_server.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ) as client:
            # The very first read after startup must already be truthful.
            listed = client.get("/jobs").json()
            states = {j["id"]: j["state"] for j in listed["jobs"]}
            assert states.get(running_id) == "error", (
                f"stale RUNNING served after restart: {states.get(running_id)!r}"
            )
            assert states.get(pending_id) == "error"
            # health stays cheap and truthful
            assert client.get("/health").json()["status"] == "ok"
    finally:
        reset_default_executor()
        reset_default_store()


def test_http_startup_fails_visibly_when_recovery_cannot_write(tmp_path, monkeypatch):
    """A recovery that cannot write must fail startup, not serve ready.

    Entering the app's lifespan with a write-broken store must surface the fault
    rather than yielding a client that reports /health ok while orphans remain.
    """
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server
    from textflowkit.core import startup

    path = tmp_path / "jobs.db"
    _seed_interrupted(path)
    broken = _WriteBrokenSqlite(str(path))

    # The lifespan resolves the store through the startup module, so that is the
    # seam the broken store must be injected at.
    monkeypatch.setattr(startup, "get_default_store", lambda: broken)
    monkeypatch.setattr(http_server, "get_default_store", lambda: broken)
    reset_default_executor()
    try:
        with pytest.raises(StartupRecoveryError), TestClient(
            http_server.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ):
            pass
    finally:
        broken.writes_broken = False
        reset_default_executor()
        broken.close()


def test_http_lifespan_shuts_down_owned_workers(tmp_path, monkeypatch):
    """Exiting the HTTP lifespan must drain the workers this process started.

    A running job starts the pool lazily; when the server stops, that pool's
    threads must be joined rather than left as daemons. The lifespan owns that
    shutdown.
    """
    from fastapi.testclient import TestClient

    from textflowkit.adapters import http_server
    from textflowkit.core.executor import get_default_executor

    monkeypatch.delenv("TEXTFLOWKIT_DB", raising=False)
    reset_default_store()
    reset_default_executor()
    try:
        with TestClient(
            http_server.app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        ):
            # Start the pool inside the lifespan so there is something to drain.
            ex = get_default_executor()
            ex.start()
            assert ex._workers and all(w.is_alive() for w in ex._workers)
        # After the lifespan exits, the owned workers are stopped.
        assert not any(w.is_alive() for w in ex._workers), "lifespan left workers running"
    finally:
        reset_default_executor()
        reset_default_store()


def test_importing_the_adapter_does_not_reap(tmp_path):
    """Importing the adapter must not touch the store (no import side effect).

    A module import must never reap: the store is chosen from the environment at
    request time, and a reap on import would fail live jobs in a process that is
    merely inspecting the module.

    This runs in a *separate* Python process. An in-process ``importlib.reload``
    would mutate a module every other boundary test already holds by identity -
    the reload rebinds ``mcp_server.mcp``/``run_http`` while a concurrent test's
    patch still targets the pre-reload objects, so a test that meant to stub
    ``run_http`` instead reaches the real server and hangs. Isolating the import
    keeps the assertion (an import is not a reap) airtight while leaving the live
    pytest process - and every module object in it - untouched.

    The child binds the candidate ``src`` explicitly and gets its own temp
    directory and database, so it shares nothing with this process.
    """
    import json
    import os
    import subprocess
    import sys

    path = tmp_path / "jobs.db"
    running_id, _ = _seed_interrupted(path)

    root = Path(__file__).resolve().parents[1]
    child = textwrap.dedent(
        """
        import json, sys
        from textflowkit.adapters import mcp_server  # the import under test
        from textflowkit.core.jobs import get_default_store
        job = get_default_store().get(sys.argv[1])
        print(json.dumps({"state": job.state.value if job else None}))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["TEXTFLOWKIT_DB"] = str(path)
    env.pop("TEXTFLOWKIT_PROFILE", None)

    proc = subprocess.run(
        [sys.executable, "-c", child, running_id],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"importing the adapter failed:\n{proc.stderr}"
    state = json.loads(proc.stdout.strip().splitlines()[-1])["state"]
    assert state == "running", f"import side effect reaped the row: {state!r}"


# --- shutdown ownership: no overlap while an old worker is still exiting -----


def test_shutdown_keeps_owner_until_old_workers_exit(tmp_path, monkeypatch):
    """A timed-out shutdown must not let a replacement pool overlap the old one.

    The bounded join can time out while a worker is still inside a long model
    call. If the cached executor were cleared regardless and a fresh pool
    started, two runs would execute at once - breaking the concurrency bound.
    Here the store is dropped through the module-level entry point exactly as an
    adapter shutdown does, and admission must refuse until the old worker
    actually exits; only then may a replacement pool start and accept work.

    The run is held on a ``threading.Event`` (no model, no network) so the
    timing is controlled rather than raced: the worker stays alive past the
    ``timeout=0.01`` join on purpose, and the event is always released in
    ``finally`` so no thread is left running after the test.
    """
    from textflowkit.core import runner
    from textflowkit.core.executor import (
        StoreUnavailableError,
        get_default_executor,
        reset_default_executor,
        shutdown_default_executor,
    )

    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    lock = threading.Lock()

    def _blocking(source, **kw):
        with lock:
            calls.append(1)
        entered.set()
        release.wait(timeout=10)
        # Never reached: the job is cancelled while blocked, so run_job raises
        # JobCancelled at its next stage boundary and reports it terminal.
        raise AssertionError("blocking stub unexpectedly resumed past cancellation")

    path = tmp_path / "jobs.db"
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(path))
    monkeypatch.setattr(runner, "transcribe", _blocking)
    reset_default_executor()
    reset_default_store()
    try:
        ex = get_default_executor()
        ex.submit(source="held")
        assert entered.wait(timeout=5), "worker never entered the stub"

        # Same call an adapter shutdown makes: clear the cached default while the
        # worker is still inside the call, with a tiny bound so the join times out.
        assert shutdown_default_executor(wait=True, timeout=0.01) is True

        # The old worker is still alive, so the pool has not stopped: the cached
        # default is still that same draining pool (identity, not a look-alike),
        # and its own admission path refuses rather than spawning an overlap.
        draining = get_default_executor()
        assert draining is ex, "a replacement pool appeared while a worker was alive"
        with pytest.raises(StoreUnavailableError):
            draining.submit(source="overlap")
        with lock:
            assert len(calls) == 1, "a replacement pool ran a job beside the old one"

        # Once the old worker is released and actually exits, the draining pool
        # is replaced by a genuinely different pool (nothing was copied between
        # them) and admission reopens there.
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(w.is_alive() for w in ex._workers):
            time.sleep(0.01)
        assert not any(w.is_alive() for w in ex._workers), "old worker never exited"
        fresh = get_default_executor()
        assert fresh is not ex, "drained pool was not replaced"
        job = fresh.submit(source="after-drain")
        assert job is not None
    finally:
        # Always free the blocking stub before tearing the pool down, so the
        # test never leaves a thread stuck in release.wait().
        release.set()
        reset_default_executor()
        reset_default_store()


# --- MCP: the real stdio protocol across a restart --------------------------


class _StdioClient:
    """Minimal MCP stdio client: newline-delimited JSON-RPC over a child."""

    def __init__(self, env: dict[str, str]) -> None:
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "textflowkit.adapters.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 0

    def call(self, method: str, params: dict | None = None, *, notify: bool = False):
        import json

        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notify:
            self._next_id += 1
            frame["id"] = self._next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout unexpectedly")
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if "id" in payload:
                return payload

    def initialize(self) -> None:
        self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "startup-recovery-test", "version": "1.0"},
            },
        )
        self.call("notifications/initialized", {}, notify=True)

    def close(self) -> None:
        import subprocess

        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def test_mcp_stdio_status_only_restart_reports_interrupted(tmp_path):
    """A real MCP process, restarted, must report orphans as error, not running.

    The audit reproduced QA-001 over status/list only: crash, restart, poll.
    This spawns the actual stdio server against a durable store holding a
    RUNNING and a PENDING orphan and drives ``get_job_status``/``list_jobs`` over
    the wire. The first status answer must already be truthful.
    """
    pytest.importorskip("mcp", reason="the mcp extra is required for the stdio server")
    import os

    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["TEXTFLOWKIT_DB"] = str(path)
    env.pop("TEXTFLOWKIT_PROFILE", None)

    client = _StdioClient(env)
    try:
        client.initialize()
        running = client.call(
            "tools/call",
            {"name": "get_job_status", "arguments": {"job_id": running_id}},
        )
        import json

        payload = json.loads(running["result"]["content"][0]["text"])
        assert payload["state"] == "error", (
            f"stale RUNNING reported after restart over real stdio: {payload['state']!r}"
        )

        listed = client.call("tools/call", {"name": "list_jobs", "arguments": {}})
        jobs = json.loads(listed["result"]["content"][0]["text"])["jobs"]
        states = {j["id"]: j["state"] for j in jobs}
        assert states.get(pending_id) == "error"
        assert states.get(running_id) == "error"
        assert all(s not in {"running", "pending"} for s in states.values())
    finally:
        client.close()


def test_mcp_streamable_http_lifespan_recovers(tmp_path, monkeypatch):
    """The MCP Streamable-HTTP app's lifespan must also run startup recovery.

    ``streamable_http_app()`` owns its own lifespan (the SDK's session manager),
    separate from the server lifespan stdio enters. Recovery must run there too,
    so a crash/restart of the HTTP surface also reports orphans truthfully before
    the first request is served.
    """
    pytest.importorskip("mcp", reason="the mcp extra is required for the HTTP app")
    import asyncio

    path = tmp_path / "jobs.db"
    running_id, pending_id = _seed_interrupted(path)
    monkeypatch.setenv("TEXTFLOWKIT_DB", str(path))
    monkeypatch.delenv("TEXTFLOWKIT_PROFILE", raising=False)
    reset_default_store()
    reset_default_executor()
    try:
        from textflowkit.adapters import mcp_server

        app = mcp_server.mcp.streamable_http_app()

        async def _enter_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(_enter_lifespan())

        assert get_default_store().get(running_id).state is JobState.ERROR
        assert get_default_store().get(pending_id).state is JobState.ERROR
    finally:
        reset_default_executor()
        reset_default_store()

