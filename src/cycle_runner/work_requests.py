"""Durable work requests: Cycle Runner's record of what a human approved for execution.

Linear is the source of truth for engineering work (the issue, its title,
status, priority, ...). This store is the source of truth for Cycle Runner's
execution intent: "a human approved SB-1234 for agent execution". It keeps
only what's needed to show what was approved and to hand it over later, not a
copy of the Linear issue.

V0.8 only creates requests, and every request stays pending: nothing consumes
them yet. The database allows no other status, so no code can pretend
otherwise.

This is the only module that knows the store is SQLite. It knows nothing
about ADK, Telegram or Linear.
"""

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DEFAULT_DB_PATH = "data/cycle-runner.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- shown as WR-000001; never reused
    recommendation_id TEXT NOT NULL UNIQUE,               -- one approval, one request: idempotency
    issue_id          TEXT NOT NULL,                      -- Linear issue id; Linear stays authoritative
    status            TEXT NOT NULL CHECK (status IN ('pending')),
    approved_by       TEXT NOT NULL,
    approved_at       TEXT NOT NULL,                      -- ISO 8601, UTC
    cycle_number      INTEGER NOT NULL,                   -- what the human saw, when they approved
    title_at_approval TEXT NOT NULL,
    rationale         TEXT NOT NULL
);
"""

WORK_REQUEST_ID = re.compile(r"^WR-(\d{6,})$")


class WorkRequest(BaseModel):
    work_request_id: str
    recommendation_id: str
    issue_id: str
    status: Literal["pending"]
    approved_by: str
    approved_at: datetime
    cycle_number: int
    title_at_approval: str
    rationale: str


def db_path_from_env() -> str:
    return os.environ.get("CYCLE_RUNNER_DB", DEFAULT_DB_PATH)


def open_store() -> "WorkRequestStore":
    return WorkRequestStore(db_path_from_env())


class WorkRequestStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation: commit on success, always close."""
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

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
                                           approved_at, cycle_number, title_at_approval, rationale)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
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
                ),
            )
            created = cursor.rowcount == 1
            row = db.execute(
                "SELECT * FROM work_requests WHERE recommendation_id = ?", (recommendation_id,)
            ).fetchone()
        return _work_request(row), created

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


def _work_request(row: sqlite3.Row) -> WorkRequest:
    fields = dict(row)
    return WorkRequest(work_request_id=f"WR-{fields.pop('id'):06d}", **fields)
