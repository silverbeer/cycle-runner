"""Collect the Linear side of a Snapshot: the team, one cycle, open issues, epics and their children.

What is read, all scoped to one team:
- the selected cycle (the active one unless a number is given), with every
  issue in it, and the previous cycle's uncompleted-on-close list (carry-in);
- every open issue in the team (open = not completed, canceled or duplicate);
- every project that isn't completed or canceled, with all its issues;
- the parents and children of those issues, so epic counts are whole.

Linear caps a query's complexity at 10,000, so issues come 50 to a page, and
child lists 50 per issue. A child list that doesn't fit is recorded as
unknown (children=None) rather than as a short list; a pagination cap that is
reached is recorded in the source's notes. Nothing here guesses.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime

from cycle_runner.scrum.models import Cycle, Issue, Project, SourceStatus

log = logging.getLogger(__name__)

Query = Callable[[str, dict], Awaitable[dict]]

PAGE = 50
MAX_PAGES = 40  # 2,000 issues per list; reaching it is reported, not hidden
NUMBER_CHUNK = 50

ISSUE_FIELDS = """
  identifier title url priority priorityLabel estimate
  createdAt updatedAt startedAt completedAt canceledAt dueDate addedToCycleAt
  state { name type }
  assignee { displayName }
  labels(first: 20) { nodes { name } }
  cycle { number }
  project { id }
  parent { identifier }
  children(first: 50) { nodes { identifier } pageInfo { hasNextPage } }
  inverseRelations(first: 20) { nodes { type issue { identifier state { type } } } }
"""

TEAM = """
query ($team: String!) {
  teams(first: 1, filter: {key: {eq: $team}}) {
    nodes { key name timezone activeCycle { number } }
  }
}
"""

CYCLES = """
query ($team: String!, $numbers: [Float!]) {
  cycles(first: 2, filter: {team: {key: {eq: $team}}, number: {in: $numbers}}) {
    nodes {
      id number name startsAt endsAt
      uncompletedIssuesUponClose(first: 250) { nodes { identifier } pageInfo { hasNextPage } }
    }
  }
}
"""

ISSUES = (
    """
