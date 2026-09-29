"""The executor runner, FakeExecutor, crashes, restarts and the CLI.

No ADK, Ollama, Telegram or Linear: several tests prove it by running the
executor in a process where importing them fails.
"""

import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime

import pytest

from cycle_runner import executor as executor_module
from cycle_runner.executor import ExecutionResult, ExecutionTask, run_next, run_request, task_from_approval
from cycle_runner.fake_executor import FakeExecutor
from cycle_runner.work_requests import InvalidTransition, WorkRequestStore

BLOCKED = ["google.adk", "google.genai", "telegram", "httpx", "litellm", "cycle_runner.linear_client",
           "cycle_runner.agent", "cycle_runner.gateway", "cycle_runner.telegram_adapter"]


@pytest.fixture
def store(work_request_db):
    return WorkRequestStore(work_request_db)


@pytest.fixture
def resolver(fixed_workspace):
    return fixed_workspace


def _approve(store, n=1, issue_id="SB-640", project_id="DEMO"):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{n}", issue_id=issue_id, approved_by="telegram:1",
        approved_at=datetime.now(UTC), cycle_number=10, title_at_approval="t", rationale="r",
        project_id=project_id,
    )
    return request.work_request_id


class Spy:
    """An Executor that records what it was given."""

    name = "spy"

    def __init__(self, outcome="changed", error=None):
        self.received, self.workspaces = [], []
        self.outcome, self.error = outcome, error

    def execute(self, task, workspace):
        self.received.append(task)
        self.workspaces.append(workspace)
        if self.error:
            raise self.error
        return ExecutionResult(outcome=self.outcome, message=f"spy {self.outcome}")


# --- the lifecycle through the runner -----------------------------------------


def test_a_pending_request_runs_to_completed(store, resolver):
    wr = _approve(store)

    done = run_next(store, FakeExecutor(), resolver)

    assert (done.work_request_id, done.status) == (wr, "completed")
    assert done.result_message == (
        f"Fake execution completed for {wr} (SB-640). No real work was done. [workspace: {resolver.workspace.path}]"
    )
    assert done.claimed_by.startswith("fake@")
    assert done.claimed_at <= done.started_at <= done.finished_at
    assert store.get(wr) == done  # durable


def test_the_executor_receives_the_task_while_the_request_is_running(store, resolver):
    wr = _approve(store)
    statuses = []

    class Watching(Spy):
        def execute(self, task, workspace):
            statuses.append(store.get(task.work_request_id))
            return super().execute(task, workspace)

    spy = Watching()
    run_next(store, spy, resolver)

    (received,) = spy.received
    assert received == ExecutionTask(
        work_request_id=wr, issue_id="SB-640", project_id="DEMO", title="t", description="", rationale="r"
    )
    (during,) = statuses
    assert during.status == "running" and during.claimed_by.startswith("spy@")


def test_no_pending_requests_is_handled_cleanly(store, resolver):
    spy = Spy()
    assert run_next(store, spy, resolver) is None
    assert spy.received == []


def test_a_completed_request_never_executes_again(store, resolver):
    wr = _approve(store)
    spy = Spy()

    first, executed_first = run_request(store, spy, resolver, wr)
    second, executed_second = run_request(store, spy, resolver, wr)

    assert (executed_first, executed_second) == (True, False)
    assert len(spy.received) == 1  # the second call executed nothing
    assert second == first and second.status == "completed"
    assert run_next(store, spy, resolver) is None


@pytest.mark.parametrize("status", ["claimed", "running", "failed"])
def test_a_request_that_is_not_pending_is_left_alone(store, status, resolver):
    wr = _approve(store)
    store.claim(wr, "someone-else")
    if status in ("running", "failed"):
        store.start(wr)
    if status == "failed":
        store.finish(wr, "failed", "boom")
    spy = Spy()

    request, executed = run_request(store, spy, resolver, wr)

    assert executed is False and request.status == status and spy.received == []


def test_running_an_unknown_request_is_an_error(store, resolver):
    with pytest.raises(InvalidTransition, match="does not exist"):
        run_request(store, Spy(), resolver, "WR-000042")


def test_an_executor_that_raises_is_recorded_as_failed(store, resolver):
    wr = _approve(store)

    failed = run_next(store, Spy(error=RuntimeError("tests exploded")), resolver)

    assert failed.status == "failed"
    assert failed.result_message.startswith("spy raised RuntimeError: tests exploded [workspace: ")
    assert run_next(store, Spy(), resolver) is None  # a failed request isn't retried


