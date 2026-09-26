import sqlite3
from datetime import date

import pytest

from cycle_runner.store import Cycle, CycleStore, Issue

CYCLE = Cycle("C1", "Week 1", "Ship it", "active", date(2026, 1, 5), date(2026, 1, 11))


@pytest.fixture
def store(tmp_path):
    return CycleStore(tmp_path / "store.db")


def test_init_creates_the_file_parent_dirs_and_tables(tmp_path):
    path = tmp_path / "nested" / "dir" / "store.db"

    CycleStore(path)

    assert path.exists()
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"cycles", "issues"}


def test_new_store_is_empty(store):
    assert store.get_active_cycle() is None


def test_add_and_read_a_cycle(store):
    store.add_cycle(CYCLE)

    assert store.get_cycle("C1") == CYCLE
    assert store.get_active_cycle() == CYCLE
    assert store.get_cycle("missing") is None


def test_add_and_read_issues_in_insertion_order(store):
    store.add_cycle(CYCLE)
    second = Issue("I2", "Second", "todo", "C1")
    first = Issue("I1", "First", "blocked", "C1")
    store.add_issue(second)
    store.add_issue(first)

    assert store.get_issue("I1") == first
    assert store.list_issues("C1") == [second, first]
    assert store.get_issue("missing") is None


def test_completed_cycles_are_not_current(store):
    store.add_cycle(Cycle("OLD", "Week 0", "Done", "completed", date(2025, 12, 29), date(2026, 1, 4)))

    assert store.get_active_cycle() is None


def test_data_survives_reopening_the_database(tmp_path):
    path = tmp_path / "store.db"
    CycleStore(path).add_cycle(CYCLE)

    assert CycleStore(path).get_active_cycle() == CYCLE


def test_only_one_cycle_can_be_active(store):
    store.add_cycle(CYCLE)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_cycle(Cycle("C2", "Week 2", "More", "active", date(2026, 1, 12), date(2026, 1, 18)))


@pytest.mark.parametrize(
    "bad",
    [
        lambda: Cycle("C9", "W", "G", "paused", date(2026, 1, 1), date(2026, 1, 7)),
        lambda: Issue("I9", "T", "wip", "C1"),
    ],
)
def test_unknown_statuses_are_rejected(store, bad):
    store.add_cycle(CYCLE)
    record = bad()
    add = store.add_cycle if isinstance(record, Cycle) else store.add_issue
    with pytest.raises(sqlite3.IntegrityError):
        add(record)


def test_issue_must_belong_to_an_existing_cycle(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_issue(Issue("I1", "Orphan", "todo", "no-such-cycle"))
