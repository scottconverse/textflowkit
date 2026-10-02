"""Durable job store backed by SQLite.

Chosen over a hand-rolled file format because SQLite is stdlib, transactional,
and already safe for concurrent access. One connection guarded by a lock is
sufficient at this scale and keeps the implementation obvious.

Ordering uses the table's AUTOINCREMENT `seq`, not `created_at` - same reasoning
as the in-memory store: wall-clock ordering is not portable across platforms.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from textflowkit.core.jobs import (
    TERMINAL_STATES,
    Job,
    JobState,
    JobStore,
    ObservedJob,
    _snapshot,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT    NOT NULL UNIQUE,
    source           TEXT    NOT NULL,
    state            TEXT    NOT NULL,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    progress         TEXT    NOT NULL DEFAULT '',
    error            TEXT,
    transcript       TEXT,
    outputs          TEXT    NOT NULL DEFAULT '[]',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    checkpoint       TEXT,
    request          TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""

# Columns a caller may patch through update().
_MUTABLE = frozenset(
    {
        "state",
        "progress",
        "error",
        "transcript",
        "outputs",
        "cancel_requested",
        "checkpoint",
        "request",
        "attempt",
    }
)

_CHECKPOINT_COLUMN = "checkpoint"


def _supports_returning(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite runtime understands ``UPDATE ... RETURNING``.

    Needed because the project supports Python 3.10 and up, and the SQLite a
    given interpreter is linked against is not the interpreter's version: a
    CPython build can ship an SQLite older than 3.35, where ``RETURNING`` is a
    syntax error. The probe is a real statement that cannot match a row
    (``WHERE 0``), so running it has no effect on any data; a syntax error is
    the only failure it should ever raise on an unsupported runtime.
    """
    try:
        conn.execute("UPDATE jobs SET seq = seq WHERE 0 RETURNING seq").fetchall()
        return True
    except sqlite3.OperationalError:
        return False


def _row_to_job(row: sqlite3.Row) -> Job:
    transcript = json.loads(row["transcript"]) if row["transcript"] else None
    outputs = json.loads(row["outputs"]) if row["outputs"] else []
    keys = set(row.keys())
    raw_checkpoint = row["checkpoint"] if "checkpoint" in keys else None
    checkpoint = json.loads(raw_checkpoint) if raw_checkpoint else None
    raw_request = row["request"] if "request" in keys else None
    request = json.loads(raw_request) if raw_request else None
    attempt = row["attempt"] if "attempt" in keys else 0
    return Job(
        id=row["id"],
        source=row["source"],
        state=JobState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        progress=row["progress"] or "",
        error=row["error"],
        transcript=transcript,
        outputs=list(outputs),
        cancel_requested=bool(row["cancel_requested"]),
        checkpoint=checkpoint,
        request=request,
        attempt=int(attempt or 0),
    )


