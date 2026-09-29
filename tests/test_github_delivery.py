"""Human approval and GitHub delivery, against a stand-in GitHub: a local bare repository for
git, and a mocked REST API that reads it. No network, no real GitHub."""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cycle_runner import executor as executor_module
from cycle_runner.delivery_approval import ApprovalRefused, approve, delivery_state, reject, review
from cycle_runner.executor import ExecutionResult, details_path, ExecutionWorkspace, run_request
from cycle_runner.git_delivery import AUTHOR_EMAIL, AUTHOR_NAME, LocalGitDelivery
from cycle_runner.github_delivery import GitHubApi, GitHubDelivery, GitHubDeliveryError, _Pusher, pr_body
from cycle_runner.projects import WorkspaceResolver, load_projects
from cycle_runner.work_requests import WorkRequestStore
from disposable_repo import make_repo

REPO = "silverbeer/cycle-runner-sandbox"
TOKEN = "github_pat_" + "T" * 60  # a fake credential; must never show up anywhere


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=check).stdout.strip()


class Agent:
    name = "agent"

    def execute(self, task, workspace):
        (workspace.path / "src" / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
        return ExecutionResult(outcome="changed", message="Added greet.", files_changed=["src/hello.py"],
                               details={"summary": "Added greet(name) to src/hello.py; @someone fixes #12.",
                                        "tests_observed": 2, "tests_output_tail": "Ran 2 tests in 0.001s\n\nOK"})


class FakeGitHub:
    """The GitHub REST calls delivery makes, answered from a bare repository."""

    def __init__(self, bare):
        self.bare = bare
        self.pulls: list[dict] = []
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}  # "GET /repos/..." prefix -> status, once
        self.permissions = {"push": True}

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        key = f"{request.method} {url.path}"
        self.calls.append(key)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        for prefix, status in list(self.fail.items()):
            if key.startswith(prefix):
                del self.fail[prefix]
                return httpx.Response(status, json={"message": "Bad credentials" if status == 401 else "boom"})
        path = url.path.removeprefix(f"/repos/{REPO}")
        if url.path == f"/repos/{REPO}":
            return httpx.Response(200, json={"full_name": REPO, "archived": False, "permissions": self.permissions})
        if not url.path.startswith(f"/repos/{REPO}/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/git/ref/heads/"):
            sha = git(self.bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{path.removeprefix('/git/ref/heads/')}",
                      check=False)
            return httpx.Response(200, json={"object": {"type": "commit", "sha": sha}}) if sha else \
                httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/compare/"):
            base, head = path.removeprefix("/compare/").split("...")
            if git(self.bare, "cat-file", "-t", head, check=False) != "commit":
                return httpx.Response(404, json={"message": "Not Found"})
            behind = subprocess.run(["git", "-C", str(self.bare), "merge-base", "--is-ancestor", head, base]).returncode == 0
            return httpx.Response(200, json={"status": "behind" if behind else "diverged"})
        if path == "/pulls" and request.method == "GET":
            head = parse_qs(url.query)["head"][0].split(":", 1)[1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"]["ref"] == head])
        if path == "/pulls" and request.method == "POST":
            body = json.loads(request.content)
            pull = {"number": len(self.pulls) + 1, "html_url": f"https://github.com/{REPO}/pull/{len(self.pulls) + 1}",
                    "draft": body["draft"], "title": body["title"], "body": body["body"], "state": "open",
                    "merged_at": None, "base": {"ref": body["base"]},
                    "head": {"ref": body["head"], "sha": git(self.bare, "rev-parse", f"refs/heads/{body['head']}")}}
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def setup(tmp_path, work_request_db, monkeypatch):
    """A DEMO project delivered locally (V1.3), and a stand-in GitHub holding its main branch."""
    origin = make_repo(tmp_path / "origin")
    bare = tmp_path / "github.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(origin.path), str(bare)], check=True)
    config_path = tmp_path / "projects.toml"
    config_path.write_text(
        f'workspace_root = "{tmp_path / "workspaces"}"\n[projects.DEMO]\nrepository = "{origin.path}"\n'
        f'test_command = "{origin.test_command}"\nbranch = "main"\ngithub = "{REPO}"\n'
    )
    monkeypatch.setenv("CYCLE_RUNNER_PROJECTS", str(config_path))
    store = WorkRequestStore(work_request_db)
    request, _ = store.create_for_approval(
        recommendation_id="rec-1", issue_id="SB-640", approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=1, title_at_approval="Add a greet(name) function", rationale="r", project_id="DEMO",
    )
    config = load_projects()
    done, _ = run_request(store, Agent(), WorkspaceResolver(config), request.work_request_id,
                          deliverer=LocalGitDelivery(environ={}))
    assert (done.outcome, done.branch) == ("changed", "cycle-runner/WR-000001")
    github = FakeGitHub(bare)

    class Setup:
        pass

    s = Setup()
    s.store, s.config, s.github, s.bare, s.origin = store, config, github, bare, origin
    s.wr, s.sha = done.work_request_id, done.commit_sha
    s.workspace = config.workspace_root / done.work_request_id
    s.delivery = lambda **kw: GitHubDelivery(store, load_projects(), token=TOKEN,
                                             api=GitHubApi(REPO, TOKEN, transport=github.transport()),
                                             push_url=str(bare), push_protocol="file", **kw)
    return s


def _approve(s, commit=None):
    return approve(s.store, s.config, s.wr, commit=commit or s.sha[:12], approved_by="cli:tom@host")


# --- review and approval --------------------------------------------------------------------


def test_a_changed_request_is_review_pending_and_verified(setup):
    current = review(setup.store, setup.config, setup.wr)
    assert current.state == "review_pending" and current.problem is None
    assert current.verified.commit_sha == setup.sha
    assert current.verified.base_sha == git(setup.origin.path, "rev-parse", "main")


def test_approval_records_the_exact_sha_and_what_the_human_saw(setup):
    approval = _approve(setup)

    assert (approval.commit_sha, approval.branch, approval.repository) == (setup.sha, "cycle-runner/WR-000001", REPO)
    assert approval.approved_by == "cli:tom@host" and approval.status == "approved"
    assert approval.base_sha == git(setup.origin.path, "rev-parse", "main")
    evidence = approval.evidence
    assert evidence["message"].startswith("SB-640: Add a greet(name) function")
    assert evidence["diff"]["files_changed"] == ["src/hello.py"]
    assert evidence["tests"] == {"observed_runs": 2, "command": None, "last_result": "OK"}
    assert evidence["tree_sha"] == git(setup.workspace, "rev-parse", f"{setup.sha}^{{tree}}")
    assert review(setup.store, setup.config, setup.wr).state == "approved"
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == ""  # approving pushes nothing


@pytest.mark.parametrize("commit", ["abc1234", "", "12345", "zzzzzzzz"])
def test_approval_must_name_the_reviewed_commit(setup, commit):
    with pytest.raises(ApprovalRefused, match="you named"):
        _approve(setup, commit=commit or "x")
    assert setup.store.approvals_for(setup.wr) == []


def _break(s, how):
    ws = s.workspace
    if how == "missing workspace":
        shutil.rmtree(ws)
    elif how == "missing branch":
        git(ws, "switch", "-q", "main")
        git(ws, "branch", "-q", "-D", "cycle-runner/WR-000001")
    elif how == "wrong HEAD":
        git(ws, "switch", "-q", "main")
    elif how == "unexpected commit":
        (ws / "extra.txt").write_text("more\n")
        git(ws, "add", "extra.txt")
        git(ws, "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}", "commit", "-qm", "more")
    elif how == "amended commit":
        (ws / "src" / "hello.py").write_text("print('something else')\n")
        git(ws, "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}", "commit", "-qa", "--amend",
            "--no-edit")
    elif how == "unexpected remote":
        git(ws, "remote", "add", "origin", "https://example.invalid/x.git")
    elif how == "changed diff record":
        record_path = details_path(ExecutionWorkspace(path=ws, test_command=""))
        record = json.loads(record_path.read_text())
        record["delivery"]["diff"]["files"][0]["insertions"] += 1
        record_path.write_text(json.dumps(record))
    elif how == "hostile config":
        git(ws, "config", "core.sshCommand", "touch /tmp/pwned")


@pytest.mark.parametrize("how", ["missing workspace", "missing branch", "wrong HEAD", "unexpected commit",
                                 "amended commit", "unexpected remote", "changed diff record", "hostile config"])
def test_approval_is_refused_when_the_local_commit_doesnt_check_out(setup, how):
    _break(setup, how)
    with pytest.raises(ApprovalRefused, match="approval refused"):
        _approve(setup)
    assert setup.store.approvals_for(setup.wr) == []
    assert review(setup.store, setup.config, setup.wr).problem


def _manual_delivery(s, files: dict[str, str], commit: str | None = None):
    """A request whose recorded local commit was made by hand (e.g. including a protected file)."""
    request, _ = s.store.create_for_approval(
        recommendation_id="rec-manual", issue_id="SB-2", approved_by="t", approved_at=datetime.now(UTC),
        cycle_number=1, title_at_approval="Manual", rationale="r", project_id="DEMO",
    )
    wr = request.work_request_id
    ws = s.config.workspace_root / wr
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(s.origin.path), str(ws)], check=True)
    git(ws, "remote", "remove", "origin")
    git(ws, "config", "--unset", "user.email", check=False)
    git(ws, "switch", "-q", "-c", f"cycle-runner/{wr}")
    for name, content in files.items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content)
        git(ws, "add", "-f", name)
    git(ws, "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}", "commit", "-qm", "SB-2: Manual")
    sha = commit or git(ws, "rev-parse", "HEAD")
    diff_out = [subprocess.run(["git", "-C", str(ws), "diff-tree", "-r", "--no-commit-id", flag, "-z", "--no-renames",
                                "HEAD^", "HEAD"], capture_output=True, text=True).stdout
                for flag in ("--name-status", "--numstat")]
    from cycle_runner.git_delivery import _diff_summary

    diff = _diff_summary(*diff_out)
    details_path(ExecutionWorkspace(path=ws, test_command="")).write_text(json.dumps(
        {"outcome": "changed", "details": {}, "delivery": {"branch": f"cycle-runner/{wr}", "commit": sha, "diff": diff}}))
    s.store.claim(wr, "x")
    s.store.start(wr)
    s.store.finish(wr, "changed", "manual", branch=f"cycle-runner/{wr}", commit_sha=sha)
    return wr, sha


