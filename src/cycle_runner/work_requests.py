"""Durable work requests: Cycle Runner's record of what a human approved, and what became of it.

Linear is the source of truth for engineering work (the issue, its title,
status, priority, ...). This store is the source of truth for Cycle Runner's
execution intent and execution state: "a human approved SB-1234 for agent
execution", then "an executor claimed it, ran it, and it completed". It keeps
only what's needed for that, not a copy of the Linear issue.

Lifecycle (TRANSITIONS below is the only definition of it; the database
trigger is generated from it):

    pending ──claim──► claimed ──start──► running ──finish──► completed
       ▲                  │   │               └────finish──► failed
       └────release───────┘   └─fail_to_start─► failed     running ──abandon──► failed

- claimed and running are different on purpose. claimed means the executor
  was never called, so nothing ran and release is safe. running means work
  may have happened, so it's never retried automatically; abandon marks it
  failed for a human to look at.
- completed and failed are final.
- Every transition is a single compare-and-set UPDATE (WHERE status = the
  expected one), so two processes can't both make the same transition.

This is the only module that knows the store is SQLite. It knows nothing
about ADK, Telegram, Linear or executors.
"""

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DEFAULT_DB_PATH = "data/cycle-runner.db"
SCHEMA_VERSION = 3  # 0/1: V0.8 (pending only). 2: V0.9 lifecycle. 3: V1.1 project_id.

Status = Literal["pending", "claimed", "running", "completed", "failed"]
STATUSES = ("pending", "claimed", "running", "completed", "failed")
TRANSITIONS = {
    ("pending", "claimed"),  # claim
    ("claimed", "running"),  # start
    ("running", "completed"),  # finish
    ("running", "failed"),  # finish, or abandon a stranded run
    ("claimed", "pending"),  # release a stranded claim (nothing ran)
    ("claimed", "failed"),  # couldn't start: no workspace for it (nothing ran)
}

TABLE = f"""
CREATE TABLE {{name}} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- shown as WR-000001; never reused
    recommendation_id TEXT NOT NULL UNIQUE,               -- one approval, one request: idempotency
    issue_id          TEXT NOT NULL,                      -- Linear issue id; Linear stays authoritative
    status            TEXT NOT NULL CHECK (status IN {STATUSES}),
    approved_by       TEXT NOT NULL,
    approved_at       TEXT NOT NULL,                      -- ISO 8601, UTC
    cycle_number      INTEGER NOT NULL,                   -- what the human saw, when they approved
    title_at_approval TEXT NOT NULL,
    rationale         TEXT NOT NULL,
    claimed_by        TEXT,                               -- executor name@host:pid
    claimed_at        TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    result_message    TEXT,
    project_id        TEXT                                -- Linear's repo label (MT, TRD, ...); NULL before V1.1
)
"""

