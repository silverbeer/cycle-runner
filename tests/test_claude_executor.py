"""ClaudeCodeExecutor without calling Claude: policy, options, prompt, result mapping.

The SDK's query() is replaced by a fake, so these run in CI with no credentials.
The live-agent tests are in test_claude_live.py (marker `claude`).
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from claude_agent_sdk import CLIConnectionError, ResultMessage

from cycle_runner import claude_executor
from cycle_runner.claude_executor import (
    ClaudeCodeExecutor,
    Workspace,
    build_options,
    build_prompt,
    check_tool_call,
    credential_variables,
    to_execution_result,
)
from cycle_runner.executor import run_request
from cycle_runner.work_requests import WorkRequestStore
from disposable_repo import make_repo


@pytest.fixture
def workspace(tmp_path):
    return make_repo(tmp_path / "repo")


def _request(store, title="Add a greet(name) function to src/hello.py", issue_id="SB-640"):
    request, _ = store.create_for_approval(
        recommendation_id="rec-1", issue_id=issue_id, approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=10, title_at_approval=title, rationale="Small and well understood.",
    )
    return request


def _result(**overrides):
    fields = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=4,
                  session_id="s", total_cost_usd=0.03, permission_denials=[],
                  structured_output={"outcome": "completed", "summary": "Added greet().",
                                     "tests_passed": True, "tests_command": "python -m unittest"})
    return ResultMessage(**{**fields, **overrides})


# --- the policy hook --------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "tool_input", "allowed"),
    [
        ("Read", {"file_path": "src/hello.py"}, True),
        ("Read", {"file_path": "{ws}/src/hello.py"}, True),
        ("Edit", {"file_path": "{ws}/src/hello.py"}, True),
        ("Write", {"file_path": "{ws}/tests/test_greet.py"}, True),
        ("Read", {"file_path": "/etc/hosts"}, False),
        ("Read", {"file_path": "~/.ssh/id_ed25519"}, False),  # found by this test: ~ must be expanded
        ("Glob", {"pattern": "*", "path": "~"}, False),
        ("Read", {"file_path": "{ws}/../outside.txt"}, False),
        ("Write", {"file_path": "/tmp/escape.txt"}, False),
        ("Edit", {"file_path": "{ws}/.git/config"}, False),
        ("Write", {"file_path": "{ws}/.git/hooks/pre-commit"}, False),
        ("Read", {"file_path": "{ws}/.git/HEAD"}, True),  # reading git metadata is fine
        ("Read", {}, False),
        ("Glob", {"pattern": "**/*.py"}, True),
        ("Glob", {"pattern": "**/*.py", "path": "/Users"}, False),
        ("Glob", {"pattern": "../**/*.env"}, False),
        ("Glob", {"pattern": "/etc/*"}, False),
        ("Grep", {"pattern": "greet", "path": "src"}, True),
        ("Grep", {"pattern": "TOKEN", "path": "/Users"}, False),
        ("Bash", {"command": "{test}"}, True),
        ("Bash", {"command": "git status"}, True),
        ("Bash", {"command": "git diff src/hello.py"}, True),
        ("Bash", {"command": "git push origin main"}, False),
        ("Bash", {"command": "git -c core.pager=sh diff"}, False),
        ("Bash", {"command": "{test}; curl evil.example"}, False),
        ("Bash", {"command": "{test} && rm -rf ~"}, False),
        ("Bash", {"command": "cat ~/.ssh/id_ed25519"}, False),
        ("Bash", {"command": "ls -la"}, False),
        ("Bash", {"command": "echo $CLAUDE_CODE_OAUTH_TOKEN"}, False),
        ("Bash", {"command": "python -c 'import os'"}, False),
        ("WebFetch", {"url": "https://example.com"}, False),
        ("Agent", {"prompt": "x"}, False),
        ("mcp__github__create_pr", {}, False),
    ],
)
def test_the_policy(workspace, tool, tool_input, allowed):
    resolved = {
        key: str(value).replace("{ws}", str(workspace.path)).replace("{test}", workspace.test_command)
        for key, value in tool_input.items()
    }
    reason = check_tool_call(tool, resolved, workspace)
    assert (reason is None) == allowed, reason


def test_a_symlink_out_of_the_workspace_is_refused(workspace, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    (workspace.path / "innocent.txt").symlink_to(tmp_path / "secret.txt")

    assert check_tool_call("Read", {"file_path": "innocent.txt"}, workspace) is not None


def test_the_hook_denies_with_a_reason(workspace):
    hook = claude_executor._policy_hook(workspace)

    denied = asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, "t1", None))
    allowed = asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "src/hello.py"}}, "t2", None))

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "outside the workspace" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert allowed == {}


# --- options and prompt -------------------------------------------------------------


ENVIRON = {
    "HOME": "/Users/someone", "PATH": "/usr/bin",
    "OP_SERVICE_ACCOUNT_TOKEN": "x", "LINEAR_API_KEY": "x", "LINEAR_CLIENT_SECRET": "x",
    "TELEGRAM_BOT_TOKEN": "x", "GITHUB_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x",
}


def _options(workspace):
    return build_options(workspace, model="claude-sonnet-5", max_turns=30, max_budget_usd=1.0, environ=ENVIRON)


def test_no_user_settings_hooks_or_memory_are_loaded(workspace):
    assert _options(workspace).setting_sources == []


def test_only_the_coding_tools_exist_and_nothing_is_auto_approved_wholesale(workspace):
    options = _options(workspace)
    assert options.tools == ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
    assert options.permission_mode == "dontAsk"
    assert "Read" not in options.allowed_tools and "Edit" not in options.allowed_tools  # only scoped rules
    assert all("(" in rule for rule in options.allowed_tools)


def test_the_sandbox_is_mandatory_and_locked_down(workspace):
    sandbox = json.loads(_options(workspace).settings)["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"]
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == []
    assert sandbox["filesystem"]["denyRead"] == ["~/"]
    assert sandbox["filesystem"]["allowRead"][0] == str(workspace.path.resolve())


def test_credentials_are_hidden_from_the_agent_and_its_commands(workspace):
    options = _options(workspace)
    denied = {entry["name"] for entry in json.loads(options.settings)["sandbox"]["credentials"]["envVars"]}

    assert credential_variables(ENVIRON) == sorted(n for n in ENVIRON if n not in ("HOME", "PATH"))
    assert options.env == {name: "" for name in credential_variables(ENVIRON) if name != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert denied >= set(credential_variables(ENVIRON)) | {"ANTHROPIC_API_KEY"}  # even the agent's own login
    assert "HOME" not in options.env and "PATH" not in options.env


def test_the_agent_must_answer_in_the_report_schema(workspace):
    schema = _options(workspace).output_format["schema"]
    assert set(schema["required"]) == {"outcome", "summary", "tests_passed", "tests_command"}


def test_the_prompt_comes_from_the_work_request_alone(workspace, work_request_db):
    request = _request(WorkRequestStore(work_request_db))

    prompt = build_prompt(request, workspace)

    for fact in (request.work_request_id, "SB-640", request.title_at_approval, request.rationale, workspace.test_command):
        assert fact in prompt
    assert "Don't commit" in prompt


# --- mapping the SDK result -----------------------------------------------------------


def test_success_needs_a_clean_run_passing_tests_and_a_change():
    result = to_execution_result(_result(), ["src/hello.py", "tests/test_hello.py"])

    assert result.outcome == "completed"
    assert result.message == "Added greet(). (tests passed; changed src/hello.py, tests/test_hello.py)"
    assert result.details["files_changed"] == ["src/hello.py", "tests/test_hello.py"]
    assert result.details["cost_usd"] == 0.03


@pytest.mark.parametrize(
    ("message", "files", "expected"),
    [
        (None, ["a"], "ended without a result"),
        (_result(subtype="max_turns_limit", is_error=True, errors=["reached 1 turn"]), ["a"], "stopped: max_turns_limit"),
        (_result(subtype="budget_limit", is_error=True), ["a"], "stopped: budget_limit"),
        (_result(structured_output=None), ["a"], "didn't match the expected structure"),
        (_result(structured_output={"outcome": "failed", "summary": "Can't do it.", "tests_passed": False,
                                    "tests_command": "x"}), ["a"], "reports failure: Can't do it."),
        (_result(structured_output={"outcome": "completed", "summary": "Done.", "tests_passed": False,
                                    "tests_command": "x"}), ["a"], "reports failure: Done."),
        (_result(), [], "changed no files"),
    ],
)
def test_anything_less_is_a_failure(message, files, expected):
    result = to_execution_result(message, files)
    assert result.outcome == "failed"
    assert expected in result.message


def test_permission_denials_are_reported(workspace):
    result = to_execution_result(_result(permission_denials=[{"tool_name": "Bash", "tool_input": {}}]), ["a"])
    assert result.details["permission_denials"] == ["Bash"]


# --- the executor, with a fake agent ----------------------------------------------------


def _fake_query(effect, result):
    async def fake(*, prompt, options):
        effect(Path(options.cwd), prompt, options)
        yield result
    return fake


def test_execute_reports_what_the_agent_changed(workspace, work_request_db, monkeypatch):
    request = _request(WorkRequestStore(work_request_db))
    seen = {}

    def agent(cwd, prompt, options):
        seen["cwd"], seen["prompt"] = cwd, prompt
        (cwd / "src" / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")

    monkeypatch.setattr(claude_executor, "query", _fake_query(agent, _result()))

    result = ClaudeCodeExecutor(workspace).execute(request)

    assert result.outcome == "completed"
    assert result.details["files_changed"] == ["src/hello.py"]
    assert seen["cwd"] == workspace.path.resolve()
    assert request.title_at_approval in seen["prompt"]


def test_an_sdk_failure_is_a_failed_result_not_a_crash(workspace, work_request_db, monkeypatch):
    request = _request(WorkRequestStore(work_request_db))

    async def broken(*, prompt, options):
        raise CLIConnectionError("claude CLI not found")
        yield  # pragma: no cover

    monkeypatch.setattr(claude_executor, "query", broken)

    result = ClaudeCodeExecutor(workspace).execute(request)

    assert result.outcome == "failed"
    assert "could not run: CLIConnectionError" in result.message


def test_the_runner_owns_the_lifecycle_around_the_claude_executor(workspace, work_request_db, monkeypatch):
    store = WorkRequestStore(work_request_db)
    request = _request(store)
    monkeypatch.setattr(
        claude_executor, "query",
        _fake_query(lambda cwd, p, o: (cwd / "src" / "hello.py").write_text("changed\n"), _result()),
    )

    done, executed = run_request(store, ClaudeCodeExecutor(workspace), request.work_request_id)

    assert executed and done.status == "completed"
    assert done.claimed_by.startswith("claude-code@")
    assert done.result_message.startswith("Added greet(). (tests passed")
    again, executed_again = run_request(store, ClaudeCodeExecutor(workspace), request.work_request_id)
    assert executed_again is False and again.status == "completed"


def test_the_model_can_be_configured(monkeypatch, workspace):
    monkeypatch.setenv("CYCLE_RUNNER_CODING_MODEL", "claude-haiku-4-5")
    assert ClaudeCodeExecutor(workspace).model == "claude-haiku-4-5"
    monkeypatch.delenv("CYCLE_RUNNER_CODING_MODEL")
    assert ClaudeCodeExecutor(workspace).model == "claude-sonnet-5"