def test_an_executor_can_report_failure(store, resolver):
    _approve(store)
    assert run_next(store, Spy(outcome="failed"), resolver).status == "failed"


# --- FakeExecutor ---------------------------------------------------------------


def test_the_fake_executor_is_deterministic(store, fixed_workspace):
    wr = _approve(store)
    request = store.claim(wr, "x")
    workspace = fixed_workspace.workspace

    assert FakeExecutor().execute(task_from_approval(request), workspace) == FakeExecutor().execute(task_from_approval(request), workspace)


def test_the_fake_executor_performs_no_external_operations(store, monkeypatch, fixed_workspace):
    wr = _approve(store)
    request = store.claim(wr, "x")
    workspace = fixed_workspace.workspace

    def forbidden(*args, **kwargs):
        raise AssertionError("external operation attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)

    result = FakeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "no_change"  # it did nothing, successfully


# --- crashes, with real processes ---------------------------------------------


def _process(db_path, code, blocked=()):
    """Run `code` in a fresh Python process against the same database."""
    script = textwrap.dedent(
        f"""
        import os, sys
        for name in {list(blocked)!r}:
            sys.modules[name] = None
        from cycle_runner.work_requests import WorkRequestStore
        store = WorkRequestStore({str(db_path)!r})
        {textwrap.indent(textwrap.dedent(code), "        ").strip()}
        """
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)


def test_crash_before_claim_leaves_the_request_pending(store, work_request_db, resolver):
    wr = _approve(store)

    crashed = _process(work_request_db, "os._exit(1)  # died before claiming anything")

    assert crashed.returncode == 1
    assert store.get(wr).status == "pending"
    assert run_next(store, FakeExecutor(), resolver).status == "completed"  # the next run just picks it up


def test_crash_after_claim_strands_it_claimed_until_released(store, work_request_db, resolver):
    wr = _approve(store)

    crashed = _process(work_request_db, "store.claim_next('fake@crashed:1'); os._exit(1)")

    assert crashed.returncode == 1
    assert store.get(wr).status == "claimed"
    assert run_next(store, FakeExecutor(), resolver) is None  # not picked up again automatically
    store.release(wr)  # manual recovery: safe, the executor never ran
    assert run_next(store, FakeExecutor(), resolver).status == "completed"


def test_crash_while_running_strands_it_running_and_it_is_never_retried(store, work_request_db, resolver):
    wr = _approve(store)
    code = """
        from cycle_runner.executor import run_next
        from cycle_runner.executor import ExecutionWorkspace
        class Fixed:
            def resolve(self, request):
                return ExecutionWorkspace(path=__import__("pathlib").Path("."), test_command="true")
        class DiesMidWork:
            name = "dies"
            def execute(self, task, workspace):
                os._exit(1)  # the process is killed while the work is in progress
        run_next(store, DiesMidWork(), Fixed())
    """

    crashed = _process(work_request_db, code)

    assert crashed.returncode == 1
    assert store.get(wr).status == "running"
    assert run_next(store, FakeExecutor(), resolver) is None
    assert run_request(store, FakeExecutor(), resolver, wr) == (store.get(wr), False)
    abandoned = store.abandon(wr, "executor process died")  # manual recovery: a human decides
    assert abandoned.status == "failed"
    assert run_next(store, FakeExecutor(), resolver) is None  # still never retried


# --- restart and independence ---------------------------------------------------


def test_another_process_sees_the_state_this_process_left(store, work_request_db, projects_config):
    wr = _approve(store)

    runner = _process(
        work_request_db,
        "from cycle_runner.executor import run_next; from cycle_runner.fake_executor import FakeExecutor;"
        " from cycle_runner.projects import WorkspaceResolver, load_projects;"
        f" print(run_next(store, FakeExecutor(), WorkspaceResolver(load_projects({str(projects_config)!r}))).status)",
    )

    assert runner.returncode == 0 and runner.stdout.strip() == "completed"
    reopened = WorkRequestStore(work_request_db).get(wr)  # a fresh store object: only the file is shared
    assert reopened.status == "completed" and reopened.result_message.startswith("Fake execution completed")


def test_the_executor_cli_runs_without_adk_telegram_linear_or_a_model(store, work_request_db, projects_config):
    wr = _approve(store)
    code = f"""
        import runpy
        os.environ["CYCLE_RUNNER_DB"] = {str(work_request_db)!r}
        os.environ["CYCLE_RUNNER_PROJECTS"] = {str(projects_config)!r}
        sys.argv = ["cycle_runner.executor", "run"]
        try:
            runpy.run_module("cycle_runner.executor", run_name="__main__")
        except SystemExit as exit:
            print("exit", exit.code)
        print("loaded:", sorted(m for m in {BLOCKED!r} if sys.modules.get(m) is not None))
    """

    result = _process(work_request_db, code, blocked=BLOCKED)

    assert result.returncode == 0, result.stderr
    assert f"{wr} SB-640 completed" in result.stdout
    assert "exit 0" in result.stdout and "loaded: []" in result.stdout
    assert store.get(wr).status == "completed"


# --- the CLI --------------------------------------------------------------------


def _cli(*args):
    return executor_module.main(list(args))


def test_cli_run_completes_the_next_request(store, capsys, projects_config):
    wr = _approve(store)
    assert _cli("run") == 0
    assert f"{wr} SB-640 completed (no_change) by fake@" in capsys.readouterr().out


def test_cli_with_nothing_pending(store, capsys, projects_config):
    assert _cli() == 0
    assert "No pending work requests." in capsys.readouterr().out


def test_cli_run_request_twice_executes_once(store, capsys, projects_config):
    wr = _approve(store)
    _cli("run", "--request", wr)
    capsys.readouterr()

    assert _cli("run", "--request", wr) == 0
    assert capsys.readouterr().out.strip() == f"{wr} is already completed; nothing executed."


def test_cli_list_release_and_abandon(store, capsys):
    claimed, running = _approve(store, 1), _approve(store, 2)
    store.claim(claimed, "crashed")
    store.claim(running, "crashed")
    store.start(running)

    assert _cli("release", claimed) == 0
    assert _cli("abandon", running, "--reason", "died") == 0
    assert _cli("list") == 0

    out = capsys.readouterr().out
    assert f"{claimed} SB-640 pending" in out
    assert f"{running} SB-640 failed by crashed - Abandoned: died" in out


def test_cli_refuses_invalid_recovery(store, capsys):
    wr = _approve(store)
    assert _cli("abandon", wr) == 2  # pending, not running
    assert f"error: {wr} is pending, not running" in capsys.readouterr().err


def test_cli_claude_needs_a_named_request(store, capsys, projects_config):
    wr = _approve(store)
    assert _cli("run", "--executor", "claude") == 2
    assert "--executor claude needs --request" in capsys.readouterr().err
    assert store.get(wr).status == "pending"  # nothing claimed


def test_cli_claude_without_linear_credentials_claims_nothing(store, capsys, projects_config):
    # conftest removes the Linear credentials for unmarked tests.
    wr = _approve(store)
    assert _cli("run", "--request", wr, "--executor", "claude") == 2
    assert "LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET must be set" in capsys.readouterr().err
    assert store.get(wr).status == "pending"


def test_cli_backfills_a_missing_project_only(store, capsys):
    wr = _approve(store, project_id=None)
    assert _cli("backfill-project", wr, "MT") == 0
    assert f"{wr} SB-640 pending (project MT)" in capsys.readouterr().out
    assert _cli("backfill-project", wr, "TRD") == 2
    assert f"error: {wr} already has project MT" in capsys.readouterr().err


def test_the_runs_details_are_kept_beside_the_workspace(store, tmp_path):
    import json

    from cycle_runner.executor import ExecutionWorkspace, details_path

    workspace = ExecutionWorkspace(path=tmp_path / "workspaces" / "WR-000001", test_command="true")
    workspace.path.mkdir(parents=True)
    wr = _approve(store)

    class Detailed(Spy):
        def execute(self, task, workspace):
            return ExecutionResult(outcome="changed", message="done", details={"cost_usd": 0.5})

    run_next(store, Detailed(), type("R", (), {"resolve": lambda self, r: workspace})())

    record = json.loads(details_path(workspace).read_text())
    assert details_path(workspace) == tmp_path / "workspaces" / "WR-000001.json"  # not inside the clone
    assert record == {"work_request_id": wr, "issue_id": "SB-640", "outcome": "changed", "message": "done",
                      "files_changed": [], "details": {"cost_usd": 0.5}, "workspace": str(workspace.path),
                      "delivery": None}
