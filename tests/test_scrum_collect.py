"""Scrum Master collection: pagination, partial data, failures, and that nothing but reads ever leaves."""

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from scrum_factory import NOW

from cycle_runner.scrum import collect_git, collect_github, collect_linear
from cycle_runner.scrum.collect import collect
from cycle_runner.scrum.config import Config, RepoConfig
from cycle_runner.scrum.github_client import GRAPHQL_URL, GitHubClient, GitHubError

TOKEN = "ghp_" + "z" * 36


def node(n: int, state="unstarted", **extra) -> dict:
    data = {
        "identifier": f"SB-{n}", "title": f"Issue {n}", "url": f"https://linear.app/x/issue/SB-{n}",
        "priority": 3, "priorityLabel": "Medium", "estimate": 1,
        "createdAt": "2030-01-01T12:00:00.000Z", "updatedAt": "2030-01-02T12:00:00.000Z",
        "startedAt": None, "completedAt": None, "canceledAt": None, "dueDate": None, "addedToCycleAt": None,
        "state": {"name": state.title(), "type": state}, "assignee": {"displayName": "me"},
        "labels": {"nodes": []}, "cycle": None, "project": None, "parent": None,
        "children": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "inverseRelations": {"nodes": []},
    }
    data.update(extra)
    return data


def page(nodes, *, more=False, cursor="c1"):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


class FakeLinear:
    """Answers the collector's queries by shape. Records every (document, variables)."""

    def __init__(self, *, open_pages, cycle_issues=(), projects=(), project_issues=None, related=(),
                 previous=True, carried_more=False):
        self.open_pages, self.cycle_issues, self.projects = list(open_pages), list(cycle_issues), list(projects)
        self.project_issues = project_issues or {}
        self.related = {n["identifier"]: n for n in related}
        self.previous, self.carried_more = previous, carried_more
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, document: str, variables: dict) -> dict:
        self.calls.append((document, variables))
        if "teams(" in document:
            return {"teams": {"nodes": [{"key": "SB", "name": "Silverbeer", "timezone": "America/New_York",
                                         "activeCycle": {"number": 13}}]}}
        if "cycles(" in document:
            nodes = [{"id": "c13", "number": 13, "name": None, "startsAt": "2030-01-06T05:00:00.000Z",
                      "endsAt": "2030-01-13T05:00:00.000Z",
                      "uncompletedIssuesUponClose": page([])}]
            if self.previous:
                nodes.append({"id": "c12", "number": 12, "name": None, "startsAt": "2029-12-30T05:00:00.000Z",
                              "endsAt": "2030-01-06T05:00:00.000Z",
                              "uncompletedIssuesUponClose": page([{"identifier": "SB-1"}], more=self.carried_more)})
            return {"cycles": {"nodes": nodes}}
        if "projects(" in document:
            return {"projects": page(self.projects)}
        if "project(id" in document:
            return {"project": {"issues": page([{"identifier": i} for i in self.project_issues[variables["project"]]])}}
        f = variables["filter"]
        if "cycle" in f:
            return {"issues": page(self.cycle_issues)}
        if "number" in f:
            wanted = {f"SB-{n}" for n in f["number"]["in"]}
            return {"issues": page([n for i, n in self.related.items() if i in wanted])}
        index = 0 if variables["after"] is None else int(variables["after"][1:])
        nodes, more = self.open_pages[index], index + 1 < len(self.open_pages)
        return {"issues": page(nodes, more=more, cursor=f"c{index + 1}")}


def linear(fake) -> collect_linear.Collected:
    return asyncio.run(collect_linear.collect(fake, team="SB"))


# --- Linear ----------------------------------------------------------------------------------


def test_linear_pages_through_open_issues_and_dedupes():
    fake = FakeLinear(open_pages=[[node(1), node(2)], [node(2), node(3)]], cycle_issues=[node(1), node(4, "completed")])
    out = linear(fake)
    assert sorted(out.issues) == ["SB-1", "SB-2", "SB-3", "SB-4"]
    assert out.cycle.issues == ["SB-1", "SB-4"] and out.cycle.carried_in == ["SB-1"]
    assert out.status().complete


def test_linear_pagination_cap_is_reported(monkeypatch):
    monkeypatch.setattr(collect_linear, "MAX_PAGES", 2)
    out = linear(FakeLinear(open_pages=[[node(1)], [node(2)], [node(3)]]))
    assert not out.status().complete
    assert any("open issues: stopped after" in n for n in out.notes)


