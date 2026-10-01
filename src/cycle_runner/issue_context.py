"""The issue behind a work request, read from Linear, as an ExecutionTask.

    WorkRequest (issue SB-640, project MT) ─► LinearIssueSource ─► Linear (read-only)
        ─► ExecutionTask(title, full description, rationale, ...) ─► any Executor

The runner uses this so the coding agent works from the whole issue, not
just its title, while the executor stays Linear-free. It only reads, through
the existing read-only client (token scope "read").

A task is only built when the issue matches the approval: the same
identifier, a project that agrees with the one recorded at approval, and a
description to work from. Otherwise the request fails to start rather than
running a production task on a guess.
"""

import asyncio
from dataclasses import dataclass

from cycle_runner.executor import ExecutionTask, TaskContextError
from cycle_runner.linear_client import LinearClient, LinearError
from cycle_runner.work_requests import WorkRequest

# The same label group the recommendation uses to identify the project.
PROJECT_LABEL_GROUP = "repo"
DESCRIPTION_LIMIT = 20_000  # characters; a guard against pasting huge documents into the prompt

ISSUE = """
query ($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    labels { nodes { name parent { name } } }
    cycle { number }
  }
}
"""


class LinearIssueSource:
    def __init__(self, client: LinearClient):
        self.client = client

    @classmethod
    def from_env(cls) -> "LinearIssueSource":
        try:
            return cls(LinearClient.from_env())
        except LinearError as exc:
            raise TaskContextError(str(exc)) from None

    def describe(self, issue_id: str) -> "IssueSummary":
        return describe_issue(self.client, issue_id)

    def task_for(self, request: WorkRequest) -> ExecutionTask:
        try:
            issue = asyncio.run(self.client.query(ISSUE, {"id": request.issue_id}))["issue"]
        except LinearError as exc:
            raise TaskContextError(f"couldn't read {request.issue_id} from Linear: {exc}") from None
        if not issue or issue.get("identifier") != request.issue_id:
            raise TaskContextError(f"Linear returned a different issue for {request.issue_id}")
        project = _project(issue)
        if request.project_id and project != request.project_id:
            raise TaskContextError(
                f"{request.issue_id} is labelled {project!r} in Linear, but was approved for {request.project_id!r}"
            )
        description = (issue.get("description") or "").strip()
        if not description:
            raise TaskContextError(f"{request.issue_id} has no description to work from")
        return ExecutionTask(
            work_request_id=request.work_request_id,
            issue_id=request.issue_id,
            project_id=request.project_id,
            title=issue["title"],
            description=description[:DESCRIPTION_LIMIT],
            rationale=request.rationale,
        )


@dataclass(frozen=True)
class IssueSummary:
    """What a human needs to request work on an issue: read from Linear, not typed in."""

    issue_id: str
    title: str
    project_id: str | None
    cycle_number: int | None


def describe_issue(client: LinearClient, issue_id: str) -> IssueSummary:
    """The issue's title, project (its repo label) and cycle, read-only. TaskContextError if unreadable."""
    try:
        issue = asyncio.run(client.query(ISSUE, {"id": issue_id}))["issue"]
    except LinearError as exc:
        raise TaskContextError(f"couldn't read {issue_id} from Linear: {exc}") from None
    if not issue or issue.get("identifier") != issue_id:
        raise TaskContextError(f"Linear has no issue {issue_id}")
    return IssueSummary(issue_id=issue_id, title=issue["title"], project_id=_project(issue),
                        cycle_number=(issue.get("cycle") or {}).get("number"))


def _project(issue: dict) -> str | None:
    projects = [
        label["name"] for label in issue["labels"]["nodes"]
        if (label.get("parent") or {}).get("name") == PROJECT_LABEL_GROUP
    ]
    return projects[0] if len(projects) == 1 else None
