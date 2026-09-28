"""ClaudeCodeExecutor without calling Claude: policy, options, prompt, result mapping.

The SDK's query() is replaced by a fake, so these run in CI with no credentials.
The live-agent tests are in test_claude_live.py (marker `claude`).
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cycle_runner import claude_executor
from cycle_runner.claude_executor import (
    ClaudeCodeExecutor,
    Transcript,
    build_options,
    build_prompt,
    check_tool_call,
    credential_variables,
    to_execution_result,
)
from cycle_runner.executor import ExecutionTask, run_request, task_from_approval
from cycle_runner.work_requests import WorkRequestStore
from conftest import FixedWorkspace
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


def _tool_call(call_id, name, **tool_input):
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name=name, input=tool_input)], model="m")


def _tool_result(call_id, output):
    return UserMessage(content=[ToolResultBlock(tool_use_id=call_id, content=output, is_error=True)])


def _test_run(workspace, output="Ran 2 tests in 0.001s\n\nOK", call_id="t1"):
    """An observed run of the workspace's test command, as the SDK streams it."""
    return [_tool_call(call_id, "Bash", command=workspace.test_command), _tool_result(call_id, output)]


def _observed(workspace, *messages):
    transcript = Transcript(workspace.test_command)
    for message in messages:
        transcript.observe(message)
    return transcript


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
        ("Glob", {"pattern": "~/.ssh/*"}, False),  # found in review: ~ in a pattern
        ("Grep", {"pattern": "greet", "path": "src"}, True),
        ("Grep", {"pattern": "TOKEN", "path": "/Users"}, False),
        ("Bash", {"command": "{test}"}, True),
        ("Bash", {"command": "{test}", "run_in_background": True}, False),  # found live: output lands outside
        ("Bash", {"command": "{test}", "run_in_background": False}, True),
        ("Bash", {"command": "git status"}, False),  # git doesn't work in the sandbox; not offered
        ("Bash", {"command": "git diff src/hello.py"}, False),
        ("StructuredOutput", {"outcome": "completed"}, True),  # the SDK's report channel
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
        key: value.replace("{ws}", str(workspace.path)).replace("{test}", workspace.test_command)
        if isinstance(value, str) else value
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
    assert sandbox["filesystem"]["denyRead"] == ["/"]  # everything, then only what's needed
    assert sandbox["filesystem"]["allowRead"][0] == str(workspace.path.resolve())


