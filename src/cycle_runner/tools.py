"""Domain tools: what the agent is allowed to know about the cycle.

The model sees only these functions' names and docstrings. They talk about
cycles and issues, never about storage, and they return plain dicts. There is
deliberately no tool that runs queries: the agent gets domain answers, not
database access.

Each call opens the store fresh, so every answer reflects the state at that
moment, and a restart loses nothing.
"""

from cycle_runner.store import Cycle, CycleStore, db_path_from_env

NO_ACTIVE_CYCLE = {"error": "There is no active cycle. Cycle state is unavailable."}


def get_cycle_status() -> dict:
    """Get the current cycle: its details plus every issue and its status.

    Use this for any question about the current cycle: its name, goal or dates,
    how it's going, what the team is working on, what's done or blocked, or
    what to focus on.

    Returns:
        dict: "cycle" (id, name, goal, status, start_date and end_date as
        YYYY-MM-DD), "issues" (each with id, title and status: todo,
        in_progress, done or blocked), "counts" (number of issues per status)
        and "in_progress" (ids of the issues being worked on now). Or an
        "error" key if there is no active cycle.
    """
    store = CycleStore(db_path_from_env())
    cycle = store.get_active_cycle()
    if cycle is None:
        return NO_ACTIVE_CYCLE

    issues = store.list_issues(cycle.id)
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.status] = counts.get(issue.status, 0) + 1
    return {
        "cycle": _cycle_dict(cycle),
        "issues": [
            {"id": issue.id, "title": issue.title, "status": issue.status}
            for issue in issues
        ],
        "counts": counts,
        "in_progress": [issue.id for issue in issues if issue.status == "in_progress"],
    }


def _cycle_dict(cycle: Cycle) -> dict:
    return {
        "id": cycle.id,
        "name": cycle.name,
        "goal": cycle.goal,
        "status": cycle.status,
        "start_date": cycle.start_date.isoformat(),
        "end_date": cycle.end_date.isoformat(),
    }
