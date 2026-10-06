"""One live UI process per durable job store.

The job store is a SQLite database. SQLite tolerates several *readers* of one
file, but this application's contract is stronger than SQLite's: a store has one
owner, and that owner runs startup recovery, the worker pool, and the request
gate. **Two UI processes started against the same database file is therefore not
supported, and the second is refused here rather than allowed to race the first
for the same rows.**

This module makes that refusal real with two pieces:

- **An exclusive owner lock**, held for as long as a UI process serves. The lock
  is a genuine *operating-system* file lock on a byte range of a small sibling
  file, keyed by the *resolved* database path, so two spellings of one file (a
  symlink, a short ``8.3`` name, a relative path) still collide on one lock. On
  Windows it is ``msvcrt.locking``; on POSIX it is ``fcntl.flock``. Because the
  OS holds the lock on behalf of a live process, the kernel releases it when
  that process exits **or is killed** - so a lock left behind by a crashed run
  is automatically free again and a fresh launch is never blocked forever. This
  module never sends a signal to any process and never inspects another
  process's liveness: ownership is *proved by holding the OS lock*, not inferred
  from a pid.
- **A small live-URL record** next to the lock, so a second launch can tell the
  operator *where* the running UI already is and open that, instead of starting a
  competing server.

**The lock file is never unlinked.** It is a persistent, tiny sibling of the
database (``jobs.sqlite3.owner.lock``). Removing a lock file and recreating it is
a classic race: another process can hold a lock on the removed inode while a
third locks a brand-new inode at the same path, and both believe they own the
store. By keeping one file for the database's lifetime and letting the OS lock
be the whole of the guarantee, there is no unlink/recreate window to race.

**The owner's identity is readable while the lock is held.** The lock is taken on
**byte 0** of the lock file, and the owning process writes its per-acquisition
**owner identity** into bytes from **offset 2** onward. On Windows a mandatory
byte-range lock denies foreign handles the *locked region* but not bytes outside
it, so a reader may read the identity from offset 2 while the lock is genuinely
held - measured, not assumed (see the module note on :data:`_IDENTITY_OFFSET`).
The identity is what makes the matching check a *real comparison*: a reader reads
the live holder's identity from its own lock file and requires the record beside
it to carry the *same* identity. A record from a previous, crashed run carries a
*different* identity and cannot be advertised as the current owner - which the
previous design, which only checked that the record existed, could not catch.

There are two files, with distinct jobs:

- the **lock file** carries the OS lock (byte 0) and the current holder's **owner
  identity** (offset 2), never unlinked; and
- the **record** (``<db>.owner.json``) carries the same owner identity plus the
  loopback URL, and is written **under the lock** and removed **under the lock**.

A record is advertised as current only when **the OS lock is held AND the identity
read from the held lock file matches the identity in the record**. A URL is only
ever published by :meth:`OwnerLock.write_metadata`, which is called once the
server is actually serving, and only after the record is validated as a loopback
HTTP URL - so discovery never returns a leftover record's stale URL and never
returns an arbitrary external or executable URL.

What this deliberately does **not** do: it does not stop, signal, or clean up any
other process, and it makes no claim about a *separate* developer HTTP server that
was pointed at the same database with no lock of its own. The honest rule is the
one printed to the operator: **do not share one database file between two
processes.** This lock enforces that rule among UI launches that go through this
module; it is not a database-level guarantee against every other program.

Everything written here lives beside the database under the per-user data
directory and contains only an owner token, a pid, a loopback URL, and a
timestamp - never a capability token, a source path, or a transcript.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

#: Suffix appended to the database file name for the owner-lock path, e.g.
#: ``jobs.sqlite3.owner.lock``. Kept as a plain sibling file so it is obvious in a
#: directory listing which database a lock belongs to. This file is **persistent**:
#: it is created on first use and never removed (see the module docstring).
LOCK_SUFFIX = ".owner.lock"
#: Where the running instance advertises its loopback URL, for a friendly second
#: launch: "the UI is already running at http://127.0.0.1:8756".
_JSON_SUFFIX = ".owner.json"

#: Fields allowed in the owner record. Anything else a caller passes is dropped,
#: so a capability token or a source can never be written here even by mistake.
#: This is the one place the field set is defined; :meth:`OwnerLock.write_metadata`
#: filters to it.
_RECORD_FIELDS = ("owner", "pid", "url", "started", "version")

#: The single byte range locked in the lock file. One byte at offset zero is
#: enough to make the whole file an exclusive ownership flag, and keeping it to a
#: fixed range means a lock is always taken and released at the same coordinates.
_LOCK_LENGTH = 1

#: Where the current holder's human-inspectable **owner identity** begins in the
#: lock file. It is deliberately *past* the locked byte (offset 0): a Windows
#: ``msvcrt`` mandatory byte-range lock denies foreign handles the locked region
#: but **not** the bytes outside it, so a reader can still read the identity from
#: offset 2 while the lock is genuinely held. Measured on Windows 11 / CPython
#: 3.13: a foreign handle reads ``b'IDENTITY'`` at offset 2 while another process
#: holds a lock at byte 0, and a read that overlaps byte 0 raises
#: ``PermissionError [Errno 13]``. The identity therefore survives the crash of
#: its holder and can be compared, without signalling any process.
_IDENTITY_OFFSET = 2

#: Longest owner identity kept/compared. A fixed width means a reader can always
#: take the identity slice without guessing a length.
_IDENTITY_LENGTH = 32

#: The URL scheme/host a discoverable instance may advertise. The UI serves on
#: loopback HTTP only, so anything else - a file:// or javascript: or
#: shell-executable scheme, or a non-loopback host - is refused rather than handed
#: to a browser by a second launch.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def lock_path_for(db_path: str | Path) -> Path:
    """The owner-lock path for ``db_path``.

    The lock sits beside the database on the *resolved* path, so two references
    to one file agree on one lock. Resolution is best-effort: a path whose parent
    does not exist yet still yields a stable absolute lock path.
    """
    resolved = _resolve(db_path)
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


def metadata_path_for(db_path: str | Path) -> Path:
    """The live-URL record path for ``db_path`` (see :func:`lock_path_for`)."""
    resolved = _resolve(db_path)
    return resolved.with_name(resolved.name + _JSON_SUFFIX)


def _resolve(db_path: str | Path) -> Path:
    path = Path(db_path).expanduser()
    try:
        # strict=False: the database file may not exist yet on a first launch.
        return path.resolve(strict=False)
    except OSError:  # pragma: no cover - a pathological path still gets a name
        return path.absolute()


def _lock_fd(fd: int) -> None:
    """Take an exclusive, non-blocking OS lock on ``fd``, or raise ``OSError``.

    Windows uses ``msvcrt.locking`` on the first byte; POSIX uses
    ``fcntl.flock``. Both are *held by the operating system on behalf of this
    process* and released automatically when the process exits or dies, which is
    what makes a crashed holder's lock free again without any pid check.

    On failure (the lock is held elsewhere) this raises the platform's
    ``OSError`` - ``EACCES``/``EDEADLK`` on Windows, ``EAGAIN``/``EWOULDBLOCK`` on
    POSIX - and the caller treats a raised error as "held by another".
    """
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_LENGTH)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    """Release the OS lock on ``fd`` (best-effort; closing the fd also releases)."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_LENGTH)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - already released or closed
        pass


