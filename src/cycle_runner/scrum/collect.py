"""Build a Snapshot from all three sources.

Linear is required: if it can't be read, collection fails and no snapshot is
made. GitHub and local git are not: when one fails, the snapshot says so in
its source status, and every report built from it is marked partial.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from cycle_runner.scrum import collect_git, collect_github, collect_linear
from cycle_runner.scrum.config import Config
from cycle_runner.scrum.github_client import GitHubClient, GitHubError
from cycle_runner.scrum.models import Scope, Snapshot, SourceStatus

log = logging.getLogger(__name__)


async def collect(
    config: Config,
    *,
    linear_query: Callable[[str, dict], Awaitable[dict]],
    github: GitHubClient | None,
    github_error: str | None = None,
    cycle: int | None = None,
    now: datetime | None = None,
) -> Snapshot:
    now = now or datetime.now(UTC)
    log.info("collect start", extra={"team": config.team, "repos": len(config.repos)})
    linear = await collect_linear.collect(linear_query, team=config.team, cycle_number=cycle)
    log.info("linear collected", extra={"issues": len(linear.issues), "projects": len(linear.projects)})

    repos = [r.github for r in config.repos]
    if not repos:
        gh_status = SourceStatus(name="github", ok=True, complete=True, notes=["no repositories configured"])
        gh = collect_github.Collected()
    elif github is None:
        gh = collect_github.Collected()
        gh_status = SourceStatus(name="github", ok=False, complete=False, error=github_error or "no GitHub client")
    else:
        gh = await collect_github.collect(github, repos, issue_key=config.team)
        gh_status = gh.status()
    log.info("github collected", extra={"pull_requests": len(gh.pull_requests), "ok": gh_status.ok})

    defaults = {r.name: r.default_branch for r in gh.repos}
    git = await asyncio.to_thread(collect_git.collect, config.repos, default_branches=defaults)
    log.info("git collected", extra={"local_repos": len(git.repos)})

    return Snapshot(
        collected_at=now,
        scope=Scope(team=config.team, repos=sorted(repos), cycle=cycle),
        sources=[linear.status(), gh_status, git.status()],
        team_name=linear.team_name,
        timezone=config.timezone,
        cycle=linear.cycle,
        issues=sorted(linear.issues.values(), key=lambda i: collect_linear._issue_order(i.identifier)),
        projects=linear.projects,
        repos=sorted(gh.repos, key=lambda r: r.name),
        pull_requests=sorted(gh.pull_requests, key=lambda p: (p.repo, p.number)),
        remote_branches=sorted(gh.branches, key=lambda b: (b.repo, b.name)),
        local_repos=git.repos,
    )


def github_client(config: Config) -> tuple[GitHubClient | None, str | None]:
    """The GitHub client, or why there isn't one."""
    try:
        return GitHubClient.from_env(config.github.token_env), None
    except GitHubError as exc:
        return None, str(exc)