@pytest.mark.parametrize(("files", "problem"), [
    ({".env": "X=1\n"}, "the commit contains .env"),
    ({"config/credentials.json": "{}\n"}, "the commit contains config/credentials.json"),
    ({"src/keys.py": "K = 'ghp_" + "a" * 36 + "'\n"}, "looks like it contains a credential"),
])
def test_approval_is_refused_for_a_commit_with_protected_content(setup, files, problem):
    wr, sha = _manual_delivery(setup, files)
    with pytest.raises(ApprovalRefused, match=problem):
        approve(setup.store, setup.config, wr, commit=sha[:12], approved_by="t")
    assert setup.store.approvals_for(wr) == []


def test_approval_is_refused_for_a_commit_not_made_by_cycle_runner(setup):
    wr, sha = _manual_delivery(setup, {"src/a.py": "a = 1\n"})
    ws = setup.config.workspace_root / wr
    git(ws, "-c", "user.name=Someone", "-c", "user.email=s@x", "commit", "-q", "--amend", "--no-edit",
        "--reset-author")
    new = git(ws, "rev-parse", "HEAD")
    assert new != sha
    with pytest.raises(ApprovalRefused, match="approval refused"):
        approve(setup.store, setup.config, wr, commit=sha[:12], approved_by="t")


