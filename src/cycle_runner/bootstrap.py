"""Seed a development database with one demo cycle: `uv run python -m cycle_runner.bootstrap`.

Everything created here is demo data. The DEMO- ids are deliberately not real
Linear ids. Running it again is safe: records that already exist are left
alone, including any status changed since the first run.
"""

from datetime import date

from cycle_runner.store import Cycle, CycleStore, Issue, db_path_from_env

DEMO_CYCLE = Cycle(
    id="DEMO-CYCLE-2026-W39",
    name="Week 39",
    goal="Build Cycle Runner",
    status="active",
    start_date=date(2026, 9, 21),
    end_date=date(2026, 9, 27),
)

DEMO_ISSUES = [
    Issue("DEMO-1", "ADK foundation", "done", DEMO_CYCLE.id),
    Issue("DEMO-2", "ADK tools", "done", DEMO_CYCLE.id),
    Issue("DEMO-3", "Telegram interface", "done", DEMO_CYCLE.id),
    Issue("DEMO-4", "Persistent cycle state", "in_progress", DEMO_CYCLE.id),
]


def seed_demo_data(store: CycleStore) -> int:
    """Create the demo cycle and issues that don't exist yet. Returns how many were created."""
    created = 0
    if store.get_cycle(DEMO_CYCLE.id) is None:
        store.add_cycle(DEMO_CYCLE)
        created += 1
    for issue in DEMO_ISSUES:
        if store.get_issue(issue.id) is None:
            store.add_issue(issue)
            created += 1
    return created


def main() -> None:
    path = db_path_from_env()
    created = seed_demo_data(CycleStore(path))
    print(f"{path}: created {created} demo record(s)")


if __name__ == "__main__":
    main()
