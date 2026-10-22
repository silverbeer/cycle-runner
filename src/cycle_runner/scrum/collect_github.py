"""Collect the GitHub side of a Snapshot: open PRs and remote branches of the allowlisted repos.

Per repository: one paginated GraphQL query for open PRs (review, mergeability,
the last commit's check rollup), one for branches with their associated PRs,
then REST compares where a number can't come from GraphQL: how far each open
PR's head is behind its base, and whether a branch without any PR has commits
the default branch lacks.

A Linear issue is linked to a PR only by the documented convention: the team
key and number (`sb-123`, `SB-123`) in the head branch, the title or the body
("Fixes SB-123"). A PR without one is reported as unlinked, not guessed at.
"""

import logging
import re
from datetime import datetime

from cycle_runner.scrum.github_client import GitHubClient, GitHubError
from cycle_runner.scrum.models import AssociatedPR, PullRequest, RemoteBranch, Repo, SourceStatus

log = logging.getLogger(__name__)

MAX_PAGES = 10
MAX_COMPARES = 50  # per repository, for branches without a PR

PULLS = """
query ($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 50, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        number title url body createdAt updatedAt isDraft reviewDecision mergeable
        headRefName baseRefName headRepositoryOwner { login }
        author { login }
        reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

BRANCHES = """
query ($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(first: 5, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes { number state url mergedAt }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class Collected:
    def __init__(self) -> None:
        self.repos: list[Repo] = []
        self.pull_requests: list[PullRequest] = []
        self.branches: list[RemoteBranch] = []
        self.notes: list[str] = []
        self.failed: list[str] = []

    def status(self) -> SourceStatus:
        if self.failed and not self.repos:
            return SourceStatus(name="github", ok=False, complete=False, error="; ".join(self.failed))
        return SourceStatus(name="github", ok=True, complete=not (self.notes or self.failed),
                            notes=sorted(set(self.notes)) + self.failed)


async def collect(client: GitHubClient, repos: list[str], *, issue_key: str) -> Collected:
    """Read every allowlisted repository. A repository that fails is recorded; the others still count."""
    out = Collected()
    pattern = re.compile(rf"\b{re.escape(issue_key)}-(\d+)\b", re.IGNORECASE)
    for full_name in sorted(repos):
        try:
            await _repo(client, out, full_name, pattern, issue_key)
        except GitHubError as exc:
            out.failed.append(f"{full_name}: {exc}")
    return out


async def _repo(client: GitHubClient, out: Collected, full_name: str, pattern: re.Pattern, key: str) -> None:
    owner, name = full_name.split("/")
    default, pulls = None, []
    after = None
    for _ in range(MAX_PAGES):
        repo = (await client.query(PULLS, {"owner": owner, "name": name, "after": after}))["repository"]
        if repo is None:
            raise GitHubError("repository not found, or the token can't see it")
        default = (repo.get("defaultBranchRef") or {}).get("name")
        page = repo["pullRequests"]
        pulls += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    else:
        out.notes.append(f"{full_name}: more than {MAX_PAGES * 50} open PRs; the list is incomplete")
    out.repos.append(Repo(name=full_name, default_branch=default))

    for node in pulls:
        out.pull_requests.append(await _pull(client, out, full_name, node, pattern, key))

    after, refs = None, []
    for _ in range(MAX_PAGES):
        page = (await client.query(BRANCHES, {"owner": owner, "name": name, "after": after}))["repository"]["refs"]
        refs += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    else:
        out.notes.append(f"{full_name}: more than {MAX_PAGES * 100} branches; the list is incomplete")

    compares = 0
    for ref in sorted(refs, key=lambda r: r["name"]):
        if ref["name"] == default:
            continue
        prs = [AssociatedPR(number=p["number"], state=p["state"], url=p["url"], merged_at=_dt(p.get("mergedAt")))
               for p in ref["associatedPullRequests"]["nodes"]]
        ahead = None
        if not prs and default:
            if compares < MAX_COMPARES:
                compares += 1
                ahead = await _compare(client, out, full_name, default, ref["name"], field="ahead_by")
            else:
                out.notes.append(f"{full_name}: more than {MAX_COMPARES} branches without a PR; "
                                 "some weren't compared with the default branch")
        out.branches.append(RemoteBranch(
            repo=full_name, name=ref["name"], last_commit_at=_dt((ref.get("target") or {}).get("committedDate")),
            prs=sorted(prs, key=lambda p: p.number), ahead_of_default=ahead,
        ))


async def _pull(client: GitHubClient, out: Collected, full_name: str, node: dict, pattern: re.Pattern,
                key: str) -> PullRequest:
    commit = (node["commits"]["nodes"] or [{}])[0].get("commit") or {}
    head_owner = (node.get("headRepositoryOwner") or {}).get("login") or full_name.split("/")[0]
    head = node["headRefName"] if head_owner == full_name.split("/")[0] else f"{head_owner}:{node['headRefName']}"
    behind = await _compare(client, out, full_name, node["baseRefName"], head, field="behind_by")
    text = " ".join([node["headRefName"], node["title"], node.get("body") or ""])
    linear_ids = sorted({f"{key.upper()}-{int(n)}" for n in pattern.findall(text)}, key=lambda i: int(i.split("-")[1]))
    reviewers = [
        r["requestedReviewer"].get("login") or r["requestedReviewer"].get("name")
        for r in node["reviewRequests"]["nodes"] if r.get("requestedReviewer")
    ]
    return PullRequest(
        repo=full_name, number=node["number"], title=node["title"], url=node["url"],
        author=(node.get("author") or {}).get("login"),
        created_at=_dt(node["createdAt"]), updated_at=_dt(node["updatedAt"]), is_draft=node["isDraft"],
        review_decision=node.get("reviewDecision"), review_requests=sorted(filter(None, reviewers)),
        mergeable=node.get("mergeable"), checks=(commit.get("statusCheckRollup") or {}).get("state"),
        last_commit_at=_dt(commit.get("committedDate")), head_ref=node["headRefName"], base_ref=node["baseRefName"],
        behind_by=behind, linear_ids=linear_ids,
    )


async def _compare(client: GitHubClient, out: Collected, full_name: str, base: str, head: str, *,
                   field: str) -> int | None:
    response = await client.get(f"/repos/{full_name}/compare/{base}...{head}", {"per_page": 1})
    if response.status_code == 404:
        out.notes.append(f"{full_name}: couldn't compare {head} with {base}")
        return None
    return int(response.json()[field])


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
