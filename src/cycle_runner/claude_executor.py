"""ClaudeCodeExecutor: an Executor that hands a work request to a Claude coding agent.

    request (WorkRequest) ─► prompt ─► Claude Agent SDK / Claude Code, confined to one workspace
                                          │ reads, edits, runs the workspace's tests
                                          ▼
                              structured report ─► ExecutionResult

Like every Executor, it only executes: the runner in executor.py claims the
request and records the outcome. It knows nothing about ADK, Telegram,
Linear or the conversation, and nothing about which project it's working on:
the workspace (a directory and how to run its tests) is given to it.

Confinement, from outermost to innermost. None of it relies on the prompt:

1. setting_sources=[]: none of the user's ~/.claude settings, hooks,
   CLAUDE.md, skills or permission rules are loaded.
2. tools: only Read, Glob, Grep, Edit, Write and Bash exist. No web, no
   subagents, no MCP.
3. permission_mode="dontAsk" with allow rules scoped to the workspace and to
   the test command: anything else that would need approval is denied.
4. A PreToolUse hook (check_tool_call) sees every tool call before anything
   else and denies paths outside the workspace, writes into .git, and any
   Bash command not on a short allowlist.
5. The OS sandbox (Seatbelt on macOS) wraps every Bash command and its child
   processes, including tests the agent wrote: writes only in the workspace;
   reads only in the workspace, the test interpreter and a few system
   directories (not the home directory, not other temp directories, not
   other repos); no network; no unsandboxed fallback; and a hard failure if
   the sandbox can't start.
6. Credentials: every credential-looking environment variable is blanked for
   the agent, and all of them (including the agent's own login) are unset
   inside sandboxed commands.

Measured, not assumed: with setting_sources=[] the sandbox settings still
apply. A test run by the agent could not read ~/.ssh, ~/.zshenv, another
repo's .env, a neighbouring directory or /etc, could not write outside the
workspace, had no network, and saw no credentials. Git doesn't work inside
this sandbox (macOS's git is an xcode-select shim that needs more of the
system), so the agent isn't given it; the executor finds changed files by
hashing the workspace instead.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, HookMatcher, ResultError, ResultMessage, query
from pydantic import BaseModel, ValidationError

from cycle_runner.executor import ExecutionResult
from cycle_runner.work_requests import WorkRequest

DEFAULT_MODEL = "claude-sonnet-5"
TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
PATH_TOOLS = {"Read", "Edit", "Write"}
WRITE_TOOLS = {"Edit", "Write"}
SEARCH_TOOLS = {"Glob", "Grep"}
SHELL_METACHARACTERS = re.compile(r"[;&|<>`$\\\n]")
# The SDK delivers output_format results through this tool; it carries the
# report and touches nothing, so the hook must let it through.
REPORT_TOOL = "StructuredOutput"
# What sandboxed commands may read besides the workspace and Workspace.readable:
# the system directories a process needs to start. Everything else, including
# the home directory, other temp directories and other repos, is unreadable.
SYSTEM_READABLE = ("/usr", "/bin", "/sbin", "/System", "/dev", "/private/etc", "/private/var/db/timezone")
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)
AGENT_LOGIN_VARIABLES = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}  # the CLI itself needs one


@dataclass(frozen=True)
class Workspace:
    """Where to work and how to check the work. The seed of a future per-project config.

    path: the repository the agent may read and change, and nothing else.
    test_command: the exact command that runs its tests.
    readable: extra directories sandboxed commands may read, such as the
        interpreter that runs the tests.
    """

    path: Path
    test_command: str
    readable: tuple[Path, ...] = ()


class AgentReport(BaseModel):
    """What the agent must hand back (enforced by the SDK's structured output)."""

    outcome: Literal["completed", "failed"]
    summary: str
    tests_passed: bool
    tests_command: str


# --- the task -------------------------------------------------------------------


def build_prompt(request: WorkRequest, workspace: Workspace) -> str:
    """The task, from the work request alone. The executor doesn't read Linear."""
    return (
        f"You are working on approved work request {request.work_request_id} "
        f"for issue {request.issue_id}.\n\n"
        f"Task: {request.title_at_approval}\n"
        f"Why it was chosen: {request.rationale}\n\n"
        "Work only inside the current directory. Use Read, Glob and Grep to look "
        "around and Edit or Write to change files. Make the smallest change that "
        "completes the task, add or update tests for it, then run the tests with "
        f"exactly this command, the only one Bash will run:\n\n    {workspace.test_command}\n\n"
        "You can't reach the network, other directories or other commands. "
        "Don't commit. Report outcome 'completed' only if the task is done and "
        "the tests pass; otherwise report 'failed' and say why."
    )


# --- the policy (the PreToolUse hook) -------------------------------------------


def check_tool_call(tool_name: str, tool_input: dict[str, Any], workspace: Workspace) -> str | None:
    """Why this tool call is refused, or None if it may proceed. Deterministic."""
    root = workspace.path.resolve()
    if tool_name == REPORT_TOOL:
        return None
    if tool_name not in TOOLS:
        return f"{tool_name} is not available to this executor"
    if tool_name in PATH_TOOLS:
        path = _inside(tool_input.get("file_path"), root)
        if path is None:
            return "files outside the workspace are off limits"
        if tool_name in WRITE_TOOLS and (path == root / ".git" or root / ".git" in path.parents):
            return "the repository's .git directory is read-only"
        return None
    if tool_name in SEARCH_TOOLS:
        where = tool_input.get("path")
        if where is not None and _inside(where, root) is None:
            return "searching outside the workspace is off limits"
        pattern = str(tool_input.get("pattern", ""))
        if ".." in pattern or (tool_name == "Glob" and pattern.startswith("/") and _inside(pattern, root) is None):
            return "search patterns must stay inside the workspace"
        return None
    command = " ".join(str(tool_input.get("command", "")).split())
    if SHELL_METACHARACTERS.search(command):
        return "shell operators, redirection and substitution aren't allowed"
    if command == workspace.test_command:
        return None
    return f"Bash only runs the tests (`{workspace.test_command}`); use Read, Glob and Grep to explore"


def _inside(raw: Any, root: Path) -> Path | None:
    """The resolved path if it's inside root (symlinks followed), else None."""
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))  # "~/.ssh" means the home directory, not ./~/.ssh
    path = (path if path.is_absolute() else root / path).resolve()
    return path if path == root or root in path.parents else None


