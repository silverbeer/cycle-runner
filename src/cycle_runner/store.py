"""Cycle Runner application state: the engineering cycle and its issues.

This is the source of truth for what the cycle actually is, and it survives
restarts. It's separate from ADK's SessionService, which only remembers the
conversation.

This is the only module that knows the state lives in SQLite. Swapping storage
later means rewriting this file, not the tools or the agent.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_DB_PATH = "data/cycle-runner.db"

CYCLE_STATUSES = ("active", "completed")
ISSUE_STATUSES = ("todo", "in_progress", "done", "blocked")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS cycles (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN {CYCLE_STATUSES}),
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL
);

-- At most one active cycle: "the current cycle" is never ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_cycle
    ON cycles (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS issues (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN {ISSUE_STATUSES}),
    cycle_id TEXT NOT NULL REFERENCES cycles (id)
);
"""


@dataclass(frozen=True)
class Cycle:
    id: str
    name: str
    goal: str
    status: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    status: str
    cycle_id: str


def db_path_from_env() -> str:
    return os.environ.get("CYCLE_RUNNER_DB", DEFAULT_DB_PATH)


class CycleStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation: commit on success, always close."""
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def add_cycle(self, cycle: Cycle) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO cycles VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cycle.id,
                    cycle.name,
                    cycle.goal,
                    cycle.status,
                    cycle.start_date.isoformat(),
                    cycle.end_date.isoformat(),
                ),
            )

    def add_issue(self, issue: Issue) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?)",
                (issue.id, issue.title, issue.status, issue.cycle_id),
            )

    def get_cycle(self, cycle_id: str) -> Cycle | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        return _cycle(row) if row else None

    def get_issue(self, issue_id: str) -> Issue | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return Issue(*row) if row else None

    def get_active_cycle(self) -> Cycle | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cycles WHERE status = 'active'").fetchone()
        return _cycle(row) if row else None

    def list_issues(self, cycle_id: str) -> list[Issue]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM issues WHERE cycle_id = ? ORDER BY rowid", (cycle_id,)
            ).fetchall()
        return [Issue(*row) for row in rows]


def _cycle(row: tuple) -> Cycle:
    id_, name, goal, status, start, end = row
    return Cycle(id_, name, goal, status, date.fromisoformat(start), date.fromisoformat(end))
