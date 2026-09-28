"""The V0.9 lifecycle in the store: transitions, the atomic claim, migration.

No ADK, Ollama, Telegram or Linear involved.
"""

import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime

import pytest

from cycle_runner.work_requests import SCHEMA_VERSION, InvalidTransition, WorkRequestStore


@pytest.fixture
def store(tmp_path):
    return WorkRequestStore(tmp_path / "wr.db")


def _approve(store, n=1, issue_id="SB-1"):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{n}", issue_id=issue_id, approved_by="telegram:1",
        approved_at=datetime.now(UTC), cycle_number=9, title_at_approval="t", rationale="r",
    )
    return request.work_request_id


# --- the happy path -----------------------------------------------------------


def test_pending_to_claimed(store):
    wr = _approve(store)

    claimed = store.claim_next("fake@host:1")

    assert (claimed.work_request_id, claimed.status, claimed.claimed_by) == (wr, "claimed", "fake@host:1")
    assert claimed.claimed_at is not None and claimed.started_at is None


def test_claimed_to_running(store):
    wr = _approve(store)
    store.claim_next("x")

    running = store.start(wr)

    assert running.status == "running" and running.started_at is not None


def test_running_to_completed(store):
    wr = _approve(store)
    store.claim_next("x")
    store.start(wr)

    done = store.finish(wr, "completed", "Fake execution completed.")

    assert (done.status, done.result_message) == ("completed", "Fake execution completed.")
    assert done.claimed_at <= done.started_at <= done.finished_at


def test_running_to_failed(store):
    wr = _approve(store)
    store.claim_next("x")
    store.start(wr)
    assert store.finish(wr, "failed", "boom").status == "failed"


def test_no_pending_requests_is_not_an_error(store):
    assert store.claim_next("x") is None


def test_the_oldest_pending_request_is_claimed_first(store):
    first, second = _approve(store, 1), _approve(store, 2)
    assert store.claim_next("x").work_request_id == first
    assert store.claim_next("x").work_request_id == second
    assert store.claim_next("x") is None


# --- invalid transitions ------------------------------------------------------


def test_an_already_claimed_request_cannot_be_claimed_again(store):
    wr = _approve(store)
    store.claim(wr, "a")

    assert store.claim(wr, "b") is None
    assert store.get(wr).claimed_by == "a"


@pytest.mark.parametrize(
    ("reach", "operation"),
    [
        ("pending", lambda s, wr: s.start(wr)),
        ("pending", lambda s, wr: s.finish(wr, "completed", "")),
        ("pending", lambda s, wr: s.release(wr)),
        ("claimed", lambda s, wr: s.finish(wr, "completed", "")),
        ("claimed", lambda s, wr: s.abandon(wr, "")),
        ("running", lambda s, wr: s.start(wr)),
        ("running", lambda s, wr: s.release(wr)),
        ("completed", lambda s, wr: s.start(wr)),
        ("completed", lambda s, wr: s.finish(wr, "failed", "")),
        ("completed", lambda s, wr: s.release(wr)),
        ("completed", lambda s, wr: s.abandon(wr, "")),
        ("failed", lambda s, wr: s.finish(wr, "completed", "")),
    ],
)
def test_invalid_transitions_are_rejected(store, reach, operation):
    wr = _approve(store)
    _advance(store, wr, reach)

    with pytest.raises(InvalidTransition, match=f"{wr} is {reach}"):
        operation(store, wr)
    assert store.get(wr).status == reach  # nothing changed


def test_completed_and_failed_requests_cannot_be_claimed(store):
    done, failed = _approve(store, 1), _approve(store, 2)
    _advance(store, done, "completed")
    _advance(store, failed, "failed")

    assert store.claim(done, "x") is None and store.claim(failed, "x") is None
    assert store.claim_next("x") is None


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [("pending", "running"), ("pending", "completed"), ("completed", "pending"), ("failed", "running"),
     ("running", "claimed"), ("completed", "failed")],
)
def test_the_database_itself_rejects_invalid_transitions(store, from_status, to_status):
    wr = _approve(store)
    _advance(store, wr, from_status)

    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError, match="invalid work request transition"):
        db.execute(f"UPDATE work_requests SET status = '{to_status}' WHERE id = 1")


def test_unknown_ids_are_invalid(store):
    with pytest.raises(InvalidTransition):
        store.start("WR-000099")
    with pytest.raises(InvalidTransition):
        store.start("SB-1")
    assert store.claim("WR-000099", "x") is None


# --- manual recovery ----------------------------------------------------------