# Enforced by the database too, whoever writes (a bug, or a human with the
# sqlite3 shell). The transition list is generated from TRANSITIONS, not
# written twice.
TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS work_request_transitions
    BEFORE UPDATE OF status ON work_requests
    WHEN NEW.status <> OLD.status AND OLD.status || '->' || NEW.status NOT IN ({allowed})
    BEGIN
        SELECT RAISE(ABORT, 'invalid work request transition');
    END
    """.format(allowed=", ".join(f"'{a}->{b}'" for a, b in sorted(TRANSITIONS))),
    """
    CREATE TRIGGER IF NOT EXISTS work_request_starts_pending
    BEFORE INSERT ON work_requests
    WHEN NEW.status <> 'pending'
    BEGIN
        SELECT RAISE(ABORT, 'a new work request must be pending');
    END
    """,
)

V08_COLUMNS = (
    "id, recommendation_id, issue_id, status, approved_by, approved_at, "
    "cycle_number, title_at_approval, rationale"
)

WORK_REQUEST_ID = re.compile(r"^WR-(\d{6,})$")


class WorkRequest(BaseModel):
    work_request_id: str
    recommendation_id: str
    issue_id: str
    status: Status
    approved_by: str
    approved_at: datetime
    cycle_number: int
    title_at_approval: str
    rationale: str
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_message: str | None = None
    project_id: str | None = None


class InvalidTransition(Exception):
    """The request isn't in the state this operation requires (or doesn't exist)."""


def db_path_from_env() -> str:
    return os.environ.get("CYCLE_RUNNER_DB", DEFAULT_DB_PATH)


def open_store() -> "WorkRequestStore":
    return WorkRequestStore(db_path_from_env())


class WorkRequestStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation: commit on success, always close.

        timeout: a writer waits up to 10s for another process's write lock
        instead of failing, which is what makes concurrent claims block and
        then lose cleanly.
        """
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        """Bring the file to SCHEMA_VERSION in one transaction; a no-op when it's current."""
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'work_requests'"
            ).fetchone()
            if not exists:
                db.execute(TABLE.format(name="work_requests"))
            elif version < 2:
                # V0.8 -> V0.9. SQLite can't change a CHECK constraint, so rebuild
                # the table, keeping every row, id and the never-reuse counter.
                sequence = db.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'work_requests'"
                ).fetchone()
                db.execute(TABLE.format(name="work_requests_v2"))
                db.execute(
                    f"INSERT INTO work_requests_v2 ({V08_COLUMNS}) SELECT {V08_COLUMNS} FROM work_requests"
                )
                db.execute("DROP TABLE work_requests")
                db.execute("ALTER TABLE work_requests_v2 RENAME TO work_requests")
                if sequence:
                    db.execute(
                        "UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'work_requests'",
                        (sequence[0],),
                    )
            elif version < 3:
                # V0.9 -> V1.1: a new nullable column; existing rows have no project.
                db.execute("ALTER TABLE work_requests ADD COLUMN project_id TEXT")
            # Recreate the triggers every time so they always match TRANSITIONS
            # (CREATE ... IF NOT EXISTS would keep an older version's rules).
            db.execute("DROP TRIGGER IF EXISTS work_request_transitions")
            db.execute("DROP TRIGGER IF EXISTS work_request_starts_pending")
            for trigger in TRIGGERS:  # one at a time: executescript would commit mid-migration
                db.execute(trigger)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK") if db.in_transaction else None
            raise
        finally:
            db.close()

    # --- approval (V0.8) --------------------------------------------------------

    def create_for_approval(
        self,
        *,
        recommendation_id: str,
        issue_id: str,
        approved_by: str,
        approved_at: datetime,
        cycle_number: int,
        title_at_approval: str,
        rationale: str,
        project_id: str | None = None,
    ) -> tuple[WorkRequest, bool]:
        """Record an approval as a pending work request, at most once per recommendation.

        Returns the request and whether this call created it. Approving the same
        recommendation again (a retry, a duplicate update, a crash between this
        write and anything after it) returns the existing request instead: the
        UNIQUE constraint on recommendation_id makes that a database guarantee,
        not a hope about message delivery.
        """
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO work_requests (recommendation_id, issue_id, status, approved_by,
                                           approved_at, cycle_number, title_at_approval, rationale,
                                           project_id)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                ON CONFLICT (recommendation_id) DO NOTHING
                """,
                (
                    recommendation_id,
                    issue_id,
                    approved_by,
                    approved_at.isoformat(),
                    cycle_number,
                    title_at_approval,
                    rationale,
                    project_id,
                ),
            )
            created = cursor.rowcount == 1
            row = db.execute(
                "SELECT * FROM work_requests WHERE recommendation_id = ?", (recommendation_id,)
            ).fetchone()
        return _work_request(row), created

    # --- execution lifecycle (V0.9) ---------------------------------------------

    def claim_next(self, claimed_by: str) -> WorkRequest | None:
        """Atomically claim the oldest pending request, or return None if there is none.

        One UPDATE statement: it finds and claims the row under SQLite's write
        lock, so two processes can never claim the same request.
        """
        with self._connect() as db:
            row = db.execute(
                """
                UPDATE work_requests SET status = 'claimed', claimed_by = ?, claimed_at = ?
                WHERE id = (SELECT id FROM work_requests WHERE status = 'pending' ORDER BY id LIMIT 1)
                  AND status = 'pending'
                RETURNING *
                """,
                (claimed_by, _now()),
            ).fetchone()
        return _work_request(row) if row else None

    def claim(self, work_request_id: str, claimed_by: str) -> WorkRequest | None:
        """Atomically claim one specific request. None if it isn't pending (or doesn't exist)."""
        try:
            return self._transition(
                work_request_id, "pending", "claimed", claimed_by=claimed_by, claimed_at=_now()
            )
        except InvalidTransition:
            return None

    def start(self, work_request_id: str) -> WorkRequest:
        return self._transition(work_request_id, "claimed", "running", started_at=_now())

    def finish(self, work_request_id: str, outcome: Literal["completed", "failed"], message: str) -> WorkRequest:
        return self._transition(
            work_request_id, "running", outcome, finished_at=_now(), result_message=message
        )

    def fail_to_start(self, work_request_id: str, reason: str) -> WorkRequest:
        """A claimed request that can't be executed (e.g. no workspace for its project).

        Nothing ran, so it isn't "running"; releasing it would only fail again.
        """
        return self._transition(
            work_request_id, "claimed", "failed", finished_at=_now(), result_message=f"Not started: {reason}"
        )

    def release(self, work_request_id: str) -> WorkRequest:
        """Manual recovery of a stranded claim: back to pending. Safe, because nothing ran."""
        return self._transition(work_request_id, "claimed", "pending", claimed_by=None, claimed_at=None)

    def abandon(self, work_request_id: str, reason: str) -> WorkRequest:
        """Manual recovery of a stranded run: failed, for a human to look at. Never retried."""
        return self._transition(
            work_request_id, "running", "failed", finished_at=_now(), result_message=f"Abandoned: {reason}"
        )

    # --- one-off repair (V1.2) ---------------------------------------------------

    def backfill_project(self, work_request_id: str, project_id: str) -> WorkRequest:
        """Record the project of a request approved before V1.1 recorded projects.

        Narrow on purpose: only a pending request with no project, so nothing
        that ran or was recorded at approval can be rewritten. Setting the
        project it already has is a no-op. The caller checks the value (the
        issue's repo label in Linear); this only stores it.
        """
        row_id = _row_id(work_request_id)
        with self._connect() as db:
            row = db.execute(
                "UPDATE work_requests SET project_id = ? "
                "WHERE id = ? AND status = 'pending' AND project_id IS NULL RETURNING *",
                (project_id, row_id),
            ).fetchone()
        if row is not None:
            return _work_request(row)
        current = self.get(work_request_id)
        if current is None:
            raise InvalidTransition(f"{work_request_id} does not exist")
        if current.project_id == project_id:
            return current
        if current.project_id is not None:
            raise InvalidTransition(f"{work_request_id} already has project {current.project_id}")
        raise InvalidTransition(f"{work_request_id} is {current.status}; only a pending request's project can be set")

    def _transition(self, work_request_id: str, from_status: str, to_status: str, **fields) -> WorkRequest:
        """Compare-and-set: change status only if it is still from_status."""
        assert (from_status, to_status) in TRANSITIONS, (from_status, to_status)
        row_id = _row_id(work_request_id)
        assignments = ", ".join(["status = ?"] + [f"{column} = ?" for column in fields])
        with self._connect() as db:
            row = db.execute(
                f"UPDATE work_requests SET {assignments} WHERE id = ? AND status = ? RETURNING *",
                (to_status, *fields.values(), row_id, from_status),
            ).fetchone()
        if row is None:
            current = self.get(work_request_id)
            state = current.status if current else "unknown"
            raise InvalidTransition(f"{work_request_id} is {state}, not {from_status}")
        return _work_request(row)

    # --- reading ----------------------------------------------------------------

    def get(self, work_request_id: str) -> WorkRequest | None:
        match = WORK_REQUEST_ID.match(work_request_id)
        if not match:
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM work_requests WHERE id = ?", (int(match[1]),)).fetchone()
        return _work_request(row) if row else None

    def list_all(self) -> list[WorkRequest]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM work_requests ORDER BY id").fetchall()
        return [_work_request(row) for row in rows]


def _row_id(work_request_id: str) -> int:
    match = WORK_REQUEST_ID.match(work_request_id)
    if not match:
        raise InvalidTransition(f"{work_request_id!r} is not a work request id")
    return int(match[1])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _work_request(row: sqlite3.Row) -> WorkRequest:
    fields = dict(row)
    return WorkRequest(work_request_id=f"WR-{fields.pop('id'):06d}", **fields)
