"""Local git delivery, in disposable repositories only. Nothing here has a remote or touches the network."""

import os
import subprocess
from datetime import UTC, datetime

import pytest

from conftest import FixedWorkspace
from cycle_runner.executor import DeliveryError, ExecutionResult, details_path, run_next, run_request
from cycle_runner.git_delivery import LocalGitDelivery, branch_name, commit_message, protected_reason
from cycle_runner.work_requests import WorkRequestStore
from disposable_repo import make_repo

TITLE = "Add a greet(name) function"


@pytest.fixture
def store(work_request_db):
    return WorkRequestStore(work_request_db)


@pytest.fixture
def workspace(tmp_path):
    return make_repo(tmp_path / "workspaces" / "WR-000001")


def _request(store, issue_id="SB-640", title=TITLE):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{issue_id}-{title[:10]}", issue_id=issue_id, approved_by="telegram:1",
        approved_at=datetime.now(UTC), cycle_number=10, title_at_approval=title, rationale="r", project_id="DEMO",
    )
    return request


def _changed(*files):
    return ExecutionResult(outcome="changed", message="Did the work.", files_changed=sorted(files))


def git(ws, *args):
    return subprocess.run(["git", "-C", str(ws.path), *args], capture_output=True, text=True).stdout


def _write(ws, path, content="x\n"):
    file = ws.path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)


def _branches(ws):
    return git(ws, "branch", "--format=%(refname:short)").split()


def _the_agents_change(ws):
    _write(ws, "src/hello.py", (ws.path / "src" / "hello.py").read_text() + "\n\ndef greet(name):\n    return f'Hello, {name}!'\n")
    _write(ws, "tests/test_greet.py", "from src.hello import greet\n\nassert greet('Ada') == 'Hello, Ada!'\n")
    return _changed("src/hello.py", "tests/test_greet.py")


# --- a successful change becomes a local branch and commit ------------------------------


def test_a_changed_result_becomes_a_local_branch_and_commit(store, workspace):
    request = _request(store)
    base = git(workspace, "rev-parse", "HEAD").strip()

    delivery = LocalGitDelivery(environ={}).deliver(request, workspace, _the_agents_change(workspace))

    assert delivery.branch == "cycle-runner/WR-000001"
    assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() == "cycle-runner/WR-000001"
    assert git(workspace, "rev-parse", "HEAD").strip() == delivery.commit_sha
    assert git(workspace, "rev-parse", "HEAD^").strip() == base  # exactly one commit on top of the clone
    assert git(workspace, "log", "-1", "--format=%an <%ae>|%cn").strip() == \
        "Cycle Runner <cycle-runner@localhost.invalid>|Cycle Runner"
    assert git(workspace, "log", "-1", "--format=%s").strip() == f"SB-640: {TITLE}"
    assert sorted(git(workspace, "show", "--name-only", "--format=", "HEAD").split()) == \
        ["src/hello.py", "tests/test_greet.py"]
    assert git(workspace, "status", "--porcelain") == ""  # nothing else was lying around
    assert git(workspace, "remote") == ""


def test_the_diff_summary_comes_from_git(store, workspace):
    (workspace.path / "README.md").unlink()
    result = _the_agents_change(workspace).model_copy(update={"files_changed": ["README.md", "src/hello.py",
                                                                                 "tests/test_greet.py"]})

    diff = LocalGitDelivery(environ={}).deliver(_request(store), workspace, result).diff

    assert diff["files_changed"] == ["src/hello.py"]
    assert diff["files_added"] == ["tests/test_greet.py"]
    assert diff["files_deleted"] == ["README.md"]
    assert diff["insertions"] == 4 + 3 and diff["deletions"] == 3
    assert {f["path"]: (f["status"], f["insertions"], f["deletions"]) for f in diff["files"]} == {
        "README.md": ("deleted", 0, 3), "src/hello.py": ("modified", 4, 0), "tests/test_greet.py": ("added", 3, 0),
    }


# --- only intended files ------------------------------------------------------------------


