"""Cycle Runner's read-only view of the team's work in Linear.

These are the tools the model sees. Their names and docstrings talk about
cycles and issues, never about how the data is fetched. Everything Linear-API
shaped (queries, auth, errors) stays in linear_client.

The tools are async: ADK awaits them, so a slow network call doesn't block the
event loop the Telegram bot runs on. (ADK calls plain sync tools directly on
the loop.)

Answers are kept small on purpose. The local model's context is a few
thousand tokens, and a real cycle has 50+ issues, so finished issues are
counted but not listed, and descriptions are trimmed.
"""

import functools
import os
import re

from cycle_runner.linear_client import LinearClient, LinearError

MAX_PAGES = 6  # 50 issues per page
DESCRIPTION_LIMIT = 1500
CLOSED_STATE_TYPES = {"completed", "canceled"}
ISSUE_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

CYCLE_ISSUES = """
query ($team: String!, $after: String) {
  teams(first: 1, filter: {key: {eq: $team}}) {
    nodes {
      activeCycle {
        number
        name
        startsAt
        endsAt
        progress
        issues(first: 50, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes { identifier title estimate state { name type } }
        }
      }
    }
  }
}
"""

ISSUE = """
query ($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    estimate
    priorityLabel
    state { name type }
    assignee { name }
    labels { nodes { name } }
    cycle { number }
  }
}
"""


@functools.cache
def _client() -> LinearClient:
    return LinearClient.from_env()


def _team_key() -> str:
    return os.environ.get("LINEAR_TEAM_KEY", "SB")


async def get_cycle_status() -> dict:
    """Get the team's current cycle: its dates and progress, and the work still open in it.

    Use this for any question about the current cycle: which cycle it is, when
    it ends, how it's going, what the team is working on, or what's left.

    Returns:
        dict: "cycle" (number, name, start_date, end_date as YYYY-MM-DD,
        progress_percent), "counts" (number of issues per status, all issues),
        "open_issues" (every issue not yet done or canceled, each with id,
        title, status and estimate) and "in_progress" (ids of issues being
        worked on now). Or an "error" key if the cycle can't be read.
    """
    try:
        cycle, issues = await _active_cycle_with_issues()
    except LinearError as exc:
        return {"error": f"Cycle state is unavailable: {exc}"}
    if cycle is None:
        return {"error": "The team has no active cycle."}

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["state"]["name"]] = counts.get(issue["state"]["name"], 0) + 1
    return {
        "cycle": {
            "number": int(cycle["number"]),
            "name": cycle["name"] or f"Cycle {int(cycle['number'])}",
            "start_date": cycle["startsAt"][:10],
            "end_date": cycle["endsAt"][:10],
            "progress_percent": round(cycle["progress"] * 100),
        },
        "counts": counts,
        "open_issues": [
            {
                "id": issue["identifier"],
                "title": issue["title"],
                "status": issue["state"]["name"],
                "estimate": issue["estimate"],
            }
            for issue in issues
            if issue["state"]["type"] not in CLOSED_STATE_TYPES
        ],
        "in_progress": [
            issue["identifier"] for issue in issues if issue["state"]["type"] == "started"
        ],
    }


async def get_issue(issue_id: str) -> dict:
    """Get one issue in detail, including its description.

    Use this when the user asks about a specific issue by its id, such as
    SB-123, or wants more detail than the cycle overview gives.

    Args:
        issue_id: The issue's id, like "SB-123".

    Returns:
        dict: id, title, status, estimate, priority, assignee, labels, cycle
        (number) and description (possibly shortened, see
        "description_truncated"). Or an "error" key if the issue can't be read.
    """
    issue_id = issue_id.strip().upper()
    if not ISSUE_ID.match(issue_id):
        return {"error": f"{issue_id!r} is not an issue id like SB-123."}
    try:
        data = await _client().query(ISSUE, {"id": issue_id})
    except LinearError as exc:
        return {"error": f"Could not read {issue_id}: {exc}"}

    issue = data["issue"]
    description = issue["description"] or ""
    return {
        "id": issue["identifier"],
        "title": issue["title"],
        "status": issue["state"]["name"],
        "estimate": issue["estimate"],
        "priority": issue["priorityLabel"],
        "assignee": (issue["assignee"] or {}).get("name"),
        "labels": [label["name"] for label in issue["labels"]["nodes"]],
        "cycle": int(issue["cycle"]["number"]) if issue["cycle"] else None,
        "description": description[:DESCRIPTION_LIMIT],
        "description_truncated": len(description) > DESCRIPTION_LIMIT,
    }


async def _active_cycle_with_issues() -> tuple[dict | None, list[dict]]:
    """The team's active cycle and all of its issues, following pagination."""
    cycle, issues, after = None, [], None
    for _ in range(MAX_PAGES):
        data = await _client().query(CYCLE_ISSUES, {"team": _team_key(), "after": after})
        teams = data["teams"]["nodes"]
        if not teams or teams[0]["activeCycle"] is None:
            return None, []
        cycle = teams[0]["activeCycle"]
        page = cycle["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return cycle, issues