def test_approval_is_refused_for_a_missing_commit(setup):
    wr, _ = _manual_delivery(setup, {"src/a.py": "a = 1\n"}, commit="d" * 40)
    with pytest.raises(ApprovalRefused, match="no longer exists"):
        approve(setup.store, setup.config, wr, commit="d" * 12, approved_by="t")


@pytest.mark.parametrize("outcome", ["no_change", "failed"])
def test_no_change_and_failed_requests_are_never_deliverable(setup, outcome):
    request, _ = setup.store.create_for_approval(
        recommendation_id=f"rec-{outcome}", issue_id="SB-3", approved_by="t", approved_at=datetime.now(UTC),
        cycle_number=1, title_at_approval="t", rationale="r", project_id="DEMO",
    )
    setup.store.claim(request.work_request_id, "x")
    setup.store.start(request.work_request_id)
    done = setup.store.finish(request.work_request_id, outcome, "m")

    assert delivery_state(done, []) == "not_deliverable"
    with pytest.raises(ApprovalRefused, match="only a changed request is delivered"):
        approve(setup.store, setup.config, done.work_request_id, commit="a" * 12, approved_by="t")
    with pytest.raises(GitHubDeliveryError, match="no approval"):
        setup.delivery().deliver(done.work_request_id)


def test_a_project_without_a_github_repository_cant_be_approved(setup, tmp_path, monkeypatch):
    path = tmp_path / "no-github.toml"
    path.write_text((tmp_path / "projects.toml").read_text().replace(f'github = "{REPO}"\n', ""))
    monkeypatch.setenv("CYCLE_RUNNER_PROJECTS", str(path))
    with pytest.raises(ApprovalRefused, match="no GitHub repository configured"):
        approve(setup.store, load_projects(), setup.wr, commit=setup.sha[:12], approved_by="t")


