import sqlite3

from cycle_runner import bootstrap
from cycle_runner.bootstrap import DEMO_CYCLE, DEMO_ISSUES, seed_demo_data
from cycle_runner.store import CycleStore, Issue


def _row_counts(path):
    with sqlite3.connect(path) as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("cycles", "issues"))


def test_seed_creates_one_active_cycle_and_its_issues(tmp_path):
    store = CycleStore(tmp_path / "db.sqlite")

    created = seed_demo_data(store)

    assert created == 1 + len(DEMO_ISSUES)
    assert store.get_active_cycle() == DEMO_CYCLE
    assert store.list_issues(DEMO_CYCLE.id) == DEMO_ISSUES


def test_seed_is_idempotent(tmp_path):
    path = tmp_path / "db.sqlite"
    seed_demo_data(CycleStore(path))
    counts_after_first = _row_counts(path)

    assert seed_demo_data(CycleStore(path)) == 0
    assert _row_counts(path) == counts_after_first == (1, 4)


def test_seed_does_not_overwrite_existing_records(tmp_path):
    store = CycleStore(tmp_path / "db.sqlite")
    store.add_cycle(DEMO_CYCLE)
    store.add_issue(Issue("DEMO-4", "Persistent cycle state", "blocked", DEMO_CYCLE.id))

    seed_demo_data(store)

    assert store.get_issue("DEMO-4").status == "blocked"


def test_demo_records_are_labelled_as_demo():
    assert DEMO_CYCLE.id.startswith("DEMO-")
    assert all(issue.id.startswith("DEMO-") for issue in DEMO_ISSUES)


def test_main_seeds_the_database_named_by_env(tmp_path, monkeypatch, capsys):
    path = tmp_path / "from-env.db"
    monkeypatch.setenv("CYCLE_RUNNER_DB", str(path))

    bootstrap.main()

    assert CycleStore(path).get_active_cycle() == DEMO_CYCLE
    assert "created 5" in capsys.readouterr().out