def test_truncated_children_are_unknown_not_short():
    big = node(1, children={"nodes": [{"identifier": "SB-2"}], "pageInfo": {"hasNextPage": True}})
    out = linear(FakeLinear(open_pages=[[big]]))
    assert out.issues["SB-1"].children is None
    assert "SB-1 has more than 50 children; its child counts are hidden" in out.notes


def test_completed_relatives_and_project_issues_are_fetched():
    parent = node(1, children={"nodes": [{"identifier": "SB-2"}], "pageInfo": {"hasNextPage": False}})
    project = {"id": "p1", "name": "Epic", "url": "u", "status": {"name": "Started", "type": "started"},
               "lead": None, "targetDate": "2030-02-01"}
    fake = FakeLinear(open_pages=[[parent]], projects=[project], project_issues={"p1": ["SB-1", "SB-3"]},
                      related=[node(2, "completed"), node(3, "completed")])
    out = linear(fake)
    assert out.issues["SB-2"].is_done and out.issues["SB-3"].is_done
    assert out.projects[0].issues == ["SB-1", "SB-3"]
    assert out.status().complete


def test_unreadable_relatives_and_unknown_carry_in_are_noted():
    parent = node(1, children={"nodes": [{"identifier": "SB-2"}], "pageInfo": {"hasNextPage": False}})
    out = linear(FakeLinear(open_pages=[[parent]], previous=False))
    assert out.cycle.carried_in is None
    assert "cycle 12 was not found; carry-in is unknown" in out.notes
    assert any("1 related issues could not be read" in n for n in out.notes)


def test_only_open_blockers_count():
    relations = {"nodes": [{"type": "blocks", "issue": {"identifier": "SB-8", "state": {"type": "started"}}},
                           {"type": "blocks", "issue": {"identifier": "SB-9", "state": {"type": "completed"}}},
                           {"type": "related", "issue": {"identifier": "SB-7", "state": {"type": "started"}}}]}
    out = linear(FakeLinear(open_pages=[[node(1, inverseRelations=relations)]]))
    assert out.issues["SB-1"].blocked_by == ["SB-8"]


def test_linear_sends_only_queries():
    fake = FakeLinear(open_pages=[[node(1)]])
    linear(fake)
    assert all(doc.lstrip().startswith("query") for doc, _ in fake.calls)
    assert not any("mutation" in doc for doc, _ in fake.calls)


# --- GitHub ----------------------------------------------------------------------------------


def pr_node(number, **extra):
    data = {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/acme/app/pull/{number}",
        "body": "Fixes SB-12 and sb-3", "createdAt": "2030-01-01T00:00:00Z", "updatedAt": "2030-01-05T00:00:00Z",
        "isDraft": False, "reviewDecision": None, "mergeable": "MERGEABLE",
        "headRefName": f"silverbeer/sb-{number}-thing", "baseRefName": "main",
        "headRepositoryOwner": {"login": "acme"}, "author": {"login": "me"},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "rev"}}]},
        "commits": {"nodes": [{"commit": {"committedDate": "2030-01-04T00:00:00Z",
                                          "statusCheckRollup": {"state": "FAILURE"}}}]},
    }
    data.update(extra)
    return data