def test_a_rejected_commit_is_never_delivered(setup):
    rejected = reject(setup.store, setup.wr, commit=setup.sha[:12], rejected_by="t", reason="not this way")
    assert rejected.status == "rejected"
    assert review(setup.store, setup.config, setup.wr).state == "rejected"
    with pytest.raises(GitHubDeliveryError, match="no approval"):
        setup.delivery().deliver(setup.wr)


# --- delivery --------------------------------------------------------------------------------


def test_delivery_pushes_exactly_the_approved_commit_and_opens_a_draft_pr(setup):
    before = setup.store.get(setup.wr)
    _approve(setup)

    approval = setup.delivery().deliver(setup.wr)

    assert approval.status == "pr_created" and approval.pr_number == 1
    assert approval.pr_url == f"https://github.com/{REPO}/pull/1"
    assert git(setup.bare, "rev-parse", "refs/heads/cycle-runner/WR-000001") == setup.sha
    assert git(setup.bare, "rev-parse", "cycle-runner/WR-000001^") == git(setup.bare, "rev-parse", "main")
    assert sorted(git(setup.bare, "for-each-ref", "--format=%(refname)").split()) == \
        ["refs/heads/cycle-runner/WR-000001", "refs/heads/main"]  # one branch, no tags
    (pull,) = setup.github.pulls
    assert pull["draft"] is True and pull["base"]["ref"] == "main" and pull["head"]["ref"] == "cycle-runner/WR-000001"
    assert pull["title"] == "SB-640: Add a greet(name) function"
    body = pull["body"]
    for fact in ("Cycle Runner", "SB-640", "Add a greet(name) function", "DEMO", "WR-000001", setup.sha,
                 "`src/hello.py` (modified", "ran 2 time(s)", "`OK`", "cli:tom@\u200bhost"):
        assert fact in body, fact
    assert "@someone" not in body and "fixes #12" not in body  # neutralized
    assert setup.store.get(setup.wr) == before  # the engineering record is untouched
    assert git(setup.workspace, "remote") == ""  # still no remote in the clone


def test_delivery_is_idempotent(setup, monkeypatch):
    _approve(setup)
    pushes = []
    real_push = _Pusher.push
    monkeypatch.setattr(_Pusher, "push", lambda self, *a, **kw: (pushes.append(1), real_push(self, *a, **kw))[1])

    first = setup.delivery().deliver(setup.wr)
    second = setup.delivery().deliver(setup.wr)

    assert first == second and len(pushes) == 1 and len(setup.github.pulls) == 1
    assert git(setup.bare, "rev-list", "--count", "main..cycle-runner/WR-000001") == "1"