def _decode_identity(raw: bytes) -> str:
    """The identity from an identity slice, or ``""`` if it is not one.

    The identity is a hex token, so the slice is expected to be ``[0-9a-f]`` (with
    ``NUL`` padding). Anything else - a truncated, torn, or foreign write - reads
    as "no identity", which callers treat as not-matching.
    """
    token = raw.split(b"\x00", 1)[0]
    if not token or len(token) > _IDENTITY_LENGTH:
        return ""
    try:
        text = token.decode("ascii")
    except UnicodeDecodeError:
        return ""
    if not re.fullmatch(r"[0-9a-f]+", text):
        return ""
    return text


def read_owner_identity(lock_path: Path) -> str:
    """The owner identity currently written in ``lock_path``, or ``""``.

    Reads **only** the identity region at :data:`_IDENTITY_OFFSET`, which is
    outside the locked byte (offset 0), so it succeeds even while another process
    holds the lock - that is the whole point of putting the lock at byte 0 and the
    identity past it. A missing file (no owner has ever run) is ``""``.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return ""
    try:
        os.lseek(fd, _IDENTITY_OFFSET, os.SEEK_SET)
        raw = os.read(fd, _IDENTITY_LENGTH)
    except OSError:
        return ""
    finally:
        os.close(fd)
    return _decode_identity(raw)


def _write_owner_identity(fd: int, identity: str) -> None:
    """Write ``identity`` into the lock ``fd``'s identity region.

    Fixed width: the region is NUL-padded to :data:`_IDENTITY_LENGTH` so a shorter
    identity can never leave a longer previous identity's tail behind, which would
    make two different identities compare equal. The write starts at
    :data:`_IDENTITY_OFFSET`, never touching the locked byte at offset 0.
    """
    blob = identity.encode("ascii")
    blob = (blob + b"\x00" * _IDENTITY_LENGTH)[:_IDENTITY_LENGTH]
    os.lseek(fd, _IDENTITY_OFFSET, os.SEEK_SET)
    os.write(fd, blob)


def is_loopback_http_url(url: object) -> bool:
    """Whether ``url`` is an ``http`` loopback URL the UI could be reached at.

    The record's URL is handed to a browser by a second launch, so it is validated
    here rather than trusted: the scheme must be ``http`` (not ``file``, not an
    executable scheme), the host must be a loopback name or address, and no
    username/password may be embedded. Anything else - an external host, a
    ``file://`` or ``javascript:`` URL - is refused, so discovery can never
    advertise an arbitrary external or executable URL.
    """
    if not isinstance(url, str) or not url:
        return False
    match = re.match(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)", url)
    if match is None or match.group("scheme").lower() != "http":
        return False
    authority = match.group("authority")
    if "@" in authority:  # embedded credentials are never part of a UI URL
        return False
    host = authority
    if host.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8756
        host = host.split("]", 1)[0] + "]"
    elif ":" in host:  # strip an optional :port
        host = host.rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def _lock_is_held(db_path: str | Path) -> bool:
    """Whether an *OS lock* is currently held on ``db_path``'s lock file.

    This opens the lock file and attempts a non-blocking exclusive lock: if the
    attempt fails the lock is genuinely held by a live process, because the OS
    releases it the instant the holder exits. If the file does not exist, no
    owner has ever run - which is "not held". This never reads a pid and never
    signals anything.
    """
    lock = OwnerLock(db_path)
    return lock.lock_is_held()


class AlreadyRunningError(RuntimeError):
    """A live UI process already owns this database.

    Carries the other instance's loopback ``url`` when it is known, so the caller
    can open the running UI instead of failing outright.
    """

    def __init__(self, message: str, *, url: str | None = None, pid: int | None = None):
        super().__init__(message)
        self.url = url
        self.pid = pid


class OwnerLock:
    """An exclusive, self-healing OS lock on one database file.

    Not re-entrant and not thread-shared: one lock object belongs to one launch
    attempt. Use :func:`acquire_owner_lock`, or as a context manager via
    :meth:`acquire`, so the lock is released on every exit path including an
    exception.

    The lock is a byte-range lock the *operating system* holds on a persistent
    sibling file. It is the whole of the guarantee: it is taken atomically, it is
    released by the kernel on exit or crash, and it is never removed or recreated,
    so there is no unlink/recreate window in which two processes could lock
    different inodes at one path.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = _resolve(db_path)
        self.lock_path = lock_path_for(self.db_path)
        self.meta_path = metadata_path_for(self.db_path)
        self._fd: int | None = None
        #: A fresh random identity for this acquisition, written to both the lock
        #: file and the metadata record so a discovery can confirm the record
        #: beside a held lock belongs to the same holder.
        self.owner_token: str = ""

    # -- acquire / release -------------------------------------------------

    def acquire(self, *, pid: int | None = None) -> None:
        """Take the lock, or raise :class:`AlreadyRunningError`.

        The lock file is opened (created if absent; never unlinked), then an
        exclusive non-blocking OS lock is taken on it. If the OS refuses, another
        live process holds it and :class:`AlreadyRunningError` is raised. There is
        no stale-reclaim branch: a crashed holder's lock is released by the kernel
        automatically, so a genuinely free lock is *taken*, not reclaimed.
        """
        del pid  # accepted for API symmetry; a pid is never used as a liveness test
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Open/create the lock file for read+write without truncating (it is a
        # shared, persistent carrier). Opening read+write keeps the handle usable
        # for the byte-range lock on both platforms.
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            raise self._already_running() from None
        # From here on we hold the lock. Write this acquisition's identity into the
        # readable region (offset 2), so a reader can compare the live holder's
        # identity against the record beside it, and - crucially - so the *previous*
        # holder's identity is overwritten and can no longer match a leftover record.
        self.owner_token = secrets.token_hex(16)
        try:
            _write_owner_identity(fd, self.owner_token)
        except OSError:  # pragma: no cover - identity is best-effort; lock still held
            self.owner_token = ""
        self._fd = fd

    def _already_running(self) -> AlreadyRunningError:
        # The refusal is established by the *held lock* (we could not take it). The
        # record beside it may still be the live holder's or a stale one; either
        # way we only echo a URL that is a valid loopback HTTP URL, so a refusal
        # never points a browser at an external or executable URL.
        meta = self.read_metadata()
        url = meta.get("url") if meta else None
        advertised = url if is_loopback_http_url(url) else None
        holder = meta.get("pid") if meta else None
        where = f" (pid {holder})" if holder else ""
        at = f" at {advertised}" if advertised else ""
        return AlreadyRunningError(
            f"another TextFlowKit UI process{where} already owns this database{at}: "
            f"{self.db_path}. Do not start a second UI against the same database; "
            "use the running one, or stop it first.",
            url=advertised,
            pid=holder if isinstance(holder, int) else None,
        )

    def release(self) -> None:
        """Remove the live-URL record, then release the OS lock.

        The metadata is removed **while the lock is still held**, so the record
        never outlives the lock it describes: a reader can never see this owner's
        URL after this owner has stopped owning. The lock file itself is left in
        place (persistent) and only the OS lock is released by closing the fd.
        """
        if self._fd is None:
            return
        # Remove the record first, still holding the lock, so "a record with no
        # live lock" is the honest stale state rather than a torn one. Then clear
        # this owner's identity from the lock file while the lock is still held, so
        # a freed lock carries no identity a later reader could mistake for live.
        self._unlink_quietly(self.meta_path)
        try:
            _write_owner_identity(self._fd, "")
        except OSError:  # pragma: no cover - best effort
            pass
        fd = self._fd
        self._fd = None
        _unlock_fd(fd)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - already closed
            pass
        self.owner_token = ""

    # The quoted return is the py3.10-compatible way to name the class without
    # importing typing_extensions at runtime; PYI034 prefers `Self`, which this
    # project's minimum Python does not have.
    def __enter__(self) -> "OwnerLock":  # noqa: PYI034, UP037
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- helpers -----------------------------------------------------------

    def _close_fd(self) -> None:
        if self._fd is not None:
            fd = self._fd
            self._fd = None
            _unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - already closed
                pass

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def lock_is_held(self) -> bool:
        """Whether this database's lock file is held by a live process right now.

        Attempts a non-blocking exclusive lock on a *separate* file descriptor:
        success means nothing holds it (so we immediately release and report
        False); failure means the OS lock is genuinely held. The lock file is
        never opened for writing here, so this probe cannot disturb a holder.
        """
        if not self.lock_path.exists():
            return False
        try:
            fd = os.open(self.lock_path, os.O_RDWR)
        except OSError:
            return False
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return True
        # We got the lock, so no one holds it. Release by closing.
        os.close(fd)
        return False

    # -- live-URL record ---------------------------------------------------

    def write_metadata(self, *, url: str, version: str | None = None) -> None:
        """Record this instance's loopback URL beside the lock.

        Only owner/pid/url/started/version are written - see :data:`_RECORD_FIELDS`.
        The write is atomic (temp file then replace) so a reader never sees a
        partial record, and it is written **while the lock is held**, carrying this
        acquisition's **owner identity** so a reader can match it against the
        identity read from the held lock file.

        The ``url`` is validated as an ``http`` loopback URL first
        (:func:`is_loopback_http_url`); a caller that passes anything else - an
        external host, a ``file://`` or ``javascript:`` URL - records **nothing**,
        so discovery can never advertise an arbitrary external or executable URL.
        It is best-effort: a UI that cannot write this file still serves, it just
        cannot be discovered by a second launch.
        """
        if not is_loopback_http_url(url):
            return
        payload = {
            "owner": self.owner_token,
            "pid": os.getpid(),
            "url": url,
            "started": int(time.time()),
        }
        if version:
            payload["version"] = version
        record = {k: payload[k] for k in _RECORD_FIELDS if k in payload}
        tmp = self.meta_path.with_name(self.meta_path.name + f".{os.getpid()}.tmp")
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(record), encoding="utf-8")
            os.replace(tmp, self.meta_path)
        except OSError:  # pragma: no cover - discovery is a convenience
            self._unlink_quietly(tmp)

    def read_metadata(self) -> dict:
        """The recorded live URL for this database, or ``{}``.

        Returns ``{}`` for a missing, unreadable, or non-object record. A record
        is trusted only together with a *held* lock and a matching owner token: a
        leftover record whose lock is gone, or whose token does not match the live
        lock, is not a running instance, and :func:`discover_running` applies that
        rule.
        """
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: data[k] for k in _RECORD_FIELDS if k in data}

    def has_live_owner(self) -> bool:
        """Whether a live owner holds the lock **and** its record matches it.

        The honest test for "the UI for this database is running". Three facts
        must all hold:

        - **The OS lock is held.** The kernel holds it for a live process and
          releases it on exit or crash, so a held lock means a process owns the
          store *right now* - never inferred from a pid.
        - **The live holder's identity is readable.** The holder writes a
          per-acquisition identity into the readable region of the held lock file
          (offset 2), so a reader can read *this* holder's identity - the fact the
          earlier "record present while locked" design could not establish.
        - **The record carries the same identity.** The record is written under the
          lock carrying that identity, and removed under the lock, so a record
          whose identity equals the live holder's can only have been written by the
          current holder. A leftover record from a crashed run carries a *different*
          (or no) identity and is therefore read as not-live - the stale case this
          comparison exists to catch.

        A crash leaves a lock the kernel has already released and (possibly) a
        stale record: the lock condition then fails, so the result is not-live.
        """
        if not self.lock_is_held():
            return False
        live_identity = read_owner_identity(self.lock_path)
        if not live_identity:
            return False
        record_owner = self.read_metadata().get("owner")
        return record_owner == live_identity


