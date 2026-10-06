"""One owner per database, and a shutdown that drains rather than kills.

These cover the two behaviours that a browser-only UI cannot leave to a terminal:

- **Single ownership.** A durable SQLite store must not be opened by two UI
  processes at once. The owner lock is a genuine *operating-system* file lock
  (``msvcrt`` on Windows, ``flock`` on POSIX) on a persistent sibling file that is
  never unlinked; the kernel releases it when the holder exits **or is killed**,
  so a crashed run does not block the next launch forever. Ownership is proved by
  the held lock plus a matching owner record, never by a pid.
- **Controlled shutdown.** "Stop server" in the workspace asks this process's own
  uvicorn server to exit through its supported mechanism; it never signals a pid.
  The endpoint refuses when no owned server is attached.

The ownership tests use **real subprocesses**: a helper interpreter acquires the
lock (or is hard-killed while holding it) and the parent asserts on the outcome.
Real contention and a real crash are the point - a mocked "dead pid" would test
our own mock, not the OS lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from textflowkit.ui import ownership
from textflowkit.ui.app import create_app
from textflowkit.ui.security import CAPABILITY_HEADER

LOOPBACK_PEER = ("127.0.0.1", 50000)

#: A child interpreter with this source directory importable, so a subprocess can
#: drive the real ``ownership`` module exactly as a second launch would.
_SRC_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_CHILD_ENV = {**os.environ, "PYTHONPATH": _SRC_ROOT}

#: Child program: acquire the lock, publish a URL, announce, then hold for a while.
_HOLD = textwrap.dedent(
    """
    import sys, os, time
    from textflowkit.ui import ownership
    lock = ownership.acquire_owner_lock(sys.argv[1])
    lock.write_metadata(url="http://127.0.0.1:8899/", version="0.1.9")
    sys.stdout.write("ACQUIRED\\n"); sys.stdout.flush()
    time.sleep(float(sys.argv[2]))
    lock.release()
    sys.stdout.write("RELEASED\\n"); sys.stdout.flush()
    """
)

#: Child program: try to acquire; report the outcome (and the URL on refusal).
_TRY = textwrap.dedent(
    """
    import sys
    from textflowkit.ui import ownership
    try:
        lock = ownership.acquire_owner_lock(sys.argv[1])
    except ownership.AlreadyRunningError as exc:
        sys.stdout.write("REFUSED url=%s\\n" % exc.url)
    else:
        sys.stdout.write("ACQUIRED\\n")
        lock.release()
    sys.stdout.flush()
    """
)

#: Child program: report what discovery says about the database.
_DISCOVER = textwrap.dedent(
    """
    import sys
    from textflowkit.ui import ownership
    url = ownership.discover_running(sys.argv[1])
    sys.stdout.write("DISCOVER %s\\n" % url); sys.stdout.flush()
    """
)


def _child(source: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source, *args],
        env=_CHILD_ENV, capture_output=True, text=True, timeout=30, check=False,
    )


# --- owner lock -----------------------------------------------------------


def test_lock_granted_when_free(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        # The lock file is a persistent carrier: it exists and stays after release.
        assert lock.lock_path.exists()
        assert lock.lock_is_held() is True
    finally:
        lock.release()
    # Persistent lock file survives the release (never unlinked).
    assert lock.lock_path.exists()
    assert lock.lock_is_held() is False


def test_second_owner_refused_while_first_live(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    first = ownership.acquire_owner_lock(db)
    try:
        # The OS lock is held, so a second acquisition in the same process fails.
        with pytest.raises(ownership.AlreadyRunningError):
            ownership.acquire_owner_lock(db)
    finally:
        first.release()


def test_lock_keys_on_resolved_path(tmp_path):
    """Two spellings of one database collide on one lock."""
    real = tmp_path / "sub" / "jobs.sqlite3"
    real.parent.mkdir()
    weird = tmp_path / "sub" / ".." / "sub" / "jobs.sqlite3"
    assert ownership.lock_path_for(real) == ownership.lock_path_for(weird)


def test_persistent_lock_file_is_never_unlinked(tmp_path):
    """The lock file persists across acquire/release cycles (no unlink race)."""
    db = tmp_path / "jobs.sqlite3"
    lock_path = ownership.lock_path_for(db)
    for _ in range(3):
        lock = ownership.acquire_owner_lock(db)
        assert lock.lock_path == lock_path
        lock.release()
        assert lock_path.exists(), "the lock file must survive a release"


def test_real_contention_two_subprocesses(tmp_path):
    """A live holder in one real process refuses a real contender in another.

    This is genuine cross-process contention: process A holds the OS lock while
    process B tries to acquire it. No pid mocking is involved - B is refused
    because the kernel will not grant the byte-range lock A holds.
    """
    db = tmp_path / "jobs.sqlite3"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(db), "5"],
        env=_CHILD_ENV, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        contender = _child(_TRY, str(db))
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip().startswith("REFUSED")
        # Discovery from a third real process sees the live URL.
        viewer = _child(_DISCOVER, str(db))
        assert viewer.stdout.strip() == "DISCOVER http://127.0.0.1:8899/"
    finally:
        holder.kill()
        holder.wait()
    # With the holder gone, a fresh process acquires cleanly.
    after = _child(_TRY, str(db))
    assert after.stdout.strip() == "ACQUIRED", after.stderr


def test_hard_crash_frees_the_lock_and_leaves_no_live_owner(tmp_path):
    """A holder killed with no cleanup must not block the next launch.

    The holder is hard-killed (no release path runs), which is what a machine
    crash or a Task Manager kill looks like. The kernel drops the lock, so:
    discovery reports no live owner even though a stale metadata record remains,
    and a fresh process acquires the lock rather than being blocked forever.
    """
    db = tmp_path / "jobs.sqlite3"
    meta = ownership.metadata_path_for(db)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(db), "30"],
        env=_CHILD_ENV, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        assert _child(_DISCOVER, str(db)).stdout.strip() == "DISCOVER http://127.0.0.1:8899/"
        holder.kill()
        holder.wait()
        # Give the kernel a moment to release the lock on the dead process.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ownership.OwnerLock(db).lock_is_held():
            time.sleep(0.05)
        # The stale metadata record is still on disk ...
        assert meta.exists(), "crashed holder's record is the stale case under test"
        # ... but it is not a live owner, and the lock is free.
        assert ownership.discover_running(db) is None
        assert ownership.stale_metadata_is_not_live(db) is True
        assert _child(_TRY, str(db)).stdout.strip() == "ACQUIRED"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_stale_owner_record_is_not_advertised_before_publish(tmp_path):
    """The coordinator's exact stale case: a crashed owner's record on disk, then
    a fresh owner acquires the lock but has not published its URL yet.

    The old design advertised the crashed owner's URL here, because it checked only
    that *a* record existed beside the held lock. The fix makes the record match the
    live holder's identity (read from the held lock file), so the stale record no
    longer matches and nothing is advertised until this holder publishes.
    """
    db = tmp_path / "jobs.sqlite3"
    meta = ownership.metadata_path_for(db)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        '{"owner": "old-crashed-owner", "pid": 1234, '
        '"url": "http://127.0.0.1:9999/", "started": 1, "version": "0.1.9"}',
        encoding="utf-8",
    )
    lock = ownership.acquire_owner_lock(db)  # acquired, but no write_metadata yet
    try:
        assert meta.exists(), "the stale record is the case under test"
        assert lock.owner_token and lock.owner_token != "old-crashed-owner"
        # The live holder's identity is readable from the held lock file ...
        assert ownership.read_owner_identity(lock.lock_path) == lock.owner_token
        # ... and the stale record does not match it, so nothing is advertised.
        assert lock.has_live_owner() is False
        assert ownership.discover_running(db) is None
        assert ownership.stale_metadata_is_not_live(db) is True
        # Once this holder publishes, discovery advertises the *new* URL.
        lock.write_metadata(url="http://127.0.0.1:9123/", version="0.1.9")
        assert ownership.discover_running(db) == "http://127.0.0.1:9123/"
    finally:
        lock.release()


def test_real_process_stale_record_is_not_advertised(tmp_path):
    """A record left by a *crashed* real process is not advertised by a new holder.

    A real subprocess acquires, publishes, and is hard-killed. Its record stays on
    disk. A new holder then acquires (without publishing yet): the crashed owner's
    identity is gone from the lock file and the new holder's differs, so discovery
    must not report the crashed URL.
    """
    db = tmp_path / "jobs.sqlite3"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(db), "30"],
        env=_CHILD_ENV, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        holder.kill()
        holder.wait()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and ownership.OwnerLock(db).lock_is_held():
        time.sleep(0.05)
    # The crashed holder's record is still on disk, but not live.
    assert ownership.metadata_path_for(db).exists()
    assert ownership.discover_running(db) is None
    # A fresh holder that has not published does not revive the crashed URL.
    lock = ownership.acquire_owner_lock(db)
    try:
        assert ownership.discover_running(db) is None
        lock.write_metadata(url="http://127.0.0.1:9124/", version="0.1.9")
        assert ownership.discover_running(db) == "http://127.0.0.1:9124/"
    finally:
        lock.release()


def test_metadata_rejects_non_loopback_and_executable_urls(tmp_path):
    """Only a valid http loopback URL may be recorded, so discovery never advertises
    an external or executable URL to a browser."""
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        for bad in (
            "http://evil.example.com:8756/",
            "https://127.0.0.1:8756/",       # not the UI's http scheme
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "http://169.254.169.254/",
            "http://10.0.0.5:8756/",
            "http://user:pass@127.0.0.1:8756/",
            "",
        ):
            lock.write_metadata(url=bad)
            assert lock.read_metadata() == {}, f"{bad!r} must not be recorded"
            assert ownership.discover_running(db) is None
        # A genuine loopback URL is recorded and advertised.
        assert ownership.is_loopback_http_url("http://127.0.0.1:8756/")
        assert ownership.is_loopback_http_url("http://localhost:8756/")
        assert ownership.is_loopback_http_url("http://[::1]:8756/")
        lock.write_metadata(url="http://127.0.0.1:8756/")
        assert ownership.discover_running(db) == "http://127.0.0.1:8756/"
    finally:
        lock.release()


def test_discovery_rejects_a_tampered_record_url(tmp_path):
    """Even a record hand-written on disk is not advertised if its URL is not a
    loopback http URL (the URL is validated at read time too, not just at write)."""
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        lock.write_metadata(url="http://127.0.0.1:8756/")
        meta = ownership.metadata_path_for(db)
        record = json.loads(meta.read_text(encoding="utf-8"))
        record["url"] = "file:///C:/Windows/System32/calc.exe"
        # Hand-tamper with the record that carries the *live* holder's identity.
        meta.write_text(json.dumps(record), encoding="utf-8")
        assert lock.has_live_owner() is True  # identity matches ...
        assert ownership.discover_running(db) is None  # ... but the URL is refused
    finally:
        lock.release()


def test_stale_metadata_without_lock_is_not_live(tmp_path):
    """A leftover live-URL record with no held lock is not a running instance."""
    db = tmp_path / "jobs.sqlite3"
    meta = ownership.metadata_path_for(db)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        '{"owner": "deadbeef", "pid": 1, "url": "http://127.0.0.1:8756/"}',
        encoding="utf-8",
    )
    # No lock file at all (or one that is not held).
    assert ownership.discover_running(db) is None
    assert ownership.stale_metadata_is_not_live(db) is True


def test_metadata_url_is_discovered_while_live(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        lock.write_metadata(url="http://127.0.0.1:8800/", version="0.1.9")
        assert ownership.discover_running(db) == "http://127.0.0.1:8800/"
        # The record never carries anything but owner/pid/url/started/version.
        record = lock.read_metadata()
        assert set(record) <= {"owner", "pid", "url", "started", "version"}
        assert record["owner"]  # a non-empty per-acquisition token
    finally:
        lock.release()
    assert ownership.discover_running(db) is None
    # The record is removed under the lock on release; the lock file persists.
    assert not ownership.metadata_path_for(db).exists()


def test_live_lock_without_record_is_not_discoverable(tmp_path):
    """A held lock with no owner record is not yet a discoverable instance."""
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        # Locked, but no URL has been published: not a *live owner* for discovery.
        assert lock.lock_is_held() is True
        assert ownership.discover_running(db) is None
    finally:
        lock.release()


def test_record_without_a_held_lock_is_never_live(tmp_path):
    """Matching is by "record present while the lock is held", so a record with
    no held lock - however well-formed - is not a live instance.

    This is the stale/foreign case that actually occurs: a crashed holder's
    record left on disk is only ever seen with the lock free, and must be read as
    not-live. (A record cannot be planted beside a *held* lock by another process,
    because that process would have to hold the lock to write it under the
    protocol - the invariant the design rests on.)
    """
    db = tmp_path / "jobs.sqlite3"
    meta = ownership.metadata_path_for(db)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        '{"owner": "not-this-holder", "pid": 1, "url": "http://127.0.0.1:1/"}',
        encoding="utf-8",
    )
    # No held lock, though the record is well-formed with a valid token.
    assert ownership.OwnerLock(db).lock_is_held() is False
    assert ownership.discover_running(db) is None
    assert ownership.stale_metadata_is_not_live(db) is True


def test_metadata_never_records_extra_fields(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    lock = ownership.acquire_owner_lock(db)
    try:
        lock.write_metadata(url="http://127.0.0.1:8756/")
        # A capability token or source cannot be smuggled in - only allowlisted fields.
        raw = lock.meta_path.read_text(encoding="utf-8")
        assert "capability" not in raw
        assert "token" not in raw.replace(lock.owner_token, "")
    finally:
        lock.release()


def test_parse_port_from_url():
    assert ownership.parse_port_from_url("http://127.0.0.1:8756/") == 8756
    assert ownership.parse_port_from_url("http://127.0.0.1:8756") == 8756
    assert ownership.parse_port_from_url("nonsense") is None
    assert ownership.parse_port_from_url(None) is None


def test_already_running_carries_url(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    first = ownership.acquire_owner_lock(db)
    first.write_metadata(url="http://127.0.0.1:8801/")
    try:
        with pytest.raises(ownership.AlreadyRunningError) as info:
            ownership.acquire_owner_lock(db)
        assert info.value.url == "http://127.0.0.1:8801/"
    finally:
        first.release()


# --- shutdown endpoint ----------------------------------------------------


@pytest.fixture
def app():
    return create_app(host="127.0.0.1", port=8756)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)


def _token(client) -> str:
    import re

    return re.search(
        r'name="textflowkit-capability" content="([^"]+)"', client.get("/").text
    ).group(1)


def test_shutdown_requires_capability(client):
    resp = client.post("/ui/shutdown")
    assert resp.status_code == 403
    assert "capability" in resp.json()["error"]


def test_shutdown_refuses_without_owned_server(client):
    """No launcher-owned server attached -> refuse, never pretend to stop."""
    token = _token(client)
    resp = client.post("/ui/shutdown", headers={CAPABILITY_HEADER: token})
    assert resp.status_code == 409


def test_shutdown_requests_owned_server_exit():
    """With an owned server attached, shutdown sets its exit flag - no pid signal."""
    app = create_app(host="127.0.0.1", port=8756)

    class _FakeServer:
        should_exit = False
        force_exit = False

    fake = _FakeServer()
    app.ui_attach_owned_server(fake)

    with TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER) as client:
        token = _token(client)
        # Prime the request loop so the controller has captured the loop.
        client.get("/ui/capabilities")
        resp = client.post("/ui/shutdown", headers={CAPABILITY_HEADER: token})
        assert resp.status_code == 200
        assert resp.json()["stopping"] is True
        # The watchdog asks the *server object* to exit; it never signals a pid.
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not fake.should_exit:
            time.sleep(0.05)
        assert fake.should_exit is True
        assert fake.force_exit is False