def test_an_already_pushed_branch_is_not_pushed_again(setup, monkeypatch):
    _approve(setup)
    subprocess.run(["git", "-C", str(setup.workspace), "push", "-q", str(setup.bare),
                    f"{setup.sha}:refs/heads/cycle-runner/WR-000001"], check=True)
    monkeypatch.setattr(_Pusher, "push", lambda *a, **kw: pytest.fail("pushed again"))

    assert setup.delivery().deliver(setup.wr).status == "pr_created"


def test_an_existing_pr_for_the_approved_commit_is_reused(setup):
    _approve(setup)
    subprocess.run(["git", "-C", str(setup.workspace), "push", "-q", str(setup.bare),
                    f"{setup.sha}:refs/heads/cycle-runner/WR-000001"], check=True)
    setup.github.pulls.append({"number": 9, "html_url": f"https://github.com/{REPO}/pull/9", "draft": True,
                               "state": "open", "base": {"ref": "main"},
                               "head": {"ref": "cycle-runner/WR-000001", "sha": setup.sha}})

    approval = setup.delivery().deliver(setup.wr)

    assert (approval.pr_number, len(setup.github.pulls)) == (9, 1)


def test_a_commit_changed_after_approval_is_never_pushed_and_the_approval_dies(setup):
    first = _approve(setup)
    _break(setup, "amended commit")

    with pytest.raises(GitHubDeliveryError, match="is now invalid; nothing pushed"):
        setup.delivery().deliver(setup.wr)

    assert setup.store.get_approval(first.approval_id).status == "invalid"
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == "" and setup.github.pulls == []
    assert "POST" not in " ".join(setup.github.calls)
    with pytest.raises(GitHubDeliveryError, match="no approval"):
        setup.delivery().deliver(setup.wr)  # nothing to deliver until a human approves again
    with pytest.raises(ApprovalRefused):
        _approve(setup)  # and the moved commit can't be approved: it isn't the recorded one


def test_after_an_invalidated_approval_the_original_commit_needs_a_new_explicit_approval(setup):
    first = _approve(setup)
    git(setup.workspace, "switch", "-q", "main")
    with pytest.raises(GitHubDeliveryError):
        setup.delivery().deliver(setup.wr)
    git(setup.workspace, "switch", "-q", "cycle-runner/WR-000001")  # back as it was

    second = _approve(setup)

    assert second.approval_id != first.approval_id and second.commit_sha == first.commit_sha
    assert setup.delivery().deliver(setup.wr).status == "pr_created"


def test_an_auth_failure_is_a_recorded_delivery_failure(setup):
    approval = _approve(setup)
    before = setup.store.get(setup.wr)
    setup.github.fail[f"GET /repos/{REPO}"] = 401

    with pytest.raises(GitHubDeliveryError, match="refused the credential"):
        setup.delivery().deliver(setup.wr)

    after = setup.store.get_approval(approval.approval_id)
    assert after.status == "approved" and "401" in after.last_error and TOKEN not in after.last_error
    assert setup.store.get(setup.wr) == before  # still completed, changed
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == "" and setup.github.pulls == []
    assert git(setup.workspace, "rev-parse", "HEAD") == setup.sha  # the approved commit untouched


def test_a_token_that_cant_push_stops_before_pushing(setup):
    _approve(setup)
    setup.github.permissions = {"push": False}
    with pytest.raises(GitHubDeliveryError, match="can't push"):
        setup.delivery().deliver(setup.wr)
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == ""


def test_a_push_failure_is_recorded_and_opens_no_pr(setup, tmp_path):
    approval = _approve(setup)
    delivery = GitHubDelivery(setup.store, setup.config, token=TOKEN,
                              api=GitHubApi(REPO, TOKEN, transport=setup.github.transport()),
                              push_url=str(tmp_path / "nowhere.git"), push_protocol="file")

    with pytest.raises(GitHubDeliveryError, match="^push:"):
        delivery.deliver(setup.wr)

    after = setup.store.get_approval(approval.approval_id)
    assert after.status == "approved" and after.last_error.startswith("push:") and TOKEN not in after.last_error
    assert setup.github.pulls == [] and git(setup.workspace, "rev-list", "--count", "main..HEAD") == "1"


