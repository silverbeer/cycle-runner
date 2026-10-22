"""Collect local branches and worktrees from the allowlisted repositories' local checkouts.

Only read-only git commands run, and run() enforces it: an allowlist of
subcommands and arguments, GIT_OPTIONAL_LOCKS=0 so `status` doesn't refresh
the index, and no fetch. "Merged" therefore means merged into the
remote-tracking default branch as of the last fetch, and the snapshot records
when that fetch was, so a stale answer can be seen as stale.
"""

import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cycle_runner.scrum.config import RepoConfig
from cycle_runner.scrum.models import LocalBranch, LocalRepo, SourceStatus, Worktree

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20

# Each allowed command, as the leading arguments it must start with.
ALLOWED = (
    ("for-each-ref",),
    ("branch", "--merged"),
    ("worktree", "list", "--porcelain"),
    ("status", "--porcelain"),
    ("symbolic-ref", "--quiet", "--short"),
    ("rev-parse", "--git-common-dir"),
)


class GitError(Exception):
    pass


def run(repo: Path, *args: str) -> str:
    """Run one read-only git command in repo and return its stdout."""
    if not any(args[: len(prefix)] == prefix for prefix in ALLOWED):
        raise GitError(f"git {' '.join(args[:2])} is not an allowed read-only command")
    if any(arg in {"-d", "-D", "--delete", "-m", "-M", "--move", "--force", "-f"} for arg in args):
        raise GitError("git write flags are not allowed")
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise GitError(f"git {args[0]} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


class Collected:
    def __init__(self) -> None:
        self.repos: list[LocalRepo] = []
        self.notes: list[str] = []

    def status(self) -> SourceStatus:
        return SourceStatus(name="git", ok=True, complete=not self.notes, notes=sorted(set(self.notes)))


def collect(repos: list[RepoConfig], *, default_branches: dict[str, str | None]) -> Collected:
    out = Collected()
    for repo in sorted(repos, key=lambda r: r.github):
        path = repo.local_path
        if path is None:
            continue
        if not (path / ".git").exists():
            out.notes.append(f"{repo.github}: local checkout {repo.local} not found")
            continue
        try:
            out.repos.append(_repo(path, repo.github, default_branches.get(repo.github), out))
        except GitError as exc:
            out.notes.append(f"{repo.github}: {exc}")
    return out


def _repo(path: Path, name: str, default: str | None, out: Collected) -> LocalRepo:
    if default is None:
        try:
            default = run(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
            default = default.removeprefix("origin/")
        except GitError:
            out.notes.append(f"{name}: default branch unknown (GitHub unavailable, no origin/HEAD); "
                             "merged status not checked")
    merged: set[str] | None = None
    if default:
        try:
            merged = set(run(path, "branch", "--merged", f"refs/remotes/origin/{default}",
                             "--format=%(refname:short)").split())
        except GitError:
            out.notes.append(f"{name}: origin/{default} not found locally; merged status not checked")

    branches = []
    listing = run(path, "for-each-ref", "refs/heads",
                  "--format=%(refname:short)%09%(committerdate:iso-strict)%09%(upstream:short)%09%(upstream:track)")
    for line in listing.splitlines():
        branch, committed, upstream, track = (line.split("\t") + ["", "", ""])[:4]
        branches.append(LocalBranch(
            name=branch, last_commit_at=_dt(committed), upstream=upstream or None,
            upstream_gone=track.strip() == "[gone]",
            merged=None if merged is None else branch in merged,
        ))
    return LocalRepo(repo=name, path=str(path), default_branch=default, last_fetched_at=_fetched_at(path),
                     branches=sorted(branches, key=lambda b: b.name), worktrees=_worktrees(path, out, name))


def _worktrees(path: Path, out: Collected, name: str) -> list[Worktree]:
    blocks = run(path, "worktree", "list", "--porcelain").strip().split("\n\n")
    trees = []
    for index, block in enumerate(b for b in blocks if b.strip()):
        fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines())
        tree_path = fields.get("worktree", "")
        prunable = "prunable" in fields
        dirty = None
        if not prunable and Path(tree_path).is_dir():
            try:
                dirty = bool(run(Path(tree_path), "status", "--porcelain").strip())
            except GitError:
                out.notes.append(f"{name}: couldn't check {tree_path} for uncommitted changes")
        branch = fields.get("branch", "").removeprefix("refs/heads/") or None
        trees.append(Worktree(path=tree_path, branch=branch, is_main=index == 0, dirty=dirty, prunable=prunable))
    return trees


def _fetched_at(path: Path) -> datetime | None:
    try:
        common = Path(run(path, "rev-parse", "--git-common-dir").strip())
    except GitError:
        return None
    fetch_head = (common if common.is_absolute() else path / common) / "FETCH_HEAD"
    if not fetch_head.exists():
        return None
    return datetime.fromtimestamp(fetch_head.stat().st_mtime, UTC)


def _dt(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
