"""Delivery approvals in the store: bound to one exact commit, immutable, never deleted."""

import sqlite3
from datetime import UTC, datetime

import pytest

from cycle_runner.work_requests import InvalidTransition, WorkRequestStore

SHA_A, SHA_B, BASE = "a" * 40, "b" * 40, "c" * 40
EVIDENCE = {"files": ["src/hello.py"], "subject": "SB-1: Add greet", "tests": "Ran 2 tests OK"}


@pytest.fixture
def store(tmp_path):
    return WorkRequestStore(tmp_path / "wr.db")


def _finished(store, outcome="changed", n=1, commit=SHA_A):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{n}", issue_id=f"SB-{n}", approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=1, title_at_approval="Add greet", rationale="r", project_id="DEMO",
    )
    wr = request.work_request_id
    store.claim(wr, "x")
    store.start(wr)
    delivery = {"branch": f"cycle-runner/{wr}", "commit_sha": commit} if outcome == "changed" else {}
    store.finish(wr, outcome, "m", **delivery)
    return wr


def _approve(store, wr, commit=SHA_A):
    return store.approve_delivery(wr, commit_sha=commit, base_sha=BASE, repository="silverbeer/sandbox",
                                  evidence=EVIDENCE, approved_by="cli:tom@host")


def test_an_approval_records_the_exact_commit_and_what_was_reviewed(store):
    wr = _finished(store)

    approval = _approve(store, wr)

    assert approval.approval_id == "APR-000001" and approval.status == "approved"
    assert (approval.work_request_id, approval.commit_sha, approval.branch, approval.base_sha) == \
        (wr, SHA_A, f"cycle-runner/{wr}", BASE)
    assert (approval.project_id, approval.repository, approval.approved_by) == \
        ("DEMO", "silverbeer/sandbox", "cli:tom@host")
    assert approval.evidence == EVIDENCE and approval.approved_at is not None
    assert store.live_approval(wr) == approval


@pytest.mark.parametrize("outcome", ["no_change", "failed"])
def test_no_change_and_failed_requests_can_never_be_approved(store, outcome):
    wr = _finished(store, outcome)
    with pytest.raises(InvalidTransition, match="only a changed, committed request"):
        _approve(store, wr)
    assert store.approvals_for(wr) == []


def test_only_the_requests_own_commit_can_be_approved(store):
    wr = _finished(store)
    with pytest.raises(InvalidTransition, match=f"commit is {SHA_A}, not {SHA_B}"):
        _approve(store, wr, commit=SHA_B)


@pytest.mark.parametrize("bad", ["abc123", "A" * 40, "g" * 40, SHA_A + "0"])
def test_approvals_need_full_shas(store, bad):
    wr = _finished(store)
    with pytest.raises(InvalidTransition, match="full 40-character SHAs"):
        _approve(store, wr, commit=bad)


def test_a_request_has_at_most_one_live_approval(store):
    wr = _finished(store)
    first = _approve(store, wr)
    with pytest.raises(InvalidTransition, match=f"already has {first.approval_id}"):
        _approve(store, wr)


def test_the_database_itself_refuses_approving_anything_else(store):
    wr = _finished(store, "no_change")
    with pytest.raises(sqlite3.IntegrityError, match="not an approvable delivery"):
        with sqlite3.connect(store.path) as db:
            db.execute(
                "INSERT INTO delivery_approvals (work_request_id, commit_sha, branch, base_sha, project_id,"
                " repository, evidence, approved_by, approved_at, status)"
                " VALUES (?, ?, 'b', ?, 'DEMO', 'r', '{}', 'x', 'now', 'approved')", (wr, SHA_A, BASE))


@pytest.mark.parametrize("column", ["commit_sha", "branch", "base_sha", "repository", "evidence", "approved_by",
                                    "work_request_id", "approved_at", "project_id"])
def test_what_was_approved_can_never_change(store, column):
    wr = _finished(store)
    approval = _approve(store, wr)
    with pytest.raises(sqlite3.IntegrityError, match="can never change what it approved"):
        with sqlite3.connect(store.path) as db:
            db.execute(f"UPDATE delivery_approvals SET {column} = ? WHERE id = 1",
                       (SHA_B if column.endswith("sha") else "changed",))
    assert store.get_approval(approval.approval_id) == approval


def test_approvals_are_never_deleted(store):
    _approve(store, _finished(store))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM delivery_approvals")


def test_the_delivery_lifecycle(store):
    approval = _approve(store, _finished(store))

    pushed = store.mark_pushed(approval.approval_id)
    assert pushed.status == "pushed" and pushed.pushed_at is not None
    done = store.mark_pr_created(approval.approval_id, pr_number=7, pr_url="https://github.com/o/r/pull/7")
    assert (done.status, done.pr_number, done.pr_url) == ("pr_created", 7, "https://github.com/o/r/pull/7")


@pytest.mark.parametrize(("from_status", "to"), [("approved", "pr_created"), ("pr_created", "pushed"),
                                                  ("pr_created", "approved"), ("invalid", "approved")])
def test_the_database_refuses_skipping_or_reversing_delivery_steps(store, from_status, to):
    approval = _approve(store, _finished(store))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE delivery_approvals SET status = 'pushed' WHERE id = 1") if from_status != "approved" else None
        if from_status == "pr_created":
            db.execute("UPDATE delivery_approvals SET status = 'pr_created' WHERE id = 1")
        if from_status == "invalid":
            db.execute("UPDATE delivery_approvals SET status = 'invalid' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="invalid approval transition"):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE delivery_approvals SET status = ? WHERE id = 1", (to,))
    assert approval.approval_id


def test_a_delivery_failure_changes_neither_the_approval_nor_the_work_request(store):
    wr = _finished(store)
    approval = _approve(store, wr)
    before = store.get(wr)

    failed = store.record_delivery_error(approval.approval_id, "GitHub said 401 Bad credentials")

    assert (failed.status, failed.last_error) == ("approved", "GitHub said 401 Bad credentials")
    assert store.get(wr) == before  # still completed, changed: a delivery failure isn't failed engineering


def test_an_invalid_approval_is_dead_and_a_new_one_can_be_made(store):
    wr = _finished(store)
    first = _approve(store, wr)

    dead = store.invalidate_approval(first.approval_id, "HEAD moved to another commit")

    assert (dead.status, dead.reason) == ("invalid", "HEAD moved to another commit")
    assert store.live_approval(wr) is None
    with pytest.raises(InvalidTransition):
        store.mark_pushed(first.approval_id)  # never delivered
    second = _approve(store, wr)  # needs a fresh, explicit approval
    assert second.approval_id == "APR-000002" and second.status == "approved"


def test_a_rejection_is_recorded_and_blocks_nothing_but_that_commit(store):
    wr = _finished(store)
    rejected = store.reject_delivery(wr, commit_sha=SHA_A, rejected_by="cli:tom@host", reason="wrong approach")
    assert (rejected.status, rejected.reason) == ("rejected", "wrong approach")
    assert store.live_approval(wr) is None
    with pytest.raises(InvalidTransition):
        store.mark_pushed(rejected.approval_id)


def test_a_v4_database_gains_the_approvals_table(tmp_path):
    from test_work_request_lifecycle import _legacy_request

    store = _legacy_request(tmp_path)
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "delivery_approvals" in tables
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
