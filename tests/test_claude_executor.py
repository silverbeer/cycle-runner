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


SUMMARY = "Added greet(name) to src/hello.py and a unit test for it; all tests pass."


def _result(**overrides):
    fields = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=4,
                  session_id="s", total_cost_usd=0.03, permission_denials=[],
                  structured_output={"outcome": "completed", "summary": SUMMARY,
                                     "tests_passed": True, "tests_command": "python -m unittest"})
    return ResultMessage(**{**fields, **overrides})


def _result_for(workspace, **report):
    """A successful result whose report names this workspace's test command, as a real one does."""
    return _result(structured_output={"outcome": "completed", "summary": SUMMARY, "tests_passed": True,
                                      "tests_command": workspace.test_command, **report})


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

    assert result.outcome == "changed"
    assert result.message == f"{SUMMARY} (tests passed; changed src/hello.py, tests/test_hello.py)"
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

    monkeypatch.setattr(claude_executor, "query", _fake_query(agent, _result_for(workspace), _test_run(workspace)))

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "changed"
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
        _fake_query(lambda cwd, p, o: (cwd / "src" / "hello.py").write_text("changed\n"), _result_for(workspace),
                    _test_run(workspace)),
    )

    resolver = FixedWorkspace(workspace)
    done, executed = run_request(store, ClaudeCodeExecutor(), resolver, request.work_request_id)

    assert executed and done.status == "completed"
    assert done.claimed_by.startswith("claude-code@")
    assert done.result_message.startswith(f"{SUMMARY} (tests passed")
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
        _fake_query(lambda cwd, p, o: (cwd / "src" / "hello.py").write_text("changed\n"), _result_for(workspace)),
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
    result = to_execution_result(_result_for(workspace), ["src/hello.py"], transcript, pattern)
    assert result.outcome == ("changed" if problem is None else "failed")


def test_tool_output_in_parts_is_read_as_text(workspace):
    transcript = _observed(
        workspace, _tool_call("t", "Bash", command=workspace.test_command),
        UserMessage(content=[ToolResultBlock(tool_use_id="t", content=[{"type": "text", "text": "Ran 1\n\nOK"}])]),
        AssistantMessage(content=[TextBlock(text="Tests pass.")], model="m"),
    )
    assert transcript.test_runs == [(1, "Ran 1\n\nOK")]
    assert transcript.verdict(r"^OK$") is None


def test_test_byproducts_are_not_the_agents_changes(workspace, work_request_db, monkeypatch):
    # Found live on MT: the work was already done, the agent changed nothing,
    # and pytest's temp files and logs made it look like a change.
    request = _request(WorkRequestStore(work_request_db))

    def tests_leave_files(cwd, prompt, options):
        (cwd / "logs").mkdir()
        (cwd / "logs" / "pytest.log").write_text("ran\n")
        (cwd / "pytest-of-someone" / "session.json").parent.mkdir()
        (cwd / "pytest-of-someone" / "session.json").write_text("{}")

    monkeypatch.setattr(claude_executor, "query", _fake_query(tests_leave_files, _result_for(workspace), _test_run(workspace)))

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "no_change" and result.message == f"No change was required. {SUMMARY}"
    assert result.details["files_changed"] == [] and result.details["other_new_files"] == 2


def test_new_files_the_agent_wrote_and_edits_to_existing_files_count(workspace, work_request_db, monkeypatch):
    request = _request(WorkRequestStore(work_request_db))

    def agent(cwd, prompt, options):
        (cwd / "src" / "hello.py").write_text("changed\n")
        (cwd / "tests" / "test_greet.py").write_text("# new test\n")
        (cwd / "junk.log").write_text("byproduct\n")

    messages = [_tool_call("w", "Write", file_path=str(workspace.path / "tests" / "test_greet.py")),
                _tool_call("e", "Edit", file_path="src/hello.py"), *_test_run(workspace)]
    monkeypatch.setattr(claude_executor, "query", _fake_query(agent, _result_for(workspace), messages))

    result = ClaudeCodeExecutor().execute(task_from_approval(request), workspace)

    assert result.outcome == "changed", result.message
    assert result.details["files_changed"] == ["src/hello.py", "tests/test_greet.py"]
    assert result.details["other_new_files"] == 1


def test_a_long_list_of_changes_is_shortened_in_the_message():
    files = [f"f{i}.py" for i in range(14)]
    result = to_execution_result(_result(), files)
    assert result.message.endswith("f9.py and 4 more)")
    assert result.details["files_changed"] == files