def test_push_succeeds_pr_fails_then_a_retry_creates_only_the_pr(setup, monkeypatch):
    approval = _approve(setup)
    setup.github.fail[f"POST /repos/{REPO}/pulls"] = 502

    with pytest.raises(GitHubDeliveryError, match="pushed, but creating the draft PR failed"):
        setup.delivery().deliver(setup.wr)

    partial = setup.store.get_approval(approval.approval_id)
    assert partial.status == "pushed" and "creating the draft PR failed" in partial.last_error
    assert git(setup.bare, "rev-parse", "refs/heads/cycle-runner/WR-000001") == setup.sha
    monkeypatch.setattr(_Pusher, "push", lambda *a, **kw: pytest.fail("pushed again"))

    done = setup.delivery().deliver(setup.wr)

    assert done.status == "pr_created" and done.last_error is None and len(setup.github.pulls) == 1


def test_a_remote_branch_at_another_commit_stops_delivery(setup):
    _approve(setup)
    subprocess.run(["git", "-C", str(setup.bare), "branch", "cycle-runner/WR-000001", "main"], check=True)
    with pytest.raises(GitHubDeliveryError, match="already exists on GitHub at"):
        setup.delivery().deliver(setup.wr)
    assert git(setup.bare, "rev-parse", "cycle-runner/WR-000001") == git(setup.bare, "rev-parse", "main")


def test_a_base_that_isnt_on_github_stops_delivery(setup):
    # The local repository's main is ahead of GitHub's: pushing would publish unrelated commits.
    _approve(setup)
    fresh = make_repo(setup.bare.parent / "other", {"UNRELATED.md": "other history\n"})  # unrelated main
    subprocess.run(["git", "-C", str(fresh.path), "push", "-q", str(setup.bare), "main:refs/heads/main", "--force"],
                   check=True)
    with pytest.raises(GitHubDeliveryError, match="isn't on .* main; nothing pushed"):
        setup.delivery().deliver(setup.wr)
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == ""


def test_a_changed_repository_configuration_stops_delivery(setup, tmp_path, monkeypatch):
    _approve(setup)
    path = tmp_path / "moved.toml"
    path.write_text((tmp_path / "projects.toml").read_text().replace(REPO, "someone-else/fork"))
    monkeypatch.setenv("CYCLE_RUNNER_PROJECTS", str(path))
    with pytest.raises(GitHubDeliveryError, match="no longer delivers DEMO to"):
        setup.delivery().deliver(setup.wr)
    assert git(setup.bare, "branch", "--list", "cycle-runner/*") == ""


# --- the credential ---------------------------------------------------------------------------


def test_the_token_is_never_written_anywhere(setup, tmp_path):
    _approve(setup)
    setup.delivery().deliver(setup.wr)
    for root in (setup.workspace.parent, setup.bare):
        for path in root.rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes(), path
    assert TOKEN not in setup.github.pulls[0]["body"]
    assert "TOKEN" not in repr(GitHubApi(REPO, TOKEN))


def test_the_token_reaches_git_only_through_the_environment(setup, monkeypatch):
    _approve(setup)
    seen = []
    real_run = subprocess.run

    def recording(args, **kwargs):
        if args[0] == "git" and "push" in args:
            seen.append((args, kwargs.get("env", {})))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    setup.delivery().deliver(setup.wr)

    ((args, env),) = seen
    assert not [a for a in args if TOKEN in a or "extraheader" in a.lower()]  # not in argv (ps)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert args[-1] == f"{setup.sha}:refs/heads/cycle-runner/WR-000001"  # the one refspec
    assert "--force" not in args and "--tags" not in args and "--all" not in args and "--mirror" not in args