def test_only_files_the_executor_attributed_to_the_agent_are_committed(store, workspace):
    result = _the_agents_change(workspace)
    _write(workspace, "notes.txt", "a test byproduct\n")  # untracked, not attributed
    _write(workspace, "src/generated.py", "# written by a test run\n")

    delivery = LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)

    committed = git(workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == ["src/hello.py", "tests/test_greet.py"]
    assert git(workspace, "status", "--porcelain", "--untracked-files=all").split() == \
        ["??", "notes.txt", "??", "src/generated.py"]  # left in the workspace, uncommitted
    assert {item.split(":")[0] for item in delivery.left_uncommitted} == {"notes.txt", "src/generated.py"}


@pytest.mark.parametrize(
    "path",
    [
        ".env", ".env.local", "backend/.env.prod", "config/credentials.json", "secrets.yaml", "deploy/id_rsa",
        "certs/server.pem", "certs/server.key", ".netrc", ".npmrc", "backend/match-scraper-token.txt",
        ".venv/lib/python3.13/site-packages/x.py", "backend/.venv/bin/activate", ".python/bin/python3",
        "node_modules/x/index.js", "src/__pycache__/hello.cpython-313.pyc", ".pytest_cache/v/cache/nodeids",
        ".claude/projects/x.jsonl", "WR-000001.json", "logs/pytest.log", "data/app.db", ".coverage",
    ],
)
def test_sensitive_and_generated_files_never_enter_a_commit(store, workspace, path):
    result = _the_agents_change(workspace)
    _write(workspace, path, "SOMETHING=1\n")
    result = result.model_copy(update={"files_changed": sorted([*result.files_changed, path])})  # even if attributed

    delivery = LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)

    assert path not in git(workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert protected_reason(path) is not None
    assert any(item.startswith(f"{path}: ") for item in delivery.left_uncommitted) or \
        git(workspace, "check-ignore", path).strip() == path


@pytest.mark.parametrize(
    "content",
    [
        "TOKEN = 'ghp_" + "a" * 36 + "'\n",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
        "key = 'sk-ant-" + "b" * 40 + "'\n",
        "AWS = 'AKIA" + "C" * 16 + "'\n",
    ],
)
def test_a_file_that_looks_like_it_holds_a_credential_refuses_the_whole_delivery(store, workspace, content):
    result = _the_agents_change(workspace)
    _write(workspace, "src/config.py", content)
    result = result.model_copy(update={"files_changed": [*result.files_changed, "src/config.py"]})

    with pytest.raises(DeliveryError, match="src/config.py looks like it contains a credential"):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)

    assert _branches(workspace) == ["main"] and git(workspace, "log", "--oneline").count("\n") == 1


def test_the_runners_own_credentials_are_recognised(store, workspace):
    # Not a known token shape, but the value of one of the runner's credential variables.
    result = _the_agents_change(workspace)
    _write(workspace, "src/hello.py", "SECRET = 'correct-horse-battery-staple-42'\n")
    with pytest.raises(DeliveryError, match="looks like it contains a credential"):
        LocalGitDelivery(environ={"LINEAR_CLIENT_SECRET": "correct-horse-battery-staple-42"}).deliver(
            _request(store), workspace, result)
    assert _branches(workspace) == ["main"]


@pytest.mark.parametrize("kind", ["symlink", "binary", "huge"])
def test_symlinks_binaries_and_huge_files_are_left_out(store, workspace, tmp_path, kind):
    result = _the_agents_change(workspace)
    target = workspace.path / "src" / "extra"
    if kind == "symlink":
        (tmp_path / "outside.txt").write_text("outside\n")
        target.symlink_to(tmp_path / "outside.txt")
    elif kind == "binary":
        target.write_bytes(b"\x00\x01binary")
    else:
        target.write_text("x" * 1_000_001)
    result = result.model_copy(update={"files_changed": [*result.files_changed, "src/extra"]})

    delivery = LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)

    assert "src/extra" not in git(workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert any(item.startswith("src/extra: ") for item in delivery.left_uncommitted)


def test_an_attributed_file_the_project_ignores_is_reported_not_committed(store, workspace):
    _write(workspace, ".gitignore", "build/\n")
    subprocess.run(["git", "-C", str(workspace.path), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(workspace.path), "commit", "-qm", "ignore build"], check=True)
    result = _the_agents_change(workspace)
    _write(workspace, "build/out.py", "x = 1\n")
    result = result.model_copy(update={"files_changed": [*result.files_changed, "build/out.py"]})

    delivery = LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)

    assert "build/out.py: ignored by the project's .gitignore" in delivery.left_uncommitted


@pytest.mark.parametrize("files", [[], [".env"], ["notes.txt"]])
def test_nothing_committable_refuses_and_leaves_no_branch(store, workspace, files):
    for path in files:
        _write(workspace, path)
    with pytest.raises(DeliveryError, match="nothing committable"):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, _changed(*[f for f in files if f != "notes.txt"]))
    assert _branches(workspace) == ["main"]


# --- git safety ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (["remote", "add", "origin", "https://example.invalid/repo.git"], "has a remote"),
        (["config", "url.https://evil.invalid/.insteadOf", "https://github.com/"], "unexpected git configuration"),
        (["config", "core.hooksPath", "/tmp/hooks"], "unexpected git configuration"),
        (["config", "core.fsmonitor", "/tmp/monitor.sh"], "unexpected git configuration"),
        (["config", "include.path", "/tmp/other.gitconfig"], "unexpected git configuration"),
        (["config", "filter.lfs.clean", "cat"], "unexpected git configuration"),
    ],
)
def test_a_workspace_with_a_remote_or_unexpected_git_config_is_refused(store, workspace, setup, problem):
    subprocess.run(["git", "-C", str(workspace.path), *setup], check=True)
    with pytest.raises(DeliveryError, match=problem):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))
    assert _branches(workspace) == ["main"]