def test_a_placeholder_report_is_not_accepted(workspace):
    # Found live: after its real report failed the schema check three times, the
    # agent sent {"summary": "test", "tests_command": "pytest"} and stopped.
    transcript = _observed(workspace, _tool_call("r1", "StructuredOutput", outcome="completed"),
                           *_test_run(workspace), _tool_call("r2", "StructuredOutput", summary="test"))

    result = to_execution_result(_result_for(workspace, summary="test", tests_command="pytest"), ["src/hello.py"],
                                 transcript)

    assert result.outcome == "failed"
    assert result.message.startswith("The coding agent's report can't be used: its summary is a placeholder ('test')")
    assert result.details["report_attempts"] == 2


def test_the_prompt_says_how_to_report(workspace, work_request_db):
    prompt = build_prompt(task_from_approval(_request(WorkRequestStore(work_request_db))), workspace)
    assert "StructuredOutput tool once with all four fields" in prompt and "placeholder" in prompt


def test_the_agents_state_is_kept_beside_the_workspace_not_under_the_home_directory(workspace, work_request_db,
                                                                                   monkeypatch):
    # Found live: Claude Code keeps a transcript of every session under
    # ~/.claude/projects/<workspace path>, out of sight of the run.
    seen = {}
    monkeypatch.setattr(claude_executor, "query",
                        _fake_query(lambda cwd, p, o: seen.update(env=o.env), _result_for(workspace)))

    ClaudeCodeExecutor().execute(task_from_approval(_request(WorkRequestStore(work_request_db))), workspace)

    state = workspace.path.with_name(workspace.path.name + ".claude")
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(state.resolve())
    assert state.is_dir() and oct(state.stat().st_mode & 0o777) == "0o700"


# --- V1.3: outcomes and meaningful reports -------------------------------------------

SB_640_SUMMARY = (
    "Rate limiting and the password policy (items 3 and 4) are already implemented and tested on main; "
    "no code change needed. Items 1 and 2 are production operations left for a human."
)


def test_sb_640_regression_verified_work_with_no_change_is_no_change_not_failure(workspace):
    # V1.2's live WR-000001: the agent read the issue, ran the tests (passing) and
    # changed nothing, because the work was already on main. It reported "completed".
    transcript = _observed(workspace, _tool_call("r", "Read", file_path="src/hello.py"), *_test_run(workspace))

    result = to_execution_result(_result_for(workspace, summary=SB_640_SUMMARY), [], transcript)

    assert result.outcome == "no_change"
    assert result.message == f"No change was required. {SB_640_SUMMARY}"
    assert result.files_changed == []


def test_an_agent_reporting_no_change_with_verified_tests_is_no_change(workspace):
    transcript = _observed(workspace, *_test_run(workspace))
    result = to_execution_result(_result_for(workspace, outcome="no_change", summary=SB_640_SUMMARY), [], transcript)
    assert result.outcome == "no_change"


def test_no_change_still_needs_the_tests_to_have_run(workspace):
    # "Nothing to do" is a claim like any other: without an observed passing run it's a failure.
    result = to_execution_result(_result_for(workspace, summary=SB_640_SUMMARY), [], _observed(workspace))
    assert result.outcome == "failed" and "the tests were never run" in result.message


def test_an_agent_claiming_no_change_while_files_changed_fails(workspace):
    transcript = _observed(workspace, _tool_call("e", "Edit", file_path="src/hello.py"), *_test_run(workspace))
    result = to_execution_result(_result_for(workspace, outcome="no_change", summary=SB_640_SUMMARY),
                                 ["src/hello.py"], transcript)
    assert result.outcome == "failed" and "reports no change, but changed src/hello.py" in result.message


def test_a_changed_result_carries_the_executors_own_file_list(workspace):
    transcript = _observed(workspace, _tool_call("e", "Edit", file_path="src/hello.py"), *_test_run(workspace))
    result = to_execution_result(_result_for(workspace), ["src/hello.py"], transcript)
    assert (result.outcome, result.files_changed) == ("changed", ["src/hello.py"])


@pytest.mark.parametrize(
    ("summary", "problem"),
    [
        ("test", "a placeholder"),
        ("  TODO ", "a placeholder"),
        ("Done.", "a placeholder"),
        ("n/a", "a placeholder"),
        ("No changes", "a placeholder"),
        ("lorem ipsum dolor sit amet, consectetur adipiscing", "a placeholder"),
        ("Fixed it.", "too short"),
        ("Added the function and tests", "too short"),  # 5 words
        ("x" * 60, "a placeholder"),
        ("Everything is fine here now.", "too short"),
    ],
)
@pytest.mark.parametrize("files", [["src/hello.py"], []])
def test_a_meaningless_report_is_never_a_success(workspace, summary, problem, files):
    transcript = _observed(workspace, _tool_call("e", "Edit", file_path="src/hello.py"), *_test_run(workspace))

    result = to_execution_result(_result_for(workspace, summary=summary), files, transcript)

    assert result.outcome == "failed"  # neither changed nor no_change
    assert result.message.startswith("The coding agent's report can't be used:") and problem in result.message