def test_the_coding_agent_never_gets_the_github_token():
    from cycle_runner.claude_executor import build_options

    environ = {"HOME": "/Users/x", "PATH": "/usr/bin", "CYCLE_RUNNER_GITHUB_TOKEN": TOKEN,
               "CLAUDE_CODE_OAUTH_TOKEN": "c"}
    options = build_options(ExecutionWorkspace(path=__import__("pathlib").Path("/tmp/ws"), test_command="t"),
                            model="m", max_turns=1, max_budget_usd=0.1, environ=environ)
    settings = json.loads(options.settings)
    assert options.env["CYCLE_RUNNER_GITHUB_TOKEN"] == ""
    assert {"name": "CYCLE_RUNNER_GITHUB_TOKEN", "mode": "deny"} in settings["sandbox"]["credentials"]["envVars"]


def test_the_agent_run_refuses_to_start_with_the_github_token_present(setup, monkeypatch, capsys):
    monkeypatch.setenv("CYCLE_RUNNER_GITHUB_TOKEN", TOKEN)
    assert executor_module.main(["run", "--request", "WR-000001", "--executor", "claude"]) == 2
    assert "run the coding agent without the GitHub credential" in capsys.readouterr().err


# --- the CLI ----------------------------------------------------------------------------------


def test_the_cli_review_approve_and_deliver(setup, capsys, monkeypatch):
    assert executor_module.main(["review", setup.wr]) == 0
    out = capsys.readouterr().out
    for line in ("issue:     SB-640 (Add a greet(name) function)", f"project:   DEMO -> GitHub {REPO}",
                 "verified:  ok", "delivery:  review_pending", f"approve {setup.wr} --commit {setup.sha[:12]}"):
        assert line in out, line

    assert executor_module.main(["approve", setup.wr, "--commit", setup.sha[:12]]) == 0
    assert f"approved {setup.sha}" in capsys.readouterr().out

    monkeypatch.delenv("CYCLE_RUNNER_GITHUB_TOKEN", raising=False)
    assert executor_module.main(["deliver", setup.wr]) == 1
    assert "CYCLE_RUNNER_GITHUB_TOKEN isn't set" in capsys.readouterr().err

    assert executor_module.main(["review", setup.wr]) == 0
    out = capsys.readouterr().out
    assert "delivery:  approved" in out and f"APR-000001 approved for {setup.sha[:12]}" in out


def test_pr_body_claims_no_tests_it_didnt_see(setup):
    approval = _approve(setup)
    evidence = {**approval.evidence, "tests": {"observed_runs": 0, "last_result": None}}
    body = pr_body(approval.model_copy(update={"evidence": evidence}))
    assert "No test run was observed" in body and "passed" not in body.split("### Tests")[1].split("###")[0]


# --- found in review ---------------------------------------------------------------------------


def test_a_rejected_commit_can_never_be_approved_afterwards(setup):
    reject(setup.store, setup.wr, commit=setup.sha[:12], rejected_by="t", reason="no")
    with pytest.raises(ApprovalRefused, match="was rejected"):
        _approve(setup)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="not an approvable delivery"):
        with sqlite3.connect(setup.store.path) as db:
            db.execute("INSERT INTO delivery_approvals (work_request_id, commit_sha, branch, base_sha, project_id,"
                       " repository, evidence, approved_by, approved_at, status) VALUES (?, ?, ?, ?, 'DEMO', ?, '{}',"
                       " 'x', 'now', 'approved')", (setup.wr, setup.sha, "cycle-runner/WR-000001", "c" * 40, REPO))
    assert review(setup.store, setup.config, setup.wr).state == "rejected"


@pytest.mark.parametrize("name", ["docs/a` @victim Fixes #1 `.md", "notes\n\nFixes #3 <img src=x>\n.md"])
def test_file_names_that_would_break_out_of_the_pr_body_are_refused(setup, name):
    from cycle_runner.git_delivery import protected_reason

    assert protected_reason(name) == "a name with control characters or backticks"
    wr, sha = _manual_delivery(setup, {name: "x\n"})
    with pytest.raises(ApprovalRefused, match="control characters or backticks"):
        approve(setup.store, setup.config, wr, commit=sha[:12], approved_by="t")


