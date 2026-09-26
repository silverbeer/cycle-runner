import asyncio
import inspect
import os

import pytest

import conftest
from conftest import FAKE_ISSUE, FakeLinear, fake_issue
from cycle_runner import linear_tools
from cycle_runner.linear_client import LinearError
from cycle_runner.linear_tools import DESCRIPTION_LIMIT, get_cycle_status, get_issue

# --- get_cycle_status --------------------------------------------------------


def test_cycle_status_summarises_the_active_cycle(fake_linear):
    status = asyncio.run(get_cycle_status())

    assert status["cycle"] == {
        "number": 42,
        "name": "Cycle 42",
        "goal": None,
        "start_date": "2030-01-06",
        "end_date": "2030-01-13",
        "today": "2030-01-10",
        "days_remaining": 3,
        "progress_percent": 50,
    }
    assert status["counts"] == {"Done": 1, "In Progress": 1, "Todo": 1, "Canceled": 1}
    assert status["in_progress"] == ["TEST-2"]


def test_cycle_status_lists_only_open_issues(fake_linear):
    open_issues = asyncio.run(get_cycle_status())["open_issues"]

    assert open_issues == [
        {
            "id": "TEST-2",
            "title": "Fix the flaky login",
            "status": "In Progress",
            "estimate": 2,
            "priority": "High",
            "labels": [],
            "age_days": 9,
            "blocked_by": [],
        },
        {
            "id": "TEST-3",
            "title": "Write the release notes",
            "status": "Todo",
            "estimate": 1,
            "priority": "Medium",
            "labels": [],
            "age_days": 9,
            "blocked_by": [],
        },
    ]


def test_cycle_status_follows_pagination(monkeypatch):
    issues = [fake_issue(f"TEST-{n}", f"Issue {n}", "Todo", "unstarted") for n in range(1, 8)]
    fake = FakeLinear(issues=issues, page_size=3)
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)

    status = asyncio.run(get_cycle_status())

    assert status["counts"] == {"Todo": 7}
    assert len(fake.calls) == 3
    assert [call["after"] for call in fake.calls] == [None, "3", "6"]


def _status_with(monkeypatch, issues):
    fake = FakeLinear(issues=issues)
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    return asyncio.run(get_cycle_status()), fake


def _open(status, issue_id):
    return next(i for i in status["open_issues"] if i["id"] == issue_id)


def test_cycle_goal_is_reported_when_linear_has_one(monkeypatch):
    fake = FakeLinear(cycle={**conftest.FAKE_CYCLE, "description": "Ship v2"})
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    assert asyncio.run(get_cycle_status())["cycle"]["goal"] == "Ship v2"


def test_only_open_blockers_count_as_blocking(monkeypatch):
    status, _ = _status_with(monkeypatch, [
        fake_issue("TEST-1", "Blocked", "Todo", "unstarted",
                   blocked_by=[("TEST-8", "unstarted"), ("TEST-9", "completed")]),
        fake_issue("TEST-2", "Was blocked", "Todo", "unstarted",
                   blocked_by=[("TEST-9", "completed"), ("TEST-7", "canceled")]),
    ])

    assert _open(status, "TEST-1")["blocked_by"] == ["TEST-8"]
    assert _open(status, "TEST-2")["blocked_by"] == []


def test_non_blocking_relations_are_ignored(monkeypatch):
    issue = fake_issue("TEST-1", "Related only", "Todo", "unstarted")
    issue["inverseRelations"]["nodes"] = [
        {"type": "related", "issue": {"identifier": "TEST-5", "state": {"type": "unstarted"}}},
        {"type": "duplicate", "issue": {"identifier": "TEST-6", "state": {"type": "started"}}},
    ]
    status, _ = _status_with(monkeypatch, [issue])

    assert _open(status, "TEST-1")["blocked_by"] == []


def test_in_progress_work_is_listed_with_its_evidence(monkeypatch):
    status, _ = _status_with(monkeypatch, [
        fake_issue("TEST-1", "Half done", "In Progress", "started", priority="Low",
                   blocked_by=[("TEST-2", "unstarted")]),
        fake_issue("TEST-2", "Prerequisite", "Todo", "unstarted", priority="Urgent"),
    ])

    assert status["in_progress"] == ["TEST-1"]
    # In progress but blocked: both facts are reported, neither is hidden.
    assert _open(status, "TEST-1")["blocked_by"] == ["TEST-2"]
    assert _open(status, "TEST-2")["priority"] == "Urgent"


def test_priority_labels_and_age_are_reported_as_facts(monkeypatch):
    status, _ = _status_with(monkeypatch, [
        fake_issue("TEST-1", "Old bug", "Todo", "unstarted", priority="Urgent",
                   created="2029-12-01", labels=["bug", "backend"]),
    ])

    issue = _open(status, "TEST-1")
    assert (issue["priority"], issue["labels"], issue["age_days"]) == ("Urgent", ["bug", "backend"], 40)


def test_cycle_with_nothing_actionable_reports_that_plainly(monkeypatch):
    # Every open issue is blocked by another open issue: the facts must show it.
    status, _ = _status_with(monkeypatch, [
        fake_issue("TEST-1", "A", "Todo", "unstarted", blocked_by=[("TEST-2", "unstarted")]),
        fake_issue("TEST-2", "B", "Todo", "unstarted", blocked_by=[("TEST-1", "unstarted")]),
        fake_issue("TEST-3", "C", "Done", "completed"),
    ])

    assert status["in_progress"] == []
    assert all(issue["blocked_by"] for issue in status["open_issues"])


