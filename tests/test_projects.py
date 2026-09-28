"""Project configuration and workspace resolution. Disposable repositories only."""

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import FixedWorkspace
from cycle_runner import claude_executor
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
        ('workspace_root = "/tmp/w"\n[projects.MT]\nrepository = "/r/mt"\ntest_command = "pytest"\nbranch = "x"\n', "Extra inputs"),
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

    def execute(self, request, workspace):
        from cycle_runner.executor import ExecutionResult

        self.calls.append((request.work_request_id, workspace))
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
