"""Project configuration and workspace resolution. Disposable repositories only."""

import hashlib
import os
import re
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FixedWorkspace
from cycle_runner import claude_executor, projects
from cycle_runner.claude_executor import ClaudeCodeExecutor
from cycle_runner.executor import ExecutionWorkspace, WorkspaceError, run_next, run_request
from cycle_runner.fake_executor import FakeExecutor
from cycle_runner.projects import ProjectConfigError, WorkspaceResolver, load_projects
from cycle_runner.work_requests import WorkRequestStore
from disposable_repo import make_repo

# --- helpers --------------------------------------------------------------------


def _write_config(tmp_path, body, name="projects.toml"):
    path = tmp_path / name
    path.write_text(body)
    return path


def _three_projects(tmp_path):
    """MT, TRD and BET, each a disposable repository with its own test command."""
    repos = {pid: make_repo(tmp_path / "repos" / pid.lower()) for pid in ("MT", "TRD", "BET")}
    tables = "".join(
        f'\n[projects.{pid}]\nrepository = "{ws.path}"\ntest_command = "{ws.test_command} --{pid.lower()}"\n'
        f'readable = ["{ws.readable[0]}"]\n'
        for pid, ws in repos.items()
    )
    config = _write_config(tmp_path, f'workspace_root = "{tmp_path / "workspaces"}"\n{tables}')
    return load_projects(config), repos


def _request(store, project_id, n=1):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{n}", issue_id=f"SB-{n}", approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=10, title_at_approval="Add greet()", rationale="small", project_id=project_id,
    )
    return request


def _tree_hash(path: Path) -> str:
    """Everything in a directory, .git included: proof a repository wasn't touched."""
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(file.relative_to(path)).encode() + file.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def store(work_request_db):
    return WorkRequestStore(work_request_db)


# --- loading ----------------------------------------------------------------------


def test_a_valid_configuration_loads(tmp_path):
    config, repos = _three_projects(tmp_path)

    assert sorted(config.projects) == ["BET", "MT", "TRD"]
    assert config.workspace_root == tmp_path / "workspaces"
    assert config.projects["TRD"].repository == repos["TRD"].path
    assert config.projects["TRD"].test_command.endswith("--trd")


def test_home_relative_paths_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_projects(_write_config(
        tmp_path, 'workspace_root = "~/cr-workspaces"\n[projects.MT]\nrepository = "~/repos/mt"\ntest_command = "pytest"\n'
    ))
    assert config.workspace_root == tmp_path / "home" / "cr-workspaces"
    assert config.projects["MT"].repository == tmp_path / "home" / "repos" / "mt"