class FakeGitHub:
    def __init__(self, *, pulls=(), refs=(), missing_repos=(), fail_compare=False):
        self.pulls, self.refs, self.missing, self.fail_compare = list(pulls), list(refs), set(missing_repos), fail_compare
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GRAPHQL_URL:
            body = json.loads(request.content)
            repo = f"{body['variables']['owner']}/{body['variables']['name']}"
            if repo in self.missing:
                return httpx.Response(200, json={"data": {"repository": None}})
            if "pullRequests" in body["query"]:
                return httpx.Response(200, json={"data": {"repository": {
                    "defaultBranchRef": {"name": "main"},
                    "pullRequests": {"nodes": self.pulls, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
            return httpx.Response(200, json={"data": {"repository": {
                "refs": {"nodes": self.refs, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
        if "/compare/" in request.url.path:
            if self.fail_compare:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"behind_by": 4, "ahead_by": 0})
        return httpx.Response(200, json={"login": "me"})

    def client(self) -> GitHubClient:
        return GitHubClient(token=TOKEN, transport=httpx.MockTransport(self.handler), sleep=_no_sleep)


async def _no_sleep(seconds):
    return None


def test_github_pull_requests_are_parsed_with_linear_links():
    fake = FakeGitHub(pulls=[pr_node(7)])
    out = asyncio.run(collect_github.collect(fake.client(), ["acme/app"], issue_key="SB"))
    pull = out.pull_requests[0]
    assert pull.linear_ids == ["SB-3", "SB-7", "SB-12"]
    assert (pull.checks, pull.behind_by, pull.review_requests) == ("FAILURE", 4, ["rev"])
    assert out.repos[0].default_branch == "main" and out.status().complete


def test_github_branches_get_prs_and_ahead_counts_only_without_a_pr():
    refs = [
        {"name": "main", "target": {"committedDate": "2030-01-01T00:00:00Z"}, "associatedPullRequests": {"nodes": []}},
        {"name": "old", "target": {"committedDate": "2029-11-01T00:00:00Z"}, "associatedPullRequests": {"nodes": []}},
        {"name": "done", "target": {"committedDate": "2029-11-01T00:00:00Z"}, "associatedPullRequests": {
            "nodes": [{"number": 2, "state": "MERGED", "url": "u", "mergedAt": "2029-11-02T00:00:00Z"}]}},
    ]
    fake = FakeGitHub(refs=refs)
    out = asyncio.run(collect_github.collect(fake.client(), ["acme/app"], issue_key="SB"))
    by_name = {b.name: b for b in out.branches}
    assert set(by_name) == {"old", "done"}
    assert by_name["old"].ahead_of_default == 0 and by_name["done"].ahead_of_default is None
    assert sum("/compare/" in r.url.path for r in fake.requests) == 1


def test_a_failing_repo_is_recorded_and_the_rest_still_count():
    fake = FakeGitHub(pulls=[pr_node(1)], missing_repos={"acme/gone"})
    out = asyncio.run(collect_github.collect(fake.client(), ["acme/app", "acme/gone"], issue_key="SB"))
    status = out.status()
    assert status.ok and not status.complete
    assert status.notes == ["acme/gone: repository not found, or the token can't see it"]
    assert [p.repo for p in out.pull_requests] == ["acme/app"]


def test_unknown_behind_count_is_none_with_a_note():
    fake = FakeGitHub(pulls=[pr_node(1)], fail_compare=True)
    out = asyncio.run(collect_github.collect(fake.client(), ["acme/app"], issue_key="SB"))
    assert out.pull_requests[0].behind_by is None
    assert "acme/app: couldn't compare silverbeer/sb-1-thing with main" in out.notes


def test_github_sends_only_gets_and_graphql_queries():
    fake = FakeGitHub(pulls=[pr_node(1)], refs=[])
    asyncio.run(collect_github.collect(fake.client(), ["acme/app"], issue_key="SB"))
    for request in fake.requests:
        if request.method == "POST":
            assert str(request.url) == GRAPHQL_URL
            assert json.loads(request.content)["query"].lstrip().startswith("query")
        else:
            assert request.method == "GET"


def test_github_client_refuses_mutations():
    client = FakeGitHub().client()
    for document in ["mutation { mergePullRequest(input: {}) { clientMutationId } }",
                     "query { x } mutation { y }", "subscription { z }"]:
        with pytest.raises(GitHubError, match="only GraphQL queries"):
            asyncio.run(client.query(document))


def test_github_retries_transient_failures_and_hides_the_token():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(502, text=f"bad gateway {TOKEN}")
        return httpx.Response(200, json={"ok": True})

    client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    assert asyncio.run(client.get("/user")).json() == {"ok": True} and len(calls) == 3

    def always_502(request):
        return httpx.Response(502, text=f"bad gateway {TOKEN}")

    client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(always_502), sleep=_no_sleep)
    with pytest.raises(GitHubError) as info:
        asyncio.run(client.get("/repos/acme/app?secret=1"))
    assert str(info.value) == "GitHub returned HTTP 502 for GET /repos/acme/app"
    assert TOKEN not in repr(client) and TOKEN not in str(info.value)


def test_github_rejected_token_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = GitHubClient(token=TOKEN, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(GitHubError, match="401"):
        asyncio.run(client.get("/user"))
    assert len(calls) == 1


@pytest.mark.parametrize("value, message", [("", "is not set"), ("op://agents/x/credential", "unresolved")])
def test_github_token_must_be_present_and_resolved(value, message):
    with pytest.raises(GitHubError, match=message):
        GitHubClient.from_env("SCRUM_GITHUB_TOKEN", {"SCRUM_GITHUB_TOKEN": value})


# --- orchestration ---------------------------------------------------------------------------


def test_missing_github_token_gives_a_partial_snapshot_not_a_crash():
    config = Config(team="SB", repos=[RepoConfig(github="acme/app")])
    snap = asyncio.run(collect(config, linear_query=FakeLinear(open_pages=[[node(1)]]), github=None,
                               github_error="SCRUM_GITHUB_TOKEN is not set", now=NOW))
    github = next(s for s in snap.sources if s.name == "github")
    assert (github.ok, github.error) == (False, "SCRUM_GITHUB_TOKEN is not set")
    assert [i.identifier for i in snap.issues] == ["SB-1"]


def test_snapshot_round_trips_through_json():
    config = Config(team="SB", repos=[RepoConfig(github="acme/app")])
    fake = FakeGitHub(pulls=[pr_node(1)])
    snap = asyncio.run(collect(config, linear_query=FakeLinear(open_pages=[[node(1)]]), github=fake.client(), now=NOW))
    from cycle_runner.scrum.models import Snapshot

    assert Snapshot.model_validate_json(snap.model_dump_json()) == snap


# --- local git -------------------------------------------------------------------------------


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def checkout(tmp_path):
    """A clone with: a merged branch, an unmerged branch, a branch whose upstream is gone, and a worktree."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(work, *args)
    git(work, "commit", "-q", "--allow-empty", "-m", "base")
    git(work, "push", "-q", "-u", "origin", "main")
    git(work, "branch", "merged")
    git(work, "checkout", "-q", "-b", "wip")
    git(work, "commit", "-q", "--allow-empty", "-m", "wip")
    git(work, "checkout", "-q", "-b", "gone")
    git(work, "push", "-q", "-u", "origin", "gone")
    git(work, "push", "-q", "origin", "--delete", "gone")
    git(work, "fetch", "-q", "--prune")
    git(work, "checkout", "-q", "main")
    git(work, "worktree", "add", "-q", str(tmp_path / "tree"), "merged")
    (tmp_path / "tree" / "scratch.txt").write_text("uncommitted")
    return work


def test_git_collects_branches_and_worktrees(checkout, tmp_path):
    out = collect_git.collect([RepoConfig(github="acme/app", local=str(checkout))], default_branches={"acme/app": "main"})
    assert out.status().complete, out.notes
    repo = out.repos[0]
    branches = {b.name: b for b in repo.branches}
    assert branches["merged"].merged and not branches["wip"].merged
    assert branches["gone"].upstream_gone and not branches["main"].upstream_gone
    trees = {Path(w.path).name: w for w in repo.worktrees}
    assert trees["work"].is_main and trees["tree"].dirty is True and trees["tree"].branch == "merged"
    assert repo.last_fetched_at is not None


def test_git_collection_changes_nothing(checkout):
    before = git(checkout, "for-each-ref"), git(checkout, "worktree", "list", "--porcelain")
    index = (checkout / ".git" / "index").stat().st_mtime_ns
    collect_git.collect([RepoConfig(github="acme/app", local=str(checkout))], default_branches={"acme/app": "main"})
    assert (git(checkout, "for-each-ref"), git(checkout, "worktree", "list", "--porcelain")) == before
    assert (checkout / ".git" / "index").stat().st_mtime_ns == index


@pytest.mark.parametrize("args", [
    ("branch", "-D", "x"), ("branch", "--merged", "main", "-d", "x"), ("push", "origin"), ("fetch",),
    ("worktree", "remove", "x"), ("checkout", "x"), ("gc",),
])
def test_git_refuses_anything_but_reads(tmp_path, args):
    with pytest.raises(collect_git.GitError):
        collect_git.run(tmp_path, *args)


def test_missing_checkout_is_noted():
    out = collect_git.collect([RepoConfig(github="acme/app", local="/nonexistent/x")], default_branches={})
    assert out.notes == ["acme/app: local checkout /nonexistent/x not found"] and not out.status().complete