class SqliteJobStore(JobStore):
    """Durable job store. Jobs survive process restart."""

    def __init__(self, path: str | Path, *, max_jobs: int = 200) -> None:
        self.path = str(path)
        # ":memory:" is allowed and useful for tests.
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_locked()
            # Probing here means the schema exists, so the harmless ``WHERE 0``
            # statement inside has a table to name. The probe may open a write
            # transaction, so it is committed away with the rest of the setup.
            self._returning = _supports_returning(self._conn)
            self._conn.commit()
        self._max_jobs = max_jobs

    # -- interface ---------------------------------------------------------

    def create(self, source: str, *, request: dict[str, Any] | None = None) -> Job:
        now = time.time()
        job = Job(
            id=uuid.uuid4().hex[:12],
            source=source,
            state=JobState.PENDING,
            created_at=now,
            updated_at=now,
            request=request,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, source, state, created_at, updated_at,"
                " progress, error, transcript, outputs, cancel_requested, checkpoint,"
                " request, attempt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.source,
                    job.state.value,
                    job.created_at,
                    job.updated_at,
                    job.progress,
                    job.error,
                    None,
                    json.dumps([]),
                    int(job.cancel_requested),
                    None,
                    json.dumps(request) if request is not None else None,
                    job.attempt,
                ),
            )
            self._conn.commit()
            self._prune_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def observe(self, job_id: str) -> ObservedJob | None:
        """Snapshot one row under the lock.

        `_row_to_job` already returns a detached `Job` built from a single
        fetched row, so its fields are one instant's worth; taking the read under
        the lock and copying keeps the snapshot contract identical to the memory
        store's and immune to a later `claim`/`update` reusing this row object.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return _snapshot(_row_to_job(row)) if row else None

    def update(self, job_id: str, **fields: Any) -> Job | None:
        patch = {k: v for k, v in fields.items() if k in _MUTABLE}
        if not patch:
            return self.get(job_id)

        sets, values = self._encode_patch(patch)
        sets.append("updated_at = ?")
        values.append(time.time())
        values.append(job_id)

        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?",
                values,
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get(job_id)

    def claim(
        self,
        job_id: str,
        *,
        allowed_states: frozenset[JobState] | set[JobState],
        observed_attempt: int | None = None,
        advance_attempt: bool = False,
        **fields: Any,
    ) -> Job | None:
        """Transition only if the row's state (and attempt) match.

        The state test rides in the UPDATE's WHERE clause, so the check-and-set
        is one statement. SQLite serializes write transactions: one connection's
        committed write is visible to the next, so two handles over the same file
        cannot both win - the first commit changes the row, and the second's
        ``WHERE state IN (...)`` matches nothing and ``rowcount`` is 0. (SQLite
        has no row-level locks over the whole row for an UPDATE; it takes a
        database-wide write lock, which is what makes the second writer wait and
        then see the first's state.)

        ``observed_attempt``, when given, adds ``AND attempt = ?`` so a claim made
        against a previously observed failure is refused if the row has since run
        again and landed back on the same state. State alone cannot tell those two
        failures apart; the attempt counter can.

        ``advance_attempt`` adds ``attempt = attempt + 1`` to the same UPDATE, so
        the claim acquires its own identity atomically with the transition. The
        identity is then usable as the exact key a later cleanup or decision
        names, even for a claim that never reaches RUNNING.

        The returned row is the row the write actually produced (``RETURNING *``,
        or a read taken inside the same open transaction where that is
        unavailable), not a later re-read - so the ``attempt`` a resume pins its
        cleanup to is the generation the claim really acquired, even if another
        writer moves the row on immediately afterwards.
        """
        allowed = {state.value if isinstance(state, JobState) else str(state) for state in allowed_states}
        if not allowed:
            return None
        patch = {k: v for k, v in fields.items() if k in _MUTABLE}
        sets, values = self._encode_patch(patch)
        if advance_attempt:
            sets.append("attempt = attempt + 1")
        sets.append("updated_at = ?")
        values.append(time.time())
        placeholders = ", ".join("?" for _ in allowed)
        values.append(job_id)
        values.extend(sorted(allowed))
        attempt_clause = ""
        if observed_attempt is not None:
            attempt_clause = " AND attempt = ?"
            values.append(int(observed_attempt))

        with self._lock:
            written = self._write_and_return_locked(
                f"UPDATE jobs SET {', '.join(sets)}"
                f" WHERE id = ? AND state IN ({placeholders}){attempt_clause}",
                values,
                job_id,
            )
        if written is None:
            return None
        return _row_to_job(written)

    def update_owned(
        self,
        job_id: str,
        *,
        observed_attempt: int,
        allowed_states: frozenset[JobState] | set[JobState] = frozenset(
            {JobState.PENDING, JobState.RUNNING}
        ),
        refuse_if_cancelled: bool = True,
        **fields: Any,
    ) -> Job | None:
        """Patch a run-owned row in one guarded statement.

        Every ownership test rides in the UPDATE's ``WHERE``: the row must still
        be at ``observed_attempt``, still in an allowed (non-terminal) state, and
        - unless the caller is writing a checkpoint - still uncancelled. Because
        the test and the write are one statement, an acceptance or a newer claim
        that commits first makes this match nothing and return ``None``, so a
        stale stage label or an old attempt's checkpoint can never land on the
        newer row or replace an accepted ``cancelling`` label.
        """
        allowed = {state.value if isinstance(state, JobState) else str(state) for state in allowed_states}
        if not allowed:
            return None
        patch = {k: v for k, v in fields.items() if k in _MUTABLE}
        sets, values = self._encode_patch(patch)
        # `updated_at` alone is always patchable, so the SET clause is never
        # empty even when no recognized field was passed.
        sets.append("updated_at = ?")
        values.append(time.time())
        placeholders = ", ".join("?" for _ in allowed)
        values.append(job_id)
        values.append(int(observed_attempt))
        values.extend(sorted(allowed))
        cancel_clause = " AND cancel_requested = 0" if refuse_if_cancelled else ""
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)}"
                f" WHERE id = ? AND attempt = ? AND state IN ({placeholders})"
                + cancel_clause,
                values,
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get(job_id)

    def begin_attempt(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Start a run atomically, only from a still-PENDING, uncancelled row.

        The state/flag/generation tests ride in the UPDATE's WHERE clause, so the
        check-and-set is one statement: a queued cancellation that already landed
        CANCELLED makes the start match nothing and the worker does not resurrect
        it. ``observed_attempt`` adds ``AND attempt = ?`` so a stale queue entry
        cannot start a newer owner's generation.

        The returned row is the *actual row the write produced*, not a later
        read of the table. The attempt is read (under the lock) before the
        guarded write so the value the write produced is known locally, and the
        update re-pins that same attempt in its ``WHERE`` - so a concurrent
        writer that commits in between makes the update match nothing. The write
        then returns the row it wrote (``RETURNING *``, or - on a runtime without
        it - a re-read taken inside the same still-open transaction, before the
        commit), decoded through the normal row-to-job path. Nothing is
        fabricated onto a later read: every field of the returned snapshot is one
        the atomic write left on the row, so a concurrent writer that advances
        the row (or rewrites its checkpoint/request) after the commit cannot leak
        into an identity the run will pin a terminal decision to.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            previous = int(row["attempt"] or 0)
            if observed_attempt is not None and previous != int(observed_attempt):
                return None
            written = previous + 1
            written_row = self._write_and_return_locked(
                "UPDATE jobs SET state = ?, attempt = ?, updated_at = ?"
                " WHERE id = ? AND state = ? AND cancel_requested = 0"
                " AND attempt = ?",
                (
                    JobState.RUNNING.value,
                    written,
                    time.time(),
                    job_id,
                    JobState.PENDING.value,
                    previous,
                ),
                job_id,
            )
        if written_row is None:
            return None
        return _row_to_job(written_row)

    def _write_and_return_locked(
        self, sql: str, values: list[Any], job_id: str
    ) -> sqlite3.Row | None:
        """Run one guarded UPDATE and return the row it actually wrote.

        Caller holds ``self._lock``. The returned row is the write's own row, not
        a later table read: with ``RETURNING *`` the database hands back the
        post-update row as part of the statement, and on a runtime without it the
        row is re-read *before* the commit, inside the same transaction, while
        this connection still holds SQLite's write lock - so no other writer can
        commit in between and the read sees exactly this write. Either way the
        snapshot cannot be a later generation. Returns ``None`` when the guard
        matched no row (nothing was written).
        """
        with self._lock:
            if self._returning:
                cur = self._conn.execute(sql + " RETURNING *", values)
                row = cur.fetchone()
                self._conn.commit()
                return row
            cur = self._conn.execute(sql, values)
            if cur.rowcount == 0:
                self._conn.commit()
                return None
            # Same transaction, pre-commit: the write lock is still held, so this
            # read cannot observe another connection's later write.
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            self._conn.commit()
            return row

    def accept_cancel(self, job_id: str, *, observed_attempt: int | None = None) -> Job | None:
        """Cancel atomically, choosing the patch from the row's own state.

        One statement, so the "still PENDING" test and the write it selects are
        indivisible. SQLite evaluates every SET expression against the row's
        *pre-update* values, so the CASE on ``state`` reads the state the WHERE
        clause just admitted. A PENDING row becomes CANCELLED; a RUNNING row only
        records the accepted flag and progress, staying non-terminal until the run
        really stops.
        """
        attempt_clause = ""
        values: list[Any] = [
            # SET: the flag, then the state/progress chosen from the pre-update state.
            JobState.PENDING.value,
            JobState.CANCELLED.value,
            JobState.PENDING.value,
            "cancelled",
            "cancelling",
            time.time(),
            # WHERE: one row, still live, at the named generation.
            job_id,
            JobState.PENDING.value,
            JobState.RUNNING.value,
            JobState.DONE.value,
            JobState.ERROR.value,
            JobState.CANCELLED.value,
        ]
        if observed_attempt is not None:
            attempt_clause = " AND attempt = ?"
            values.append(int(observed_attempt))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET cancel_requested = 1,"
                " state = CASE WHEN state = ? THEN ? ELSE state END,"
                " progress = CASE WHEN state = ? THEN ? ELSE ? END,"
                " updated_at = ?"
                " WHERE id = ? AND state IN (?, ?)"
                " AND state NOT IN (?, ?, ?)" + attempt_clause,
                values,
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get(job_id)

    def finalize_done(
        self, job_id: str, *, observed_attempt: int, **fields: Any
    ) -> Job | None:
        """Choose CANCELLED/DONE for the owned generation, atomically.

        The DONE write is guarded on the owned attempt, a non-terminal row, and no
        accepted cancellation, so an accepted cancellation always wins. When it
        does not land, a second guarded write lands CANCELLED if - and only if -
        the *same* generation carries the accepted flag. Both writes are pinned to
        the same attempt and both refuse a terminal row, so neither can clobber a
        newer owner; a row that moved on gets None.
        """
        done_patch = {k: v for k, v in fields.items() if k in _MUTABLE}
        done_patch["state"] = JobState.DONE
        terminal = sorted(state.value for state in TERMINAL_STATES)
        placeholders = ", ".join("?" for _ in terminal)
        with self._lock:
            sets, values = self._encode_patch(done_patch)
            sets.append("updated_at = ?")
            values.append(time.time())
            values.append(job_id)
            values.append(int(observed_attempt))
            values.extend(terminal)
            cur = self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)}"
                f" WHERE id = ? AND attempt = ? AND cancel_requested = 0"
                f" AND state NOT IN ({placeholders})",
                values,
            )
            self._conn.commit()
            if cur.rowcount == 0:
                # An accepted cancellation owns this same generation. Land the
                # terminal CANCELLED, keeping any rendered outputs on the record.
                cancel_sets = ["state = ?", "progress = ?"]
                cancel_values: list[Any] = [JobState.CANCELLED.value, "cancelled"]
                if "outputs" in done_patch:
                    cancel_sets.append("outputs = ?")
                    cancel_values.append(json.dumps(list(done_patch["outputs"] or [])))
                cancel_sets.append("updated_at = ?")
                cancel_values.append(time.time())
                cancel_values.append(job_id)
                cancel_values.append(int(observed_attempt))
                cancel_values.extend(terminal)
                cur = self._conn.execute(
                    f"UPDATE jobs SET {', '.join(cancel_sets)}"
                    f" WHERE id = ? AND attempt = ? AND cancel_requested = 1"
                    f" AND state NOT IN ({placeholders})",
                    cancel_values,
                )
                self._conn.commit()
            landed = cur.rowcount
        if landed == 0:
            return None
        return self.get(job_id)

    @staticmethod
    def _encode_patch(patch: dict[str, Any]) -> tuple[list[str], list[Any]]:
        """Turn field values into SET clauses and bound parameters."""
        sets: list[str] = []
        values: list[Any] = []
        for key, value in patch.items():
            sets.append(f"{key} = ?")
            if key == "state" and isinstance(value, JobState):
                values.append(value.value)
            elif key == "transcript":
                values.append(json.dumps(value) if value is not None else None)
            elif key == "outputs":
                values.append(json.dumps(list(value or [])))
            elif key in {"checkpoint", "request"}:
                values.append(json.dumps(value) if value is not None else None)
            elif key == "cancel_requested":
                values.append(int(bool(value)))
            else:
                values.append(value)
        return sets, values

    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        with self._lock:
            if state is None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY seq DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE state = ? ORDER BY seq DESC LIMIT ?",
                    (state.value, limit),
                ).fetchall()
        return [_row_to_job(r) for r in rows]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs")
            self._conn.commit()

    # -- internals ---------------------------------------------------------

    def _migrate_locked(self) -> None:
        """Add columns introduced after the original schema."""
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if _CHECKPOINT_COLUMN not in existing:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint TEXT")
        if "request" not in existing:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN request TEXT")
        if "attempt" not in existing:
            # Files written before attempts existed: their terminal rows describe
            # attempt 0, which is what the counter starts at.
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
            )

    def _prune_locked(self) -> None:
        """Drop the oldest terminal jobs once over capacity."""
        rows = self._conn.execute(
            "SELECT seq FROM jobs WHERE state IN (?, ?, ?) ORDER BY seq ASC",
            (JobState.DONE.value, JobState.ERROR.value, JobState.CANCELLED.value),
        ).fetchall()
        excess = self._count_locked() - self._max_jobs
        if excess <= 0 or not rows:
            return
        doomed = [r["seq"] for r in rows[:excess]]
        if doomed:
            self._conn.executemany("DELETE FROM jobs WHERE seq = ?", [(s,) for s in doomed])
            self._conn.commit()

    def _count_locked(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