def test_hooks_in_the_clone_never_run(store, workspace, tmp_path):
    marker = tmp_path / "hook-ran"
    for hook in ("pre-commit", "commit-msg", "post-commit", "prepare-commit-msg", "reference-transaction"):
        path = workspace.path / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {marker}\n")
        path.chmod(0o755)

    LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))

    assert not marker.exists()


def test_the_users_global_git_config_is_not_used(store, workspace, tmp_path, monkeypatch):
    # e.g. a global hooks path or signing: none of it applies to Cycle Runner's commits.
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "pre-commit").chmod(0o755)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgSign = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))

    assert not marker.exists()


def test_an_existing_branch_is_never_reused(store, workspace):
    subprocess.run(["git", "-C", str(workspace.path), "branch", "cycle-runner/WR-000001"], check=True)
    with pytest.raises(DeliveryError, match="already exists"):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))


def test_a_staged_index_means_something_else_touched_git(store, workspace):
    result = _the_agents_change(workspace)
    subprocess.run(["git", "-C", str(workspace.path), "add", "src/hello.py"], check=True)
    with pytest.raises(DeliveryError, match="index was changed outside Cycle Runner"):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, result)


def test_a_symlinked_git_directory_is_refused(store, tmp_path):
    real = make_repo(tmp_path / "real")
    workspace = make_repo(tmp_path / "workspaces" / "WR-000001")
    subprocess.run(["rm", "-rf", str(workspace.path / ".git")], check=True)
    (workspace.path / ".git").symlink_to(real.path / ".git")
    with pytest.raises(DeliveryError, match="not a plain directory"):
        LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))


# --- names and messages ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["WR-1", "WR-000001/../../x", "../WR-000001", "WR-000001 ", "main", "-WR-000001"])
def test_branch_names_come_only_from_work_request_ids(bad):
    with pytest.raises(DeliveryError, match="not a work request id"):
        branch_name(bad)


def test_the_commit_message_is_bounded_and_single_line(store):
    request = _request(store, title="Fix it\n\nrm -rf / ; $(curl evil)\x1b[31m " + "long " * 40)
    subject, blank, *body = commit_message(request).split("\n")

    assert subject.startswith("SB-640: Fix it rm -rf / ; $(curl evil)")
    assert len(subject) <= 72 and subject.endswith("…") and "\x1b" not in subject
    assert blank == "" and body[0] == f"Work request {request.work_request_id}, delivered by Cycle Runner."


def test_an_unusual_issue_id_is_not_put_in_the_subject(store):
    assert commit_message(_request(store, issue_id="sb-640; rm")).startswith("Cycle Runner: ")


# --- the runner -------------------------------------------------------------------------------


class Agent:
    """An executor that makes a real change in the workspace, like the coding agent would."""

    name = "agent"

    def __init__(self, outcome="changed", change=True):
        self.outcome, self.change = outcome, change

    def execute(self, task, workspace):
        if self.change:
            _write(workspace, "src/hello.py", "def greet(name):\n    return name\n")
        files = ["src/hello.py"] if self.change and self.outcome == "changed" else []
        return ExecutionResult(outcome=self.outcome, message="Agent message.", files_changed=files)


def test_the_runner_delivers_a_changed_result_and_records_it(store, workspace):
    request = _request(store)

    done = run_next(store, Agent(), FixedWorkspace(workspace), deliverer=LocalGitDelivery(environ={}))

    assert (done.status, done.outcome, done.branch) == ("completed", "changed", "cycle-runner/WR-000001")
    assert done.commit_sha == git(workspace, "rev-parse", "HEAD").strip()
    assert f"Committed {done.commit_sha[:12]} on local branch cycle-runner/WR-000001" in done.result_message
    assert "not pushed, awaiting human review" in done.result_message
    import json

    record = json.loads(details_path(workspace).read_text())
    assert record["delivery"]["commit"] == done.commit_sha and record["delivery"]["review"].startswith("pending")
    assert record["delivery"]["diff"]["files_changed"] == ["src/hello.py"]
    assert git(workspace, "remote") == ""
    assert request.work_request_id == done.work_request_id


