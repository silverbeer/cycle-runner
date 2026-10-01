"""V1.5a: MT is configured for GitHub delivery through projects.toml, and nothing else knows it."""

import ast
from pathlib import Path

import pytest

import cycle_runner
from cycle_runner import executor as executor_module
from cycle_runner.executor import TaskContextError
from cycle_runner.issue_context import IssueSummary, LinearIssueSource
from cycle_runner.projects import ProjectConfigError, load_projects
from cycle_runner.work_requests import WorkRequestStore

COMMITTED = Path(__file__).parent.parent / "projects.toml"
PACKAGE = Path(cycle_runner.__file__).parent


def test_mt_delivers_to_silverbeer_missing_table_against_main():
    mt = load_projects(COMMITTED).projects["MT"]
    assert (mt.github, mt.branch) == ("silverbeer/missing-table", "main")
    # V1.2's execution settings are unchanged.
    assert mt.repository == Path("~/gitrepos/missing-table").expanduser()
    assert mt.setup_command.endswith("uv sync --directory backend --frozen")
    assert mt.test_command.startswith("/usr/bin/env -C backend PATH=/usr/bin:/bin .venv/bin/python -m pytest tests/unit")


@pytest.mark.parametrize("project", ["MTA", "TRD", "JT"])
def test_projects_without_github_delivery_are_unchanged(project):
    config = load_projects(COMMITTED).projects[project]
    assert config.github is None  # not deliverable: approval refuses, nothing is pushed


def test_the_repository_comes_from_configuration(tmp_path):
    body = COMMITTED.read_text().replace('github = "silverbeer/missing-table"', 'github = "someone/fork"')
    path = tmp_path / "projects.toml"
    path.write_text(body)
    assert load_projects(path).projects["MT"].github == "someone/fork"


@pytest.mark.parametrize(("github", "problem"), [
    ("missing-table", "owner/name"), ("silverbeer/missing-table/extra", "owner/name"),
    ("https://github.com/silverbeer/missing-table", "owner/name"), ("../evil", "owner/name"),
])
def test_a_malformed_github_repository_is_refused_at_load(tmp_path, github, problem):
    path = tmp_path / "projects.toml"
    path.write_text(COMMITTED.read_text().replace('"silverbeer/missing-table"', f'"{github}"'))
    with pytest.raises(ProjectConfigError, match=problem):
        load_projects(path)


def test_github_without_a_base_branch_is_refused(tmp_path):
    path = tmp_path / "projects.toml"
    path.write_text(COMMITTED.read_text().replace('[projects.MT]    # missing-table\nrepository = "~/gitrepos/missing-table"\nbranch = "main"\n',
                                                  '[projects.MT]\nrepository = "~/gitrepos/missing-table"\n'))
    with pytest.raises(ProjectConfigError, match="github needs branch"):
        load_projects(path)


def test_no_project_or_repository_is_named_in_the_delivery_code():
    named = {"MT", "MTA", "TRD", "JT", "silverbeer", "missing-table", "silverbeer/missing-table",
             "cycle-runner-sandbox", "silverbeer/cycle-runner-sandbox"}
    for module in ("github_delivery.py", "delivery_approval.py", "git_delivery.py", "executor.py"):
        tree = ast.parse((PACKAGE / module).read_text())
        constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not constants & named, (module, constants & named)
        assert "missing-table" not in (PACKAGE / module).read_text(), module


# --- request: a human asks for work on one named issue -------------------------------------


class StubIssues:
    def __init__(self, summary):
        self.summary = summary

    def describe(self, issue_id):
        if self.summary is None:
            raise TaskContextError(f"Linear has no issue {issue_id}")
        return self.summary


@pytest.fixture
def linear(monkeypatch):
    stub = StubIssues(IssueSummary(issue_id="SB-866", title="mt team matches: add --age-group", project_id="MT",
                                   cycle_number=10))
    monkeypatch.setattr(LinearIssueSource, "from_env", classmethod(lambda cls: stub))
    monkeypatch.setenv("CYCLE_RUNNER_PROJECTS", str(COMMITTED))
    return stub


def _cli(*args):
    return executor_module.main(list(args))


def test_request_creates_a_pending_work_request_from_linears_facts(linear, work_request_db, capsys):
    assert _cli("request", "SB-866", "--confirm", "SB-866", "--reason", "Low-risk V1.5a delivery test") == 0

    (request,) = WorkRequestStore(work_request_db).list_all()
    assert (request.issue_id, request.project_id, request.status) == ("SB-866", "MT", "pending")
    assert request.title_at_approval == "mt team matches: add --age-group"  # from Linear, not typed
    assert request.cycle_number == 10 and request.rationale == "Low-risk V1.5a delivery test"
    assert request.approved_by.startswith("cli:") and request.recommendation_id.startswith("cli-SB-866-")
    assert "Nothing started." in capsys.readouterr().out


@pytest.mark.parametrize(("args", "problem"), [
    (("SB-866", "--confirm", "SB-865", "--reason", "x"), "must repeat the issue id"),
    (("sb-866", "--confirm", "sb-866", "--reason", "x"), "must repeat the issue id"),
    (("SB-866", "--confirm", "SB-866", "--reason", "  "), "must say why"),
])
def test_request_needs_an_exact_confirmation_and_a_reason(linear, work_request_db, capsys, args, problem):
    assert _cli("request", *args) == 2
    assert problem in capsys.readouterr().err
    assert WorkRequestStore(work_request_db).list_all() == []


def test_request_refuses_an_issue_with_open_work(linear, work_request_db, capsys):
    _cli("request", "SB-866", "--confirm", "SB-866", "--reason", "first")
    capsys.readouterr()
    assert _cli("request", "SB-866", "--confirm", "SB-866", "--reason", "again") == 2
    assert "already has WR-000001 (pending)" in capsys.readouterr().err
    assert len(WorkRequestStore(work_request_db).list_all()) == 1


@pytest.mark.parametrize("project", [None, "ZZ"])
def test_request_refuses_an_unconfigured_project(linear, work_request_db, capsys, project):
    linear.summary = IssueSummary(issue_id="SB-866", title="t", project_id=project, cycle_number=None)
    assert _cli("request", "SB-866", "--confirm", "SB-866", "--reason", "x") == 2
    assert "isn't configured" in capsys.readouterr().err
    assert WorkRequestStore(work_request_db).list_all() == []


def test_request_refuses_an_unreadable_issue(linear, work_request_db, capsys):
    linear.summary = None
    assert _cli("request", "SB-866", "--confirm", "SB-866", "--reason", "x") == 2
    assert "Linear has no issue SB-866" in capsys.readouterr().err