def test_test_output_and_urls_cant_inject_into_the_pr_body(setup):
    approval = _approve(setup)
    evidence = {**approval.evidence,
                "tests": {"observed_runs": 1, "last_result": "1 passed `@x` <b>bold</b>\nFixes #4"},
                "summary": "Closes https://github.com/o/r/issues/5 and fixes o/r#6 cc @admin"}
    body = pr_body(approval.model_copy(update={"evidence": evidence}))
    tests = body.split("### Tests")[1].split("###")[0]
    assert "`@x`" not in tests and "<b>" not in tests and "\nFixes" not in tests
    assert "Closes https://" not in body and "fixes o/r#6" not in body and "@admin" not in body


@pytest.mark.parametrize("oddity", ["replace", "grafts", "alternates", "shallow"])
def test_history_rewriting_in_the_workspace_refuses_approval(setup, tmp_path, oddity):
    git_dir = setup.workspace / ".git"
    if oddity == "replace":  # found in review: git showed one tree while another was pushed
        other = git(setup.workspace, "commit-tree", "-p", f"{setup.sha}^", "-m", "benign",
                    git(setup.workspace, "rev-parse", f"{setup.sha}^^{{tree}}"))
        git(setup.workspace, "replace", setup.sha, other)
    elif oddity == "grafts":
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "grafts").write_text(f"{setup.sha}\n")
    elif oddity == "alternates":
        (git_dir / "objects" / "info" / "alternates").write_text(str(tmp_path) + "\n")
    else:
        (git_dir / "shallow").write_text(git(setup.workspace, "rev-parse", "main") + "\n")
    with pytest.raises(ApprovalRefused, match="approval refused"):
        _approve(setup)


def test_workspace_config_changed_between_verify_and_push_cant_redirect_the_push(setup, tmp_path, monkeypatch):
    # Found in review: the push re-read the workspace's .git/config after it was audited.
    _approve(setup)
    evil = tmp_path / "evil.git"
    subprocess.run(["git", "init", "-q", "--bare", str(evil)], check=True)
    real_contains = GitHubApi.contains

    def race(self, *args):
        git(setup.workspace, "config", f"url.{evil}.pushInsteadOf", str(setup.bare))
        return real_contains(self, *args)

    monkeypatch.setattr(GitHubApi, "contains", race)
    assert setup.delivery().deliver(setup.wr).status == "pr_created"
    assert git(setup.bare, "rev-parse", "refs/heads/cycle-runner/WR-000001") == setup.sha
    assert git(evil, "for-each-ref") == ""  # nothing went to the redirected place


@pytest.mark.parametrize(("change", "problem"), [
    ({"state": "closed"}, "is closed"), ({"merged_at": "2026-01-01T00:00:00Z", "state": "closed"}, "is merged"),
    ({"draft": False}, "not a draft"), ({"base": {"ref": "release"}}, "against release"),
])
def test_an_existing_pr_is_reused_only_if_open_draft_and_against_the_base(setup, change, problem):
    _approve(setup)
    subprocess.run(["git", "-C", str(setup.workspace), "push", "-q", str(setup.bare),
                    f"{setup.sha}:refs/heads/cycle-runner/WR-000001"], check=True)
    setup.github.pulls.append({"number": 9, "html_url": f"https://github.com/{REPO}/pull/9", "draft": True,
                               "state": "open", "base": {"ref": "main"},
                               "head": {"ref": "cycle-runner/WR-000001", "sha": setup.sha}, **change})
    with pytest.raises(GitHubDeliveryError, match=problem):
        setup.delivery().deliver(setup.wr)
    assert setup.store.live_approval(setup.wr).status == "pushed"  # not marked delivered


def test_a_pr_github_created_as_ready_is_never_accepted_on_retry(setup, monkeypatch):
    _approve(setup)
    real = setup.github.handle

    def ready(request):
        response = real(request)
        if request.method == "POST":
            setup.github.pulls[-1]["draft"] = False
            return httpx.Response(201, json=setup.github.pulls[-1])
        return response

    monkeypatch.setattr(setup.github, "handle", ready)
    with pytest.raises(GitHubDeliveryError, match="not a draft"):
        setup.delivery().deliver(setup.wr)
    with pytest.raises(GitHubDeliveryError, match="not a draft"):
        setup.delivery().deliver(setup.wr)
    assert setup.store.live_approval(setup.wr).status == "pushed"
