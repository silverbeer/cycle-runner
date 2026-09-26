import inspect
from datetime import date
from pathlib import Path

import pytest

from cycle_runner import tools
from cycle_runner.store import Cycle, CycleStore, Issue
from cycle_runner.tools import get_current_cycle, get_cycle_status


def test_get_current_cycle_reads_the_active_cycle():
    assert get_current_cycle() == {
        "id": "DEMO-CYCLE-2026-W39",
        "name": "Week 39",
        "goal": "Build Cycle Runner",
        "status": "active",
        "start_date": "2026-09-21",
        "end_date": "2026-09-27",
    }


def test_get_cycle_status_reads_cycle_issues_and_progress():
    status = get_cycle_status()

    assert status["cycle"] == get_current_cycle()
    assert [i["id"] for i in status["issues"]] == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"]
    assert status["counts"] == {"done": 3, "in_progress": 1}
    assert status["in_progress"] == ["DEMO-4"]


@pytest.fixture
def other_db(tmp_path, monkeypatch):
    path = tmp_path / "other.db"
    monkeypatch.setenv("CYCLE_RUNNER_DB", str(path))
    return CycleStore(path)


def test_tools_return_whatever_the_store_holds(other_db):
    # Proves nothing is hard-coded: different rows in, different answer out.
    other_db.add_cycle(Cycle("X", "Sprint Zebra", "Tame it", "active", date(2030, 1, 1), date(2030, 1, 7)))
    other_db.add_issue(Issue("X-1", "Find zebra", "blocked", "X"))

    assert get_current_cycle()["name"] == "Sprint Zebra"
    status = get_cycle_status()
    assert status["issues"] == [{"id": "X-1", "title": "Find zebra", "status": "blocked"}]
    assert status["counts"] == {"blocked": 1}
    assert status["in_progress"] == []


def test_status_reflects_changes_immediately(other_db):
    other_db.add_cycle(Cycle("X", "W", "G", "active", date(2030, 1, 1), date(2030, 1, 7)))
    assert get_cycle_status()["issues"] == []

    other_db.add_issue(Issue("X-1", "New", "todo", "X"))

    assert len(get_cycle_status()["issues"]) == 1


def test_tools_report_unavailable_state_instead_of_inventing_it(other_db):
    assert "error" in get_current_cycle()
    assert "error" in get_cycle_status()


def test_no_cycle_data_is_hard_coded_in_the_tools():
    source = Path(tools.__file__).read_text()
    for literal in ["Week 39", "DEMO-", "Build Cycle Runner", "2026-"]:
        assert literal not in source


def test_tools_do_not_reveal_storage_to_the_model():
    # The model sees names and docstrings only; keep both about the domain.
    for tool in (get_current_cycle, get_cycle_status):
        text = (tool.__name__ + inspect.getdoc(tool)).lower()
        for word in ["sql", "database", "query", "table"]:
            assert word not in text, (tool.__name__, word)