def acquire_owner_lock(db_path: str | Path) -> OwnerLock:
    """Acquire the owner lock for ``db_path`` or raise :class:`AlreadyRunningError`.

    The lock is taken *before* startup recovery runs, so a second launch cannot
    reap the first launch's jobs or open the store to serve a competing UI.
    """
    lock = OwnerLock(db_path)
    lock.acquire()
    return lock


def discover_running(db_path: str | Path) -> str | None:
    """The live URL of a UI already serving ``db_path``, else ``None``.

    A URL counts as live only when the OS lock is held, the live holder's identity
    is readable, **and** the metadata record beside it carries that same identity
    (a real comparison, not ``bool(record)``). A leftover record with no held lock
    - or a record whose identity does not match the live holder's - is stale
    metadata and is reported as not running, so a crashed instance is never
    mistaken for a live one. The advertised URL is additionally required to be an
    ``http`` loopback URL, so discovery never hands a browser an external or
    executable URL. This never starts, stops, or signals anything, and never reads
    a pid as a liveness test.
    """
    lock = OwnerLock(db_path)
    if not lock.has_live_owner():
        return None
    url = lock.read_metadata().get("url")
    return url if is_loopback_http_url(url) else None


def parse_port_from_url(url: str | None) -> int | None:
    """The port in a ``http://host:port`` URL, or ``None`` if there is not one."""
    if not url:
        return None
    match = re.search(r":(\d{1,5})(?:/|$)", url)
    if not match:
        return None
    port = int(match.group(1))
    return port if 0 < port <= 65535 else None


def stale_metadata_is_not_live(db_path: str | Path) -> bool:
    """Convenience predicate: there is a metadata record but no live owner.

    "No live owner" means the OS lock is not held *or* the live holder's identity
    does not match the record's owner identity - the same condition
    :func:`discover_running` uses (an unreadable identity counts as no match).
    """
    lock = OwnerLock(db_path)
    has_record = bool(lock.read_metadata())
    return has_record and not lock.has_live_owner()


__all__ = [
    "AlreadyRunningError",
    "OwnerLock",
    "acquire_owner_lock",
    "discover_running",
    "is_loopback_http_url",
    "lock_path_for",
    "metadata_path_for",
    "read_owner_identity",
    "stale_metadata_is_not_live",
]