@pytest.mark.parametrize(("outcome", "status"), [("no_change", "completed"), ("failed", "failed")])
def test_no_change_and_failure_never_touch_git(store, workspace, outcome, status):
    _request(store)
    head = git(workspace, "rev-parse", "HEAD")

    class Refusing(LocalGitDelivery):
        def deliver(self, *args):
            raise AssertionError("delivery must not run")

    done = run_next(store, Agent(outcome=outcome, change=(outcome == "failed")), FixedWorkspace(workspace),
                    deliverer=Refusing(environ={}))

    assert (done.status, done.outcome, done.branch, done.commit_sha) == (status, outcome, None, None)
    assert _branches(workspace) == ["main"] and git(workspace, "rev-parse", "HEAD") == head
    assert git(workspace, "remote") == ""


def test_a_refused_delivery_is_a_failed_run_with_nothing_committed(store, workspace):
    _request(store)
    _write(workspace, "src/hello.py", "API = 'ghp_" + "z" * 36 + "'\n")

    class Leaky(Agent):
        def execute(self, task, ws):
            return ExecutionResult(outcome="changed", message="Agent message.", files_changed=["src/hello.py"])

    done = run_next(store, Leaky(), FixedWorkspace(workspace), deliverer=LocalGitDelivery(environ={}))

    assert (done.status, done.outcome, done.commit_sha) == ("failed", "failed", None)
    assert done.result_message.startswith("Local delivery refused, nothing committed: src/hello.py looks like")
    assert _branches(workspace) == ["main"] and git(workspace, "log", "--oneline").count("\n") == 1


def test_without_a_deliverer_a_change_stays_uncommitted(store, workspace):
    _request(store)
    done = run_next(store, Agent(), FixedWorkspace(workspace))
    assert (done.outcome, done.commit_sha) == ("changed", None)
    assert _branches(workspace) == ["main"] and "src/hello.py" in git(workspace, "status", "--porcelain")


def test_run_request_passes_the_deliverer(store, workspace):
    request = _request(store)
    done, executed = run_request(store, Agent(), FixedWorkspace(workspace), request.work_request_id,
                                 deliverer=LocalGitDelivery(environ={}))
    assert executed and done.commit_sha


def test_no_git_process_touches_the_network(store, workspace, monkeypatch):
    # Every git call carries protocol.allow=never: even a stray fetch couldn't connect.
    calls = []
    real_run = subprocess.run

    def recording(args, **kwargs):
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    LocalGitDelivery(environ={}).deliver(_request(store), workspace, _the_agents_change(workspace))

    git_calls = [c for c in calls if c[0] == "git"]
    assert git_calls and all("protocol.allow=never" in c for c in git_calls)
    assert not [c for c in git_calls if {"push", "fetch", "pull", "clone"} & set(c)]
    assert os.environ.get("GIT_CONFIG_GLOBAL") != os.devnull  # the process environment isn't changed


# --- the human review point ---------------------------------------------------------------


def test_review_shows_what_a_delivered_run_left_for_a_human(store, projects_config, capsys):
    from cycle_runner import executor as executor_module
    from cycle_runner.projects import WorkspaceResolver, load_projects

    request = _request(store)
    done = run_next(store, Agent(), WorkspaceResolver(load_projects()), deliverer=LocalGitDelivery(environ={}))
    capsys.readouterr()

    assert executor_module.main(["review", request.work_request_id]) == 0

    out = capsys.readouterr().out
    workspace = load_projects().workspace_root / request.work_request_id
    assert f"{request.work_request_id} SB-640 completed (changed)" in out
    assert f"workspace: {workspace}" in out
    assert "branch:    cycle-runner/WR-000001 (local only, no remote)" in out
    assert f"commit:    {done.commit_sha}" in out
    diff = f"+{done_diff(workspace)}"
    assert f"diff:      1 files, {diff}; added [], changed ['src/hello.py'], deleted []" in out
    assert "review:    pending. Nothing has been pushed; no PR exists." in out
    origin = load_projects().projects["DEMO"].repository
    assert "cycle-runner/" not in subprocess.run(["git", "-C", str(origin), "branch"], capture_output=True,
                                                 text=True).stdout  # the origin never saw the branch


def done_diff(workspace):
    """+insertions -deletions of the last commit, as git itself counts them."""
    numstat = subprocess.run(["git", "-C", str(workspace), "show", "--numstat", "--format=", "HEAD"],
                             capture_output=True, text=True).stdout.split()
    return f"{numstat[0]} -{numstat[1]}"


def test_review_of_a_no_change_run_has_no_delivery(store, projects_config, capsys):
    from cycle_runner import executor as executor_module

    request = _request(store)
    executor_module.main(["run", "--request", request.work_request_id])  # the fake executor: no_change
    capsys.readouterr()

    assert executor_module.main(["review", request.work_request_id]) == 0

    out = capsys.readouterr().out
    assert "completed (no_change)" in out and "outcome:   no_change" in out
    assert "branch:" not in out and "commit:" not in out