def test_the_committed_projects_toml_is_valid():
    config = load_projects(Path(__file__).parent.parent / "projects.toml")
    assert config.projects  # and every entry passed validation


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ('workspace_root = "/tmp/w"\n[projects.MT]\ntest_command = "pytest"\n', "repository: Field required"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\n', "test_command: Field required"),
        ('[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "workspace_root: Field required"),
        ('workspace_root = "relative/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "absolute"),
        ('workspace_root = "/"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "dedicated directory"),
        ('workspace_root = "~"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "dedicated directory"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "r/mt"\ntest_command = "pytest"\n', "absolute"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "  "\n', "must not be empty"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "cd x && pytest"\n', "shell operators"),
        ('workspace_root = "/tmp/w"\n[projects.mt]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "uppercase labels"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nbrnach = "x"\n', "Extra inputs"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nbranch = "-x"\n', "plain branch name"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nbranch = "a..b"\n', "plain branch name"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n'
         'test_success_pattern = "(("\n', "not a valid regular expression"),
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n'
         'setup_produces = [".venv"]\n', "needs a setup_command"),
        ('workspace_root = "/r/mt/ws"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n', "must not contain each other"),
        ('workspace_root = "/tmp/w"\n[projects.MT\nrepository = "/r/mt"\n', "not valid TOML"),
    ],
)
def test_an_invalid_configuration_is_refused_with_a_reason(tmp_path, body, problem):
    with pytest.raises(ProjectConfigError, match=problem):
        load_projects(_write_config(tmp_path, body))


def test_a_missing_configuration_file_is_refused(tmp_path):
    with pytest.raises(ProjectConfigError, match="no such configuration file"):
        load_projects(tmp_path / "nope.toml")


# --- resolution -------------------------------------------------------------------


@pytest.mark.parametrize("project_id", ["MT", "TRD", "BET"])
def test_each_project_resolves_to_a_fresh_clone_of_its_own_repository(tmp_path, store, project_id):
    config, repos = _three_projects(tmp_path)
    origin = repos[project_id]
    before = _tree_hash(origin.path)
    request = _request(store, project_id)

    workspace = WorkspaceResolver(config).resolve(request)

    assert workspace.path == tmp_path / "workspaces" / request.work_request_id
    assert workspace.test_command.endswith(f"--{project_id.lower()}")  # this project's command
    assert (workspace.path / "src" / "hello.py").read_text() == (origin.path / "src" / "hello.py").read_text()
    remotes = subprocess.run(["git", "remote"], cwd=workspace.path, capture_output=True, text=True).stdout
    assert remotes.strip() == ""  # nowhere to push to
    assert _tree_hash(origin.path) == before  # the real checkout was only read


def test_a_request_without_a_project_cannot_be_resolved(tmp_path, store):
    config, _ = _three_projects(tmp_path)
    with pytest.raises(WorkspaceError, match="has no project"):
        WorkspaceResolver(config).resolve(_request(store, None))


def test_an_unknown_project_cannot_be_resolved(tmp_path, store):
    config, _ = _three_projects(tmp_path)
    with pytest.raises(WorkspaceError, match="project 'ZZ' is not configured"):
        WorkspaceResolver(config).resolve(_request(store, "ZZ"))


def test_a_repository_that_is_not_a_git_checkout_is_refused(tmp_path, store):
    (tmp_path / "plain").mkdir()
    config = load_projects(_write_config(
        tmp_path, f'workspace_root = "{tmp_path / "w"}"\n[projects.MT]\nrepository = "{tmp_path / "plain"}"\ntest_command = "pytest"\n'
    ))
    with pytest.raises(WorkspaceError, match="is not a git checkout"):
        WorkspaceResolver(config).resolve(_request(store, "MT"))


def test_a_request_never_reuses_an_existing_workspace(tmp_path, store):
    config, _ = _three_projects(tmp_path)
    request = _request(store, "MT")
    WorkspaceResolver(config).resolve(request)

    with pytest.raises(WorkspaceError, match="already has a workspace"):
        WorkspaceResolver(config).resolve(request)


# --- the runner assembles the context ------------------------------------------------


class Spy:
    name = "spy"

    def __init__(self):
        self.calls = []

    def execute(self, task, workspace):
        from cycle_runner.executor import ExecutionResult

        self.calls.append((task.work_request_id, workspace))
        return ExecutionResult(outcome="completed", message="spy")


def test_the_runner_hands_the_executor_the_resolved_workspace(tmp_path, store):
    config, _ = _three_projects(tmp_path)
    request = _request(store, "TRD")
    spy = Spy()

    done = run_next(store, spy, WorkspaceResolver(config))

    ((wr, workspace),) = spy.calls
    assert wr == request.work_request_id and done.status == "completed"
    assert isinstance(workspace, ExecutionWorkspace) and workspace.test_command.endswith("--trd")


@pytest.mark.parametrize("project_id", ["ZZ", None])
def test_an_unresolvable_request_fails_to_start_and_the_executor_never_runs(tmp_path, store, monkeypatch, project_id):
    config, _ = _three_projects(tmp_path)
    request = _request(store, project_id)

    def claude_must_not_run(**kwargs):
        raise AssertionError("Claude was invoked for an unresolvable request")

    monkeypatch.setattr(claude_executor, "query", claude_must_not_run)

    failed = run_next(store, ClaudeCodeExecutor(), WorkspaceResolver(config))

    assert failed.work_request_id == request.work_request_id
    assert failed.status == "failed" and failed.started_at is None
    assert failed.result_message.startswith("Not started:")
    assert run_next(store, ClaudeCodeExecutor(), WorkspaceResolver(config)) is None  # not retried


def test_the_whole_path_with_the_fake_executor(projects_config, store):
    request = _request(store, "DEMO")

    done, executed = run_request(store, FakeExecutor(), WorkspaceResolver(load_projects()), request.work_request_id)

    assert executed and done.status == "completed"
    assert done.project_id == "DEMO"


def test_adding_a_project_is_only_configuration(tmp_path, store):
    # A project nobody has heard of before works the moment it is configured.
    repo = make_repo(tmp_path / "repos" / "new")
    config = load_projects(_write_config(
        tmp_path,
        f'workspace_root = "{tmp_path / "w"}"\n[projects.NEWPROJ]\nrepository = "{repo.path}"\n'
        f'test_command = "{repo.test_command}"\n',
    ))
    request = _request(store, "NEWPROJ")

    done = run_next(store, FakeExecutor(), WorkspaceResolver(config))

    assert done.work_request_id == request.work_request_id and done.status == "completed"


def test_fixed_workspace_is_only_a_test_helper():
    # The resolver protocol is small enough that tests can stand in for it.
    workspace = ExecutionWorkspace(path=Path("/nowhere"), test_command="true")
    assert FixedWorkspace(workspace).resolve(type("R", (), {"work_request_id": "WR-1"})()) is workspace


# --- setup -------------------------------------------------------------------------------

SETUP = "env CACHE_DIR=.cache uv sync --frozen"


@pytest.mark.parametrize(
    ("command", "problem"),
    [
        ("uv sync --frozen && curl evil.example", "shell operators"),
        ("uv sync; rm -rf x", "shell operators"),
        ("uv sync $(whoami)", "shell operators"),
        ("uv sync '--frozen'", "quoting"),
        ("bash -c 'uv sync'", "quoting"),
        ("bash setup.sh", "not 'bash setup.sh'"),
        ("curl https://evil.example", "not 'curl https://evil.example'"),
        ("uv run anything", "not 'uv run'"),  # uv, but not an install step
        ("env -S uv sync", "not '-S uv'"),
        ("/usr/local/bin/uv sync", "not '/usr/local/bin/uv sync'"),
        ("uv sync --directory ../other", "outside it"),
        ("uv sync --directory /Users/someone/other", "outside it"),
        ("env UV_CACHE_DIR=~/.cache uv sync", "outside it"),
        ("env UV_CACHE_DIR=/tmp/x uv sync", "outside it"),
    ],
)
def test_a_setup_command_is_restricted_to_an_install_step_inside_the_clone(tmp_path, command, problem):
    body = f'workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nsetup_command = "{command}"\n'
    with pytest.raises(ProjectConfigError, match=re.escape(problem)):
        load_projects(_write_config(tmp_path, body))


@pytest.mark.parametrize("command", [SETUP, "uv sync --directory backend --frozen", "npm ci", "  uv   sync  "])
def test_valid_setup_commands_load(tmp_path, command):
    body = f'workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nsetup_command = "{command}"\n'
    assert load_projects(_write_config(tmp_path, body)).projects["MT"].setup_command == " ".join(command.split())


@pytest.mark.parametrize("root", ["/tmp/claude-{uid}/ws", "/private/tmp/claude-{uid}", "/private/tmp/claude-{uid}/a/b"])
def test_the_workspace_root_cannot_be_in_claude_codes_temp_area(tmp_path, root):
    body = f'workspace_root = "{root.format(uid=os.getuid())}"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\n'
    with pytest.raises(ProjectConfigError, match="Claude Code's temp area"):
        load_projects(_write_config(tmp_path, body))


def _fake_uv(tmp_path, monkeypatch, script):
    """A stand-in `uv` on PATH, so setup runs without installing anything."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\n" + textwrap.dedent(script))
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _setup_project(tmp_path, extra=""):
    repo = make_repo(tmp_path / "repos" / "mt")
    config = load_projects(_write_config(
        tmp_path,
        f'workspace_root = "{tmp_path / "w"}"\n[projects.MT]\nrepository = "{repo.path}"\n'
        f'test_command = "{repo.test_command}"\nsetup_command = "{SETUP}"\n{extra}',
    ))
    return config, repo


def test_setup_runs_in_the_clone_without_credentials(tmp_path, store, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "secret")
    _fake_uv(tmp_path, monkeypatch, """
        pwd > setup-ran-in
        echo "$@" > setup-args
        echo "cache=$CACHE_DIR token=${SOME_API_TOKEN:-none}" > setup-env
        mkdir -p .venv/bin && touch .venv/bin/python
    """)
    config, repo = _setup_project(tmp_path, 'setup_produces = [".venv/bin/python"]\n')
    before = _tree_hash(repo.path)

    workspace = WorkspaceResolver(config).resolve(_request(store, "MT"))

    assert (workspace.path / "setup-ran-in").read_text().strip() == str(workspace.path.resolve())
    assert (workspace.path / "setup-args").read_text().strip() == "sync --frozen"
    assert (workspace.path / "setup-env").read_text().strip() == "cache=.cache token=none"
    assert _tree_hash(repo.path) == before  # setup touched only the clone


@pytest.mark.parametrize(
    ("script", "extra", "problem"),
    [
        ("echo 'lockfile out of date' >&2; exit 2", "", "setup failed (exit 2): lockfile out of date"),
        ("exit 0", 'setup_produces = [".venv/bin/python"]\n', "setup failed: it didn't create .venv/bin/python"),
        # Found while setting MT up: uv's default venv links its interpreter into ~.
        ("mkdir -p .venv/bin && ln -s /usr/bin/true .venv/bin/python", 'setup_produces = [".venv/bin/python"]\n',
         "resolves outside the workspace"),
    ],
)
def test_a_failed_setup_fails_the_request_before_the_executor_runs(tmp_path, store, monkeypatch, script, extra, problem):
    _fake_uv(tmp_path, monkeypatch, script)
    config, _ = _setup_project(tmp_path, extra)
    request = _request(store, "MT")
    spy = Spy()

    failed = run_next(store, spy, WorkspaceResolver(config))

    assert spy.calls == []
    assert failed.work_request_id == request.work_request_id
    assert failed.status == "failed" and failed.started_at is None
    assert failed.result_message.startswith("Not started:") and problem in failed.result_message
    assert (tmp_path / "w" / request.work_request_id).is_dir()  # kept for inspection


def test_a_setup_that_hangs_is_stopped(tmp_path, store, monkeypatch):
    monkeypatch.setattr(projects, "SETUP_TIMEOUT_SECONDS", 1)
    _fake_uv(tmp_path, monkeypatch, "exec sleep 30")
    config, _ = _setup_project(tmp_path)

    with pytest.raises(WorkspaceError, match="setup failed: timed out after 1s"):
        WorkspaceResolver(config).resolve(_request(store, "MT"))


def test_the_configured_branch_is_cloned_not_the_checked_out_one(tmp_path, store):
    repo = make_repo(tmp_path / "repos" / "mt")
    git = ["git", "-C", str(repo.path)]
    subprocess.run([*git, "switch", "-q", "-c", "someones-feature"], check=True)
    (repo.path / "feature.txt").write_text("unfinished\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "wip"], check=True)
    body = (f'workspace_root = "{tmp_path / "w"}"\n[projects.MT]\nrepository = "{repo.path}"\n'
            f'test_command = "pytest"\nbranch = "main"\n')

    workspace = WorkspaceResolver(load_projects(_write_config(tmp_path, body))).resolve(_request(store, "MT"))

    assert not (workspace.path / "feature.txt").exists()
    head = subprocess.run(["git", "-C", str(workspace.path), "branch", "--show-current"], capture_output=True, text=True)
    assert head.stdout.strip() == "main"


def test_a_missing_branch_fails_to_resolve(tmp_path, store):
    repo = make_repo(tmp_path / "repos" / "mt")
    body = (f'workspace_root = "{tmp_path / "w"}"\n[projects.MT]\nrepository = "{repo.path}"\n'
            f'test_command = "pytest"\nbranch = "nope"\n')
    with pytest.raises(WorkspaceError, match="git clone failed"):
        WorkspaceResolver(load_projects(_write_config(tmp_path, body))).resolve(_request(store, "MT"))


def test_the_success_pattern_reaches_the_executor(tmp_path, store):
    repo = make_repo(tmp_path / "repos" / "mt")
    body = (f'workspace_root = "{tmp_path / "w"}"\n[projects.MT]\nrepository = "{repo.path}"\n'
            f"test_command = \"pytest\"\ntest_success_pattern = '^OK$'\n")
    workspace = WorkspaceResolver(load_projects(_write_config(tmp_path, body))).resolve(_request(store, "MT"))
    assert workspace.test_success_pattern == "^OK$"