def _policy_hook(workspace: Workspace):
    async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check_tool_call(input_data.get("tool_name", ""), input_data.get("tool_input") or {}, workspace)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return pre_tool_use


# --- options --------------------------------------------------------------------


def credential_variables(environ: dict[str, str]) -> list[str]:
    return sorted(name for name in environ if CREDENTIAL_NAME.search(name))


def build_options(workspace: Workspace, *, model: str, max_turns: int, max_budget_usd: float,
                  environ: dict[str, str]) -> ClaudeAgentOptions:
    root = str(workspace.path.resolve())
    credentials = credential_variables(environ)
    settings = {
        "permissions": {"blockReadsOutsideWorkingDirectories": True},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "filesystem": {
                "denyRead": ["/"],
                "allowRead": [root, *(str(p) for p in workspace.readable), *SYSTEM_READABLE],
            },
            "network": {"allowedDomains": []},
            "credentials": {
                "envVars": [{"name": name, "mode": "deny"} for name in sorted({*credentials, *AGENT_LOGIN_VARIABLES})]
            },
        },
    }
    return ClaudeAgentOptions(
        cwd=root,
        model=model,
        setting_sources=[],
        tools=TOOLS,
        permission_mode="dontAsk",
        allowed_tools=[
            f"Read(/{root}/**)",
            f"Edit(/{root}/**)",
            f"Bash({workspace.test_command})",
        ],
        hooks={"PreToolUse": [HookMatcher(hooks=[_policy_hook(workspace)])]},
        settings=json.dumps(settings),
        # Blank every credential the agent doesn't need; the SDK can only add or
        # override variables, not remove them.
        env={name: "" for name in credentials if name not in AGENT_LOGIN_VARIABLES},
        output_format={"type": "json_schema", "schema": AgentReport.model_json_schema()},
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )


# --- the result -----------------------------------------------------------------


def to_execution_result(message: ResultMessage | None, files_changed: list[str]) -> ExecutionResult:
    """Success only if the SDK run succeeded and the agent reports passing tests."""
    if message is None:
        return ExecutionResult(outcome="failed", message="The coding agent ended without a result.")
    details: dict[str, Any] = {
        "files_changed": files_changed,
        "turns": message.num_turns,
        "cost_usd": message.total_cost_usd,
        "stop": message.subtype,
        "permission_denials": [_denial(d) for d in message.permission_denials or []],
    }
    if message.is_error or message.subtype != "success":
        errors = "; ".join(message.errors or []) or (message.result or "")
        return ExecutionResult(
            outcome="failed", message=f"The coding agent stopped: {message.subtype}. {errors}".strip(), details=details
        )
    try:
        report = AgentReport.model_validate(message.structured_output)
    except ValidationError:
        return ExecutionResult(
            outcome="failed", message="The coding agent's report didn't match the expected structure.", details=details
        )
    details |= {"tests_passed": report.tests_passed, "tests_command": report.tests_command, "summary": report.summary}
    if report.outcome != "completed" or not report.tests_passed:
        return ExecutionResult(outcome="failed", message=f"The coding agent reports failure: {report.summary}", details=details)
    if not files_changed:
        return ExecutionResult(outcome="failed", message="The coding agent reports success but changed no files.", details=details)
    return ExecutionResult(
        outcome="completed",
        message=f"{report.summary} (tests passed; changed {', '.join(files_changed)})",
        details=details,
    )


def _denial(denial: Any) -> str:
    if isinstance(denial, dict):
        return str(denial.get("tool_name") or denial)
    return str(getattr(denial, "tool_name", denial))


def _snapshot(root: Path) -> dict[str, str]:
    """A hash of every file in the workspace (outside .git): what changed, without running git."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and relative.parts[0] != ".git" and "__pycache__" not in relative.parts:
            snapshot[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


# --- the executor ---------------------------------------------------------------


class ClaudeCodeExecutor:
    name = "claude-code"

    def __init__(self, workspace: Workspace, *, model: str | None = None, max_turns: int = 30,
                 max_budget_usd: float = 1.0):
        self.workspace = workspace
        self.model = model or os.environ.get("CYCLE_RUNNER_CODING_MODEL", DEFAULT_MODEL)
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd

    def execute(self, request: WorkRequest) -> ExecutionResult:
        before = _snapshot(self.workspace.path)
        try:
            message = asyncio.run(self._run_agent(request))
        except ClaudeSDKError as exc:
            return ExecutionResult(
                outcome="failed", message=f"The coding agent could not run: {type(exc).__name__}: {exc}"
            )
        after = _snapshot(self.workspace.path)
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        return to_execution_result(message, changed)

    async def _run_agent(self, request: WorkRequest) -> ResultMessage | None:
        options = build_options(
            self.workspace, model=self.model, max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd, environ=dict(os.environ),
        )
        result = None
        try:
            async for message in query(prompt=build_prompt(request, self.workspace), options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except ResultError:
            # A failed run yields its ResultMessage (turn limit, budget, ...) and
            # then raises. Report it from the message rather than as a crash.
            if result is None:
                raise
        return result


def python_for_tests() -> Path:
    """The base interpreter (not a venv): sandboxed tests can read it without the home directory."""
    return Path(sys.base_prefix) / "bin" / "python3"