def test_credentials_are_hidden_from_the_agent_and_its_commands(workspace):
    options = _options(workspace)
    denied = {entry["name"] for entry in json.loads(options.settings)["sandbox"]["credentials"]["envVars"]}

    assert credential_variables(ENVIRON) == sorted(n for n in ENVIRON if n not in ("HOME", "PATH"))
    blanked = {name for name, value in options.env.items() if value == ""}
    assert blanked == {name for name in credential_variables(ENVIRON) if name != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert denied >= set(credential_variables(ENVIRON)) | {"ANTHROPIC_API_KEY"}  # even the agent's own login
    assert "HOME" not in options.env and "PATH" not in options.env


def test_the_agent_gets_its_own_scratch_directory_and_nothing_else_to_write(workspace, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    options = build_options(workspace, model="m", max_turns=1, max_budget_usd=0.1, environ=ENVIRON, scratch=scratch)
    filesystem = json.loads(options.settings)["sandbox"]["filesystem"]
    root = str(workspace.path.resolve())

    assert filesystem["allowWrite"] == [str(scratch.resolve())]
    assert str(scratch.resolve()) in filesystem["allowRead"]
    # Claude Code's shared temp area (every session's files) and the clone's .git
    # are never writable. Found live: the temp area was writable by default.
    uid = os.getuid()
    assert set(filesystem["denyWrite"]) == {f"/private/tmp/claude-{uid}", f"/tmp/claude-{uid}", f"{root}/.git"}
    assert options.env["CLAUDE_CODE_TMPDIR"] == options.env["TMPPREFIX"] == str(scratch.resolve())
    assert options.env["SHELL"] == "/bin/bash"  # not the user's shell and its startup files


def test_a_parent_claude_code_sessions_variables_are_hidden_during_the_run(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_TMPDIR", "/tmp/claude-parent")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "login")

    with claude_executor._without_parent_session():
        assert "CLAUDE_CODE_SESSION_ID" not in os.environ and "CLAUDE_TMPDIR" not in os.environ
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "login"  # the agent's own login stays

    assert os.environ["CLAUDE_CODE_SESSION_ID"] == "parent"  # and everything comes back


def test_the_agent_must_answer_in_the_report_schema(workspace):
    schema = _options(workspace).output_format["schema"]
    assert set(schema["required"]) == {"outcome", "summary", "tests_passed", "tests_command"}


def test_the_prompt_comes_from_the_task(workspace):
    task = ExecutionTask(
        work_request_id="WR-000007", issue_id="DEMO-9", project_id="DEMO", title="Tighten signup passwords",
        description="Signups accept 3-character passwords.\n\nAlso rotate the admin password in prod.",
        rationale="Security fix.",
    )

    prompt = build_prompt(task, workspace)

    for fact in ("WR-000007", "DEMO-9", task.title, task.rationale, workspace.test_command):
        assert fact in prompt
    assert f"<issue>\n{task.description}\n</issue>" in prompt  # the whole description, delimited
    assert "not instructions" in prompt and "left for a human" in prompt
    assert "Don't commit" in prompt


def test_a_task_without_a_description_has_no_issue_section(workspace, work_request_db):
    request = _request(WorkRequestStore(work_request_db))

    prompt = build_prompt(task_from_approval(request), workspace)

    assert request.title_at_approval in prompt and "<issue>" not in prompt


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


def _fake_query(effect, result, messages=()):
    async def fake(*, prompt, options):
        effect(Path(options.cwd), prompt, options)
        for message in messages:
            yield message
        yield result
    return fake


def test_execute_reports_what_the_agent_changed(workspace, work_request_db, monkeypatch):
    request = _request(WorkRequestStore(work_request_db))
    seen = {}

    def agent(cwd, prompt, options):
        seen["cwd"], seen["prompt"] = cwd, prompt
        (cwd / "src" / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")

    monkeypatch.setattr(claude_executor, "query", _fake_query(agent, _result(), _test_run(workspace)))

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "completed"
    assert result.details["files_changed"] == ["src/hello.py"]
    assert result.details["tests_observed"] == 1 and result.details["tests_output_tail"].endswith("OK")
    assert seen["cwd"] == workspace.path.resolve()
    assert request.title_at_approval in seen["prompt"]


def test_an_sdk_failure_is_a_failed_result_not_a_crash(workspace, work_request_db, monkeypatch):
    request = _request(WorkRequestStore(work_request_db))

    async def broken(*, prompt, options):
        raise CLIConnectionError("claude CLI not found")
        yield  # pragma: no cover

    monkeypatch.setattr(claude_executor, "query", broken)

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "failed"
    assert "could not run: CLIConnectionError" in result.message


def test_the_runner_owns_the_lifecycle_around_the_claude_executor(workspace, work_request_db, monkeypatch):
    store = WorkRequestStore(work_request_db)
    request = _request(store)
    monkeypatch.setattr(
        claude_executor, "query",
        _fake_query(lambda cwd, p, o: (cwd / "src" / "hello.py").write_text("changed\n"), _result(),
                    _test_run(workspace)),
    )

    resolver = FixedWorkspace(workspace)
    done, executed = run_request(store, ClaudeCodeExecutor(), resolver, request.work_request_id)

    assert executed and done.status == "completed"
    assert done.claimed_by.startswith("claude-code@")
    assert done.result_message.startswith("Added greet(). (tests passed")
    assert done.result_message.endswith(f"[workspace: {workspace.path}]")  # where to inspect the work
    again, executed_again = run_request(store, ClaudeCodeExecutor(), resolver, request.work_request_id)
    assert executed_again is False and again.status == "completed"


def test_the_model_can_be_configured(monkeypatch, workspace):
    monkeypatch.setenv("CYCLE_RUNNER_CODING_MODEL", "claude-haiku-4-5")
    assert ClaudeCodeExecutor().model == "claude-haiku-4-5"
    monkeypatch.delenv("CYCLE_RUNNER_CODING_MODEL")
    assert ClaudeCodeExecutor().model == "claude-sonnet-5"


def test_a_failed_run_is_reported_from_its_result_not_as_a_crash(workspace, work_request_db, monkeypatch):
    # The SDK yields the failing ResultMessage, then raises ResultError.
    from claude_agent_sdk import ResultError

    request = _request(WorkRequestStore(work_request_db))

    async def out_of_turns(*, prompt, options):
        yield _result(subtype="error_max_turns", is_error=True, errors=["Reached maximum number of turns (1)"])
        raise ResultError("Claude Code returned an error result")

    monkeypatch.setattr(claude_executor, "query", out_of_turns)

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "failed"
    assert result.message == "The coding agent stopped: error_max_turns. Reached maximum number of turns (1)"



# --- the executor's own check of the tests ------------------------------------------------


def test_the_agents_word_alone_is_not_enough(workspace, monkeypatch, work_request_db):
    # The agent reports passing tests but never ran them.
    request = _request(WorkRequestStore(work_request_db))
    monkeypatch.setattr(
        claude_executor, "query",
        _fake_query(lambda cwd, p, o: (cwd / "src" / "hello.py").write_text("changed\n"), _result()),
    )

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "failed"
    assert "the tests were never run" in result.message
    assert result.details["tests_observed"] == 0


@pytest.mark.parametrize(
    ("messages", "pattern", "problem"),
    [
        (lambda ws: [], None, "the tests were never run"),
        # A command that merely resembles the test command isn't a test run.
        (lambda ws: [_tool_call("x", "Bash", command=ws.test_command + " -k nothing"), _tool_result("x", "OK")],
         None, "the tests were never run"),
        (lambda ws: [*_test_run(ws), _tool_call("e", "Edit", file_path="src/hello.py")],
         None, "files were edited after the last test run"),
        (lambda ws: _test_run(ws, output="FAILED (failures=1)"), r"^OK$", "doesn't show the tests passing"),
        (lambda ws: [*_test_run(ws, output="FAILED", call_id="a"), *_test_run(ws, call_id="b")], r"^OK$", None),
        (lambda ws: [*_test_run(ws, call_id="a"), *_test_run(ws, output="FAILED", call_id="b")], r"^OK$",
         "doesn't show the tests passing"),  # the last run counts, not the best one
        (lambda ws: [_tool_call("e", "Write", file_path="t.py"), *_test_run(ws)], r"^OK$", None),
        (lambda ws: _test_run(ws, output="Exit code 1\n1324 passed, 12 skipped in 31.02s"),
         r"^\d+ passed(, \d+ skipped)? in ", None),  # the sandbox's non-zero exit is ignored
    ],
)
def test_the_observed_test_runs(workspace, messages, pattern, problem):
    transcript = _observed(workspace, *messages(workspace))

    verdict = transcript.verdict(pattern)
    assert verdict is None if problem is None else problem in verdict
    result = to_execution_result(_result(), ["src/hello.py"], transcript, pattern)
    assert result.outcome == ("completed" if problem is None else "failed")


def test_tool_output_in_parts_is_read_as_text(workspace):
    transcript = _observed(
        workspace, _tool_call("t", "Bash", command=workspace.test_command),
        UserMessage(content=[ToolResultBlock(tool_use_id="t", content=[{"type": "text", "text": "Ran 1\n\nOK"}])]),
        AssistantMessage(content=[TextBlock(text="Tests pass.")], model="m"),
    )
    assert transcript.test_runs == [(1, "Ran 1\n\nOK")]
    assert transcript.verdict(r"^OK$") is None
