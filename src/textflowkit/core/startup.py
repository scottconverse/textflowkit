"""Startup recovery: fail orphaned jobs exactly once, before reads are served.

A durable store can hold jobs left mid-flight by a process that died. After a
restart there is no worker for them, so leaving them alone reports a job that
will never finish - the audit's QA-001: crash, restart, then poll only ``/health``
and job status, and the row reads ``running`` forever because nothing ever runs
the recovery. Recovery must therefore be an explicit part of *startup*, owned by
the process that owns the store, and it must run **before** the adapters serve a
read.

Design constraints this module satisfies, each of which was a failure mode to
close:

- **Exactly once per owning store.** ``StartupRecovery`` remembers the store it
  has already recovered (by identity) and is a no-op on every later call. So the
  HTTP lifespan, the MCP lifespan, and a lazy executor start can all ask for
  recovery without any of them reaping a second time - and none of them reaps on
  a per-request path.
- **Not on import.** Nothing here runs at module import. Recovery only happens
  when ``run()`` (or ``recover_startup()``) is called from a lifecycle hook.
- **One owning process per store.** Reaping is safe only under the documented
  single-writer deployment model; a job this process is *running* is never a
  candidate because it is created by this process after recovery has already run.
- **Visible on failure.** A store that rejects the recovery write raises
  ``StartupRecoveryError``: startup must fail rather than serve a healthy-looking
  server whose orphans still read ``running``.
"""

from __future__ import annotations

import threading

from textflowkit.core.jobs import JobStore, get_default_store

REAP_REASON = "interrupted by restart; no worker is running this job"


class StartupRecoveryError(RuntimeError):
    """Startup recovery could not persist; the server must not report ready.

    Distinct from a store fault raised mid-flight: this is raised from the
    *startup* path, before any read is served, and it means orphaned rows are
    still non-terminal. A caller that catches it (a lifespan hook) should let it
    propagate so the server fails to start rather than admit traffic.
    """


class StartupRecovery:
    """Owns the one-per-store reap that startup performs.

    An instance is bound to a single store. ``run()`` reaps that store's
    orphaned rows the first time it is called and returns ``True``; every later
    call returns ``False`` without touching the store. The guard is a lock plus a
    flag on the instance, so concurrent startup callers on one owner race to a
    single reap rather than one each.
    """

    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._done = False

    @property
    def store(self) -> JobStore:
        return self._store

    def run(self) -> bool:
        """Reap orphaned rows once for this owner.

        Returns True when this call performed the reap, False when recovery had
        already run for this owner. Raises ``StartupRecoveryError`` when the
        store rejects the recovery write, so startup fails visibly instead of
        serving a false ready.
        """
        with self._lock:
            if self._done:
                return False
            try:
                self._store.reap_incomplete(reason=REAP_REASON)
            except Exception as exc:  # surfaced as a startup fault
                raise StartupRecoveryError(
                    f"startup recovery could not fail orphaned jobs: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._done = True
            return True

    def reset(self) -> None:
        """Forget that recovery ran (tests / an explicitly new owner epoch)."""
        with self._lock:
            self._done = False


# Process-wide owner per store, keyed by store identity.
_owner_lock = threading.Lock()
_owners: dict[int, StartupRecovery] = {}


def recovery_for(store: JobStore) -> StartupRecovery:
    """The shared recovery owner for ``store``, keyed by store identity.

    One owner per store object, so the executor and both adapters, all holding
    the process-default store, share a single recovery that runs once. A caller
    holding a *different* store gets that store's own owner - recovery is a
    property of the store, not of the process.
    """
    with _owner_lock:
        owner = _owners.get(id(store))
        if owner is None or owner.store is not store:
            owner = StartupRecovery(store)
            _owners[id(store)] = owner
        return owner


def recover_startup(store: JobStore | None = None) -> bool:
    """Run startup recovery for a store (the process default by default).

    Returns True when this call performed the reap, False when recovery had
    already run. Raises ``StartupRecoveryError`` when the reap could not be
    persisted. This is the one entry point the adapter lifecycles call; it never
    runs on import and never on a per-request path.
    """
    target = store if store is not None else get_default_store()
    return recovery_for(target).run()


def reset_startup_recovery() -> None:
    """Forget every recovery owner (tests, embedding, a new store epoch).

    The owner map is keyed by store identity and would otherwise hold a stale
    owner for a store object that has been replaced. Callers that reset the
    default store should call this too, so a fresh store starts with a fresh,
    unspent recovery.
    """
    with _owner_lock:
        _owners.clear()
