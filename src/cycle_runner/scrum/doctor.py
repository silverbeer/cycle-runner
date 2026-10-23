"""`scrum-master doctor`: is everything the report needs present, reachable and read-only?

Every check is a read. Results say whether a credential is present and
accepted, never what it is.
"""

import os
import shutil
from dataclasses import dataclass
from typing import Literal

from cycle_runner.linear_client import LinearClient, LinearError
from cycle_runner.scrum import collect_git
from cycle_runner.scrum.config import Config
from cycle_runner.scrum.github_client import GitHubClient, GitHubError, write_scopes


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


async def run(config: Config, *, environ=os.environ, linear: LinearClient | None = None,
              github: GitHubClient | None = None) -> list[Check]:
    results = [Check("config", "ok", f"team {config.team}, {len(config.repos)} repos, timezone {config.timezone}")]
    results += await _linear(config, environ, linear)
    results += await _github(config, environ, github)
    results += _git(config)
    return results


def _presence(environ, *names: str) -> str | None:
    """Why the named variables can't be used, or None if they're all set and resolved."""
    for name in names:
        value = environ.get(name, "")
        if not value.strip():
            return f"{name} is not set"
        if value.startswith("op://"):
            return f"{name} is an unresolved op:// reference (run under `op run`)"
    return None


async def _linear(config: Config, environ, client: LinearClient | None) -> list[Check]:
    problem = _presence(environ, "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET")
    if problem and client is None:
        return [Check("linear credentials", "fail", problem)]
    out = [Check("linear credentials", "ok", "LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET present")]
    client = client or LinearClient.from_env(environ)
    try:
        data = await client.query(
            "query ($team: String!) { viewer { name } "
            "teams(first: 1, filter: {key: {eq: $team}}) { nodes { name activeCycle { number } } } }",
            {"team": config.team},
        )
    except LinearError as exc:
        return out + [Check("linear access", "fail", str(exc))]
    out.append(Check("linear access", "ok", f"read-only token granted to {data['viewer']['name']!r} (scope: read)"))
    teams = data["teams"]["nodes"]
    if not teams:
        out.append(Check("linear team", "fail", f"no team with key {config.team!r} visible"))
    else:
        cycle = teams[0].get("activeCycle")
        out.append(Check("linear team", "ok" if cycle else "warn",
                         f"{teams[0]['name']}: " + (f"active cycle {cycle['number']}" if cycle else "no active cycle")))
    return out


async def _github(config: Config, environ, client: GitHubClient | None) -> list[Check]:
    if not config.repos:
        return [Check("github", "warn", "no repositories configured in scrum.toml")]
    if client is None:
        try:
            client = GitHubClient.from_env(config.github.token_env, environ)
        except GitHubError as exc:
            return [Check("github token", "fail", f"{exc}; PR and branch sections will be missing")]
    name = client.source
    try:
        user = await client.get("/user")
    except GitHubError as exc:
        return [Check("github token", "fail", str(exc))]
    out = []
    risky = write_scopes(user)
    if risky:
        out.append(Check("github token", "warn", f"{name} is a classic token with write scopes "
                         f"({', '.join(sorted(risky))}); use a read-only fine-grained token"))
    else:
        out.append(Check("github token", "ok", f"{name} accepted"
                         + (" (fine-grained or read-only classic)" if user.status_code == 200 else "")))
    for repo in config.repos:
        try:
            response = await client.get(f"/repos/{repo.github}")
        except GitHubError as exc:
            out.append(Check(f"github {repo.github}", "fail", str(exc)))
            continue
        if response.status_code == 404:
            out.append(Check(f"github {repo.github}", "fail", "not found, or the token can't see it"))
        else:
            out.append(Check(f"github {repo.github}", "ok", f"readable, default branch "
                             f"{response.json().get('default_branch')}"))
    return out


def _git(config: Config) -> list[Check]:
    local = [r for r in config.repos if r.local]
    if not local:
        return []
    if shutil.which("git") is None:
        return [Check("git", "fail", "git is not on PATH; local branches and worktrees will be missing")]
    out = []
    for repo in local:
        path = repo.local_path
        if not (path / ".git").exists():
            out.append(Check(f"git {repo.github}", "warn", f"{repo.local} is not a git checkout"))
            continue
        try:
            collect_git.run(path, "rev-parse", "--git-common-dir")
            out.append(Check(f"git {repo.github}", "ok", f"{repo.local}"))
        except collect_git.GitError as exc:
            out.append(Check(f"git {repo.github}", "fail", str(exc)))
    return out
