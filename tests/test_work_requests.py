"""The work-request store on its own: no ADK, no model, no Telegram, no Linear."""

import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, datetime

import pytest

from cycle_runner.work_requests import WorkRequest, WorkRequestStore

APPROVED_AT = datetime(2030, 1, 10, 12, 30, tzinfo=UTC)


def _approve(store, recommendation_id="rec-1", issue_id="SB-1234"):
    return store.create_for_approval(
        recommendation_id=recommendation_id,
        issue_id=issue_id,
        approved_by="telegram:111",
        approved_at=APPROVED_AT,
        cycle_number=9,
        title_at_approval="Fix the flaky login",
        rationale="Already in progress.",
    )


@pytest.fixture
def store(tmp_path):
    return WorkRequestStore(tmp_path / "wr.db")


def test_approval_creates_one_pending_work_request(store):
    request, created = _approve(store)

    assert created is True
    assert request == WorkRequest(
        work_request_id="WR-000001",
        recommendation_id="rec-1",
        issue_id="SB-1234",
        status="pending",
        approved_by="telegram:111",
        approved_at=APPROVED_AT,
        cycle_number=9,
        title_at_approval="Fix the flaky login",
        rationale="Already in progress.",
    )
    assert store.list_all() == [request]


def test_the_same_approval_twice_creates_nothing_new(store):
    first, _ = _approve(store)

    second, created = _approve(store)

    assert created is False
    assert second == first
    assert len(store.list_all()) == 1


def test_idempotency_is_enforced_by_the_database_itself(store):
    _approve(store)
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "INSERT INTO work_requests (recommendation_id, issue_id, status, approved_by, approved_at,"
            " cycle_number, title_at_approval, rationale)"
            " VALUES ('rec-1', 'SB-1', 'pending', 'x', 'x', 1, 'x', 'x')"
        )


def test_a_new_recommendation_of_the_same_issue_is_a_new_request(store):
    # One Linear issue may produce more than one work request over its lifetime.
    first, _ = _approve(store, recommendation_id="rec-1")
    second, created = _approve(store, recommendation_id="rec-2")

    assert created is True
    assert (first.work_request_id, second.work_request_id) == ("WR-000001", "WR-000002")
    assert first.issue_id == second.issue_id


@pytest.mark.parametrize(("status", "error"), [("running", "must be pending"), ("paused", "must be pending")])
def test_a_new_request_can_only_start_as_pending(store, status, error):
    # V0.9 allows more statuses, but a new row still has to start at the beginning.
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError, match=error):
        db.execute(
            "INSERT INTO work_requests (recommendation_id, issue_id, status, approved_by, approved_at,"
            f" cycle_number, title_at_approval, rationale) VALUES ('r', 'SB-1', '{status}', 'x', 'x', 1, 'x', 'x')"
        )


def test_ids_are_never_reused(store):
    _approve(store, recommendation_id="rec-1")
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM work_requests")

    request, _ = _approve(store, recommendation_id="rec-2")

    assert request.work_request_id == "WR-000002"


def test_requests_survive_reopening_the_store(tmp_path):
    path = tmp_path / "wr.db"
    created, _ = _approve(WorkRequestStore(path))

    assert WorkRequestStore(path).get("WR-000001") == created


@pytest.mark.parametrize("bad", ["", "SB-1234", "WR-1", "wr-000001", "WR-000002"])
def test_get_returns_none_for_unknown_or_malformed_ids(store, bad):
    _approve(store)
    assert store.get(bad) is None


def test_the_store_runs_without_adk_telegram_or_linear(tmp_path):
    # A separate process in which importing ADK, Telegram, httpx or the Linear
    # client fails: the store must not need any of them.
    script = textwrap.dedent(
        f"""
        import sys
        from datetime import UTC, datetime
        for name in ["google.adk", "google.genai", "telegram", "httpx", "litellm", "cycle_runner.linear_client"]:
            sys.modules[name] = None
        import importlib.util
        spec = importlib.util.spec_from_file_location("work_requests", {repr(str(_module_path()))})
        work_requests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(work_requests)
        store = work_requests.WorkRequestStore({repr(str(tmp_path / "isolated.db"))})
        request, created = store.create_for_approval(
            recommendation_id="r", issue_id="SB-1", approved_by="u",
            approved_at=datetime.now(UTC), cycle_number=1, title_at_approval="t", rationale="r")
        print(request.work_request_id, request.status, created)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["WR-000001", "pending", "True"]


def _module_path():
    import cycle_runner.work_requests

    return cycle_runner.work_requests.__file__
