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

from textflowkit.core.jobs import Job, JobState, JobStore

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
    request          TEXT
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
    }
)

_CHECKPOINT_COLUMN = "checkpoint"


def _row_to_job(row: sqlite3.Row) -> Job:
    transcript = json.loads(row["transcript"]) if row["transcript"] else None
    outputs = json.loads(row["outputs"]) if row["outputs"] else []
    keys = set(row.keys())
    raw_checkpoint = row["checkpoint"] if "checkpoint" in keys else None
    checkpoint = json.loads(raw_checkpoint) if raw_checkpoint else None
    raw_request = row["request"] if "request" in keys else None
    request = json.loads(raw_request) if raw_request else None
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
                " progress, error, transcript, outputs, cancel_requested, checkpoint, request)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    def update(self, job_id: str, **fields: Any) -> Job | None:
        patch = {k: v for k, v in fields.items() if k in _MUTABLE}
        if not patch:
            return self.get(job_id)

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

    def list(self, *, limit: int = 50, state: JobState | None = None) -> list[Job]:
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