def test_cycle_with_no_open_work_has_empty_lists(monkeypatch):
    status, _ = _status_with(monkeypatch, [fake_issue("TEST-1", "All done", "Done", "completed")])

    assert status["open_issues"] == [] and status["in_progress"] == []
    assert status["counts"] == {"Done": 1}


def test_tools_only_ever_send_read_queries(fake_linear):
    asyncio.run(get_cycle_status())
    asyncio.run(get_issue("TEST-2"))

    assert fake_linear.documents
    for document in fake_linear.documents:
        assert document.lstrip().startswith("query")
        assert "mutation" not in document and "subscription" not in document


def test_cycle_status_asks_for_the_configured_team(fake_linear, monkeypatch):
    monkeypatch.setenv("LINEAR_TEAM_KEY", "XY")
    asyncio.run(get_cycle_status())
    assert fake_linear.calls[0]["team"] == "XY"


def test_no_active_cycle_is_reported_not_invented(monkeypatch):
    monkeypatch.setattr(linear_tools, "_client", lambda: FakeLinear(cycle=None))
    assert "error" in asyncio.run(get_cycle_status())


def test_linear_failure_becomes_an_error_result(monkeypatch):
    class Down:
        async def query(self, *args):
            raise LinearError("could not reach Linear (ConnectError)")

    monkeypatch.setattr(linear_tools, "_client", lambda: Down())

    assert asyncio.run(get_cycle_status()) == {
        "error": "Cycle state is unavailable: could not reach Linear (ConnectError)"
    }


def test_missing_credentials_become_an_error_result():
    # The autouse fixture removed LINEAR_CLIENT_ID/SECRET for this test.
    assert "must be set" in asyncio.run(get_cycle_status())["error"]


# --- get_issue ---------------------------------------------------------------


def test_get_issue_returns_the_issue_in_detail(fake_linear):
    issue = asyncio.run(get_issue("test-2 "))

    assert fake_linear.calls == [{"id": "TEST-2"}]
    assert issue == {
        "id": "TEST-2",
        "title": "Fix the flaky login",
        "status": "In Progress",
        "estimate": 2,
        "priority": "High",
        "assignee": "Pat Example",
        "labels": ["bug"],
        "cycle": 42,
        "blocked_by": [],
        "description": "Login fails about 1 in 20 runs on CI.",
        "description_truncated": False,
    }


def test_get_issue_trims_long_descriptions(monkeypatch):
    long_issue = {**FAKE_ISSUE, "description": "x" * (DESCRIPTION_LIMIT + 10), "assignee": None, "cycle": None}
    monkeypatch.setattr(linear_tools, "_client", lambda: FakeLinear(issue=long_issue))

    issue = asyncio.run(get_issue("TEST-2"))

    assert len(issue["description"]) == DESCRIPTION_LIMIT
    assert issue["description_truncated"] is True
    assert issue["assignee"] is None and issue["cycle"] is None


@pytest.mark.parametrize("bad", ["hello", "SB 12", "12", "SB-", "SB-12; drop"])
def test_get_issue_rejects_malformed_ids_without_asking_linear(fake_linear, bad):
    assert "error" in asyncio.run(get_issue(bad))
    assert fake_linear.calls == []


def test_get_issue_reports_linear_errors(monkeypatch):
    class NotFound:
        async def query(self, *args):
            raise LinearError("Could not find referenced Issue.")

    monkeypatch.setattr(linear_tools, "_client", lambda: NotFound())

    assert asyncio.run(get_issue("SB-999999")) == {
        "error": "Could not read SB-999999: Could not find referenced Issue."
    }


# --- what the model sees -----------------------------------------------------


@pytest.mark.parametrize("tool", [get_cycle_status, get_issue])
def test_tools_are_async_so_they_do_not_block_the_bot(tool):
    assert inspect.iscoroutinefunction(tool)


@pytest.mark.parametrize("tool", [get_cycle_status, get_issue])
def test_tool_descriptions_do_not_reveal_how_data_is_fetched(tool):
    text = (tool.__name__ + inspect.getdoc(tool)).lower()
    for word in ["graphql", "http", "api", "token", "oauth", "url", "query", "sql", "database"]:
        assert word not in text, word


# --- real Linear (network) ---------------------------------------------------


needs_linear = pytest.mark.skipif(
    not os.environ.get("LINEAR_CLIENT_ID", "").strip() or os.environ["LINEAR_CLIENT_ID"].startswith("op://"),
    reason="run under `op run --env-file .env` with Linear credentials",
)


@pytest.mark.linear
@needs_linear
def test_real_cycle_status_and_issue():
    status = asyncio.run(get_cycle_status())
    assert "error" not in status, status
    assert status["cycle"]["number"] > 0
    assert sum(status["counts"].values()) >= len(status["open_issues"])

    some_issue = (status["in_progress"] or [i["id"] for i in status["open_issues"]])[0]
    issue = asyncio.run(get_issue(some_issue))
    assert issue["id"] == some_issue
    assert issue["title"]


def test_get_issue_reports_open_blockers(monkeypatch):
    blocked = {
        **FAKE_ISSUE,
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"identifier": "TEST-8", "state": {"type": "started"}}},
                {"type": "blocks", "issue": {"identifier": "TEST-9", "state": {"type": "completed"}}},
            ]
        },
    }
    monkeypatch.setattr(linear_tools, "_client", lambda: FakeLinear(issue=blocked))

    assert asyncio.run(get_issue("TEST-2"))["blocked_by"] == ["TEST-8"]