query ($filter: IssueFilter!, $after: String) {
  issues(first: %d, after: $after, filter: $filter) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
"""
    % (PAGE, ISSUE_FIELDS)
)

PROJECTS = """
query ($team: String!, $after: String) {
  projects(first: 50, after: $after, filter: {
    accessibleTeams: {some: {key: {eq: $team}}}, status: {type: {nin: ["completed", "canceled"]}}
  }) {
    nodes { id name url status { name type } lead { displayName } targetDate }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PROJECT_ISSUES = """
query ($project: String!, $after: String) {
  project(id: $project) {
    issues(first: 100, after: $after) { nodes { identifier } pageInfo { hasNextPage endCursor } }
  }
}
"""


class Collected:
    """What the Linear collection produced, with its own completeness notes."""

    def __init__(self) -> None:
        self.team_name: str | None = None
        self.timezone: str | None = None
        self.cycle: Cycle | None = None
        self.issues: dict[str, Issue] = {}
        self.projects: list[Project] = []
        self.notes: list[str] = []

    def status(self) -> SourceStatus:
        return SourceStatus(name="linear", ok=True, complete=not self.notes, notes=sorted(set(self.notes)))


async def collect(query: Query, *, team: str, cycle_number: int | None = None) -> Collected:
    """Read everything the report needs from Linear. Raises LinearError if Linear can't be read at all."""
    out = Collected()
    teams = (await query(TEAM, {"team": team}))["teams"]["nodes"]
    if not teams:
        raise LookupError(f"no Linear team with key {team!r}")
    out.team_name, out.timezone = teams[0]["name"], teams[0].get("timezone")
    number = cycle_number or ((teams[0].get("activeCycle") or {}).get("number"))

    if number is not None:
        out.cycle = await _cycle(query, out, team=team, number=int(number))
    else:
        out.notes.append("the team has no active cycle; cycle health is not reported")

    await _issues(query, out, {"team": {"key": {"eq": team}}, "state": {"type": {"nin": sorted(_CLOSED)}}},
                  what="open issues")
    out.projects = await _projects(query, out, team=team)
    await _fill_relatives(query, out, team=team)
    return out


_CLOSED = {"completed", "canceled", "duplicate"}


async def _cycle(query: Query, out: Collected, *, team: str, number: int) -> Cycle | None:
    nodes = (await query(CYCLES, {"team": team, "numbers": [number, number - 1]}))["cycles"]["nodes"]
    by_number = {int(node["number"]): node for node in nodes}
    node = by_number.get(number)
    if node is None:
        out.notes.append(f"cycle {number} was not found; cycle health is not reported")
        return None
    issues = await _issues(query, out, {"cycle": {"id": {"eq": node["id"]}}}, what=f"cycle {number} issues")

    carried_in, previous = None, by_number.get(number - 1)
    if previous is None:
        out.notes.append(f"cycle {number - 1} was not found; carry-in is unknown")
    elif previous["uncompletedIssuesUponClose"]["pageInfo"]["hasNextPage"]:
        out.notes.append(f"cycle {number - 1} carried over more than 250 issues; carry-in is unknown")
    else:
        carried_in = sorted(i["identifier"] for i in previous["uncompletedIssuesUponClose"]["nodes"])

    return Cycle(number=number, name=node.get("name"), starts_at=_dt(node["startsAt"]), ends_at=_dt(node["endsAt"]),
                 issues=sorted(issues, key=_issue_order), carried_in=carried_in,
                 previous_number=int(previous["number"]) if previous else None)


async def _issues(query: Query, out: Collected, filter_: dict, *, what: str) -> list[str]:
    """Page through an issue list, adding each issue to out.issues. Returns the identifiers seen."""
    seen: list[str] = []
    after = None
    for _ in range(MAX_PAGES):
        page = (await query(ISSUES, {"filter": filter_, "after": after}))["issues"]
        for node in page["nodes"]:
            issue = _issue(node, out)
            out.issues.setdefault(issue.identifier, issue)  # dedupe: the same issue appears in several lists
            seen.append(issue.identifier)
        if not page["pageInfo"]["hasNextPage"]:
            return seen
        after = page["pageInfo"]["endCursor"]
    out.notes.append(f"{what}: stopped after {MAX_PAGES * PAGE} issues; the list is incomplete")
    return seen


async def _projects(query: Query, out: Collected, *, team: str) -> list[Project]:
    nodes, after = [], None
    for _ in range(MAX_PAGES):
        page = (await query(PROJECTS, {"team": team, "after": after}))["projects"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    else:
        out.notes.append("projects: pagination cap reached; some epics are missing")

    projects = []
    for node in nodes:
        identifiers, complete, after = [], True, None
        for _ in range(MAX_PAGES):
            page = (await query(PROJECT_ISSUES, {"project": node["id"], "after": after}))["project"]["issues"]
            identifiers += [n["identifier"] for n in page["nodes"]]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        else:
            complete = False
            out.notes.append(f"project {node['name']!r}: too many issues to list; its counts are hidden")
        projects.append(Project(
            id=node["id"], name=node["name"], url=node["url"], status_name=node["status"]["name"],
            status_type=node["status"]["type"], lead=(node.get("lead") or {}).get("displayName"),
            target_date=_date(node.get("targetDate")),
            issues=sorted(set(identifiers), key=_issue_order) if complete else None,
        ))
    return sorted(projects, key=lambda p: (p.name.lower(), p.id))


async def _fill_relatives(query: Query, out: Collected, *, team: str) -> None:
    """Fetch the parents, children and project issues not already collected (mostly completed ones)."""
    wanted: set[str] = set()
    for issue in out.issues.values():
        wanted.update(issue.children or [])
        if issue.parent:
            wanted.add(issue.parent)
    for project in out.projects:
        wanted.update(project.issues or [])
    prefix = f"{team}-"
    missing = sorted((i for i in wanted - out.issues.keys() if i.startswith(prefix)), key=_issue_order)
    other = sorted(i for i in wanted - out.issues.keys() if not i.startswith(prefix))
    if other:
        out.notes.append(f"{len(other)} related issues belong to other teams and were not read: {', '.join(other[:5])}")
    numbers = [int(i.removeprefix(prefix)) for i in missing]
    for start in range(0, len(numbers), NUMBER_CHUNK):
        chunk = numbers[start:start + NUMBER_CHUNK]
        await _issues(query, out, {"team": {"key": {"eq": team}}, "number": {"in": chunk}}, what="related issues")
    still_missing = [i for i in missing if i not in out.issues]
    if still_missing:
        out.notes.append(f"{len(still_missing)} related issues could not be read (archived?): "
                         f"{', '.join(still_missing[:5])}")


def _issue(node: dict, out: Collected) -> Issue:
    children = node.get("children") or {"nodes": [], "pageInfo": {"hasNextPage": False}}
    if children["pageInfo"]["hasNextPage"]:
        out.notes.append(f"{node['identifier']} has more than 50 children; its child counts are hidden")
        child_ids = None
    else:
        child_ids = sorted((c["identifier"] for c in children["nodes"]), key=_issue_order)
    relations = (node.get("inverseRelations") or {}).get("nodes", [])
    blocked_by = sorted({
        r["issue"]["identifier"] for r in relations
        if r["type"] == "blocks" and r["issue"]["state"]["type"] not in _CLOSED
    }, key=_issue_order)
    return Issue(
        identifier=node["identifier"], title=node["title"], url=node["url"],
        state_name=node["state"]["name"], state_type=node["state"]["type"],
        priority=int(node.get("priority") or 0), priority_label=node.get("priorityLabel") or "No priority",
        estimate=node.get("estimate"), assignee=(node.get("assignee") or {}).get("displayName"),
        labels=sorted(label["name"] for label in (node.get("labels") or {}).get("nodes", [])),
        created_at=_dt(node["createdAt"]), updated_at=_dt(node["updatedAt"]),
        started_at=_dt(node.get("startedAt")), completed_at=_dt(node.get("completedAt")),
        canceled_at=_dt(node.get("canceledAt")), due_date=_date(node.get("dueDate")),
        added_to_cycle_at=_dt(node.get("addedToCycleAt")),
        cycle_number=int(node["cycle"]["number"]) if node.get("cycle") else None,
        project_id=(node.get("project") or {}).get("id"),
        parent=(node.get("parent") or {}).get("identifier"),
        children=child_ids, blocked_by=blocked_by,
    )


def _issue_order(identifier: str) -> tuple[str, int]:
    """SB-9 before SB-10: sort by team key, then number."""
    key, _, number = identifier.rpartition("-")
    return (key, int(number)) if number.isdigit() else (identifier, 0)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