def test_release_puts_a_stranded_claim_back_to_pending(store):
    wr = _approve(store)
    store.claim_next("crashed@host:1")

    released = store.release(wr)

    assert (released.status, released.claimed_by, released.claimed_at) == ("pending", None, None)
    assert store.claim_next("x").work_request_id == wr


def test_abandon_marks_a_stranded_run_failed_and_it_is_never_retried(store):
    wr = _approve(store)
    _advance(store, wr, "running")

    abandoned = store.abandon(wr, "executor process died")

    assert (abandoned.status, abandoned.result_message) == ("failed", "Abandoned: executor process died")
    assert store.claim_next("x") is None


# --- the atomic claim, under real concurrency ---------------------------------


def _race(path, contenders, code):
    """Start `contenders` OS processes that all run `code` at the same instant."""
    start_at = time.time() + 1.5
    script = textwrap.dedent(
        f"""
        import sys, time
        from cycle_runner.work_requests import WorkRequestStore
        store = WorkRequestStore({str(path)!r})
        me = sys.argv[1]
        while time.time() < {start_at}:
            pass
        {code}
        """
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script, f"p{i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(contenders)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    assert all(process.returncode == 0 for process in processes), [err for _, err in results]
    return [out.strip() for out, _ in results]


def test_exactly_one_of_many_processes_wins_a_claim_on_the_same_request(store):
    wr = _approve(store)

    outcomes = _race(store.path, 8, f"r = store.claim({wr!r}, me); print(r.claimed_by if r else 'already-claimed')")

    winners = [o for o in outcomes if o != "already-claimed"]
    assert len(winners) == 1
    assert outcomes.count("already-claimed") == 7
    assert store.get(wr).claimed_by == winners[0]


def test_concurrent_claim_next_hands_each_request_to_exactly_one_process(store):
    ids = {_approve(store, n) for n in range(1, 4)}

    outcomes = _race(store.path, 8, "r = store.claim_next(me); print(r.work_request_id if r else 'none')")

    claimed = [o for o in outcomes if o != "none"]
    assert sorted(claimed) == sorted(ids)  # each of the 3 claimed once, none twice
    assert outcomes.count("none") == 5


# --- migration from V0.8 ------------------------------------------------------


V08_SCHEMA = """
CREATE TABLE work_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id TEXT NOT NULL UNIQUE,
    issue_id          TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('pending')),
    approved_by       TEXT NOT NULL,
    approved_at       TEXT NOT NULL,
    cycle_number      INTEGER NOT NULL,
    title_at_approval TEXT NOT NULL,
    rationale         TEXT NOT NULL
);
"""


def test_a_v08_database_is_migrated_without_losing_anything(tmp_path):
    path = tmp_path / "v08.db"
    with sqlite3.connect(path) as db:
        db.executescript(V08_SCHEMA)
        for n in (1, 2, 3):
            db.execute(
                "INSERT INTO work_requests (recommendation_id, issue_id, status, approved_by, approved_at,"
                " cycle_number, title_at_approval, rationale) VALUES (?, 'SB-640', 'pending', 'telegram:1',"
                " '2026-09-27T22:54:39+00:00', 10, 'Prod admin password', 'Urgent')",
                (f"rec-{n}",),
            )
        db.execute("DELETE FROM work_requests WHERE id = 3")  # WR-000003 was used once: never reuse it

    store = WorkRequestStore(path)

    assert [(r.work_request_id, r.status, r.claimed_by) for r in store.list_all()] == [
        ("WR-000001", "pending", None), ("WR-000002", "pending", None),
    ]
    assert _approve(store, 9) == "WR-000004"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.claim_next("x").work_request_id == "WR-000001"  # the new lifecycle works


def test_opening_a_current_database_again_changes_nothing(tmp_path):
    path = tmp_path / "wr.db"
    wr = _approve(WorkRequestStore(path))
    _advance(WorkRequestStore(path), wr, "completed")

    assert WorkRequestStore(path).get(wr).status == "completed"


def _advance(store, wr, status):
    """Walk one request along the happy path (or to failed) up to `status`."""
    path = {"pending": [], "claimed": ["claim"], "running": ["claim", "start"],
            "completed": ["claim", "start", "complete"], "failed": ["claim", "start", "fail"]}[status]
    for step in path:
        if step == "claim":
            assert store.claim(wr, "x")
        elif step == "start":
            store.start(wr)
        elif step == "complete":
            store.finish(wr, "completed", "done")
        else:
            store.finish(wr, "failed", "boom")
