"""Builders for Scrum Master snapshots: small, explicit, and dated relative to one fixed NOW."""

from datetime import UTC, date, datetime, timedelta

from cycle_runner.scrum.config import Config, RepoConfig
from cycle_runner.scrum.models import (
    AssociatedPR,
    Cycle,
    Issue,
    LocalBranch,
    LocalRepo,
    Project,
    PullRequest,
    RemoteBranch,
    Repo,
    Scope,
    Snapshot,
    SourceStatus,
    Worktree,
)

# Thursday 2030-01-10, 15:00 UTC = 10:00 in New York.
NOW = datetime(2030, 1, 10, 15, 0, tzinfo=UTC)
CONFIG = Config(team="TEST", repos=[RepoConfig(github="acme/app", local="/nonexistent/app")])


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def day(days_ago: int) -> date:
    return (NOW - timedelta(days=days_ago)).date()


STATE_NAMES = {"backlog": "Backlog", "unstarted": "Todo", "started": "In Progress", "completed": "Done",
               "canceled": "Canceled", "duplicate": "Duplicate", "triage": "Triage"}


def issue(n: int, state: str = "unstarted", *, created: float = 1, updated: float | None = None,
          started: float | None = None, priority: int = 3, estimate: float | None = 1, assignee: str | None = "me",
          **fields) -> Issue:
    labels = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
    if state == "started" and started is None:
        started = created
    return Issue(
        identifier=f"TEST-{n}", title=fields.pop("title", f"Issue {n}"), url=f"https://linear.app/t/issue/TEST-{n}",
        state_name=fields.pop("state_name", STATE_NAMES[state]), state_type=state, priority=priority,
        priority_label=labels[priority], estimate=estimate, assignee=assignee,
        created_at=ago(created), updated_at=ago(created if updated is None else updated),
        started_at=ago(started) if started is not None else None,
        completed_at=ago(0.5) if state == "completed" else None, **fields,
    )


def cycle(issues: list[str], *, starts: float = 4, length: int = 7, carried_in: list[str] | None = None,
          number: int = 5) -> Cycle:
    """A cycle that started `starts` days ago at local midnight in New York (05:00 UTC in January)."""
    start = datetime.combine(day(int(starts)), datetime.min.time(), tzinfo=UTC) + timedelta(hours=5)
    return Cycle(number=number, starts_at=start, ends_at=start + timedelta(days=length), issues=issues,
                 carried_in=carried_in, previous_number=number - 1)


def ok(*names: str) -> list[SourceStatus]:
    return [SourceStatus(name=n, ok=True, complete=True) for n in names or ("linear", "github", "git")]


def snapshot(*, issues=(), cycle_=None, projects=(), prs=(), branches=(), local=(), sources=None,
             repos=(Repo(name="acme/app", default_branch="main"),)) -> Snapshot:
    return Snapshot(
        collected_at=NOW, scope=Scope(team="TEST", repos=["acme/app"]), sources=sources or ok(),
        team_name="Test", timezone="America/New_York", cycle=cycle_, issues=list(issues), projects=list(projects),
        repos=list(repos), pull_requests=list(prs), remote_branches=list(branches), local_repos=list(local),
    )


def project(pid: str, issues: list[str] | None, *, status: str = "started", target: date | None = None) -> Project:
    return Project(id=pid, name=f"Epic {pid}", url=f"https://linear.app/t/project/{pid}", status_name=status.title(),
                   status_type=status, lead="me", target_date=target, issues=issues)


def pr(number: int, *, created: float = 1, updated: float | None = None, commit: float | None = None, **fields
       ) -> PullRequest:
    return PullRequest(
        repo="acme/app", number=number, title=fields.pop("title", f"PR {number}"),
        url=f"https://github.com/acme/app/pull/{number}", author="me", created_at=ago(created),
        updated_at=ago(created if updated is None else updated), is_draft=fields.pop("is_draft", False),
        last_commit_at=ago(commit if commit is not None else (created if updated is None else updated)),
        head_ref=fields.pop("head_ref", f"feature-{number}"), base_ref="main", **fields,
    )


def branch(name: str, *, idle: float = 30, prs: list[tuple[int, str]] = (), ahead: int | None = None) -> RemoteBranch:
    return RemoteBranch(
        repo="acme/app", name=name, last_commit_at=ago(idle), ahead_of_default=ahead,
        prs=[AssociatedPR(number=n, state=s, url=f"https://github.com/acme/app/pull/{n}",
                          merged_at=ago(idle) if s == "MERGED" else None) for n, s in prs],
    )


def local_repo(branches: list[LocalBranch], worktrees: list[Worktree] | None = None) -> LocalRepo:
    return LocalRepo(
        repo="acme/app", path="/src/app", default_branch="main", last_fetched_at=ago(1), branches=branches,
        worktrees=worktrees or [Worktree(path="/src/app", branch="main", is_main=True, dirty=False)],
    )


def local_branch(name: str, *, idle: float = 30, merged: bool | None = False, gone: bool = False) -> LocalBranch:
    return LocalBranch(name=name, last_commit_at=ago(idle), upstream=f"origin/{name}", upstream_gone=gone,
                       merged=merged)
