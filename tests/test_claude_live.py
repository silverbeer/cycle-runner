"""The real Claude coding agent, in disposable repositories.

Local only (marker `claude`); needs a Claude credential in the environment:

    CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) \\
        uv run pytest -m claude

Each run uses real model calls (a few cents each). Everything happens in
pytest temp directories; no real repository, Linear, GitHub or Telegram.
"""

import hashlib
import json
import os
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cycle_runner.claude_executor import ClaudeCodeExecutor
from cycle_runner.executor import run_request
from cycle_runner.work_requests import WorkRequestStore
from conftest import FixedWorkspace
from disposable_repo import changed_files, make_repo, run_tests

pytestmark = [
    pytest.mark.claude,
    pytest.mark.skipif(
        not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")),
        reason="no Claude credential (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)",
    ),
]

GREET_TASK = (
    "Add a greet(name: str) -> str function to src/hello.py that returns 'Hello, <name>!' "
    "(for example greet('Ada') returns 'Hello, Ada!'), and add a test for it."
)


def _pending(store, title):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{title[:12]}", issue_id="TEST-1", approved_by="tests",
        approved_at=datetime.now(UTC), cycle_number=1, title_at_approval=title,
        rationale="A small, well-defined change for testing the coding agent.",
    )
    return request.work_request_id


def test_the_agent_completes_a_small_task_through_the_real_lifecycle(tmp_path, work_request_db):
    workspace = make_repo(tmp_path / "repo")
    store = WorkRequestStore(work_request_db)
    wr = _pending(store, GREET_TASK)

    done, executed = run_request(store, ClaudeCodeExecutor(max_budget_usd=1.0), FixedWorkspace(workspace), wr)

    assert executed and done.status == "completed", done.result_message
    assert done.claimed_by.startswith("claude-code@")
    # Check the work ourselves rather than trusting the agent's report.
    changed = changed_files(workspace)
    assert "src/hello.py" in changed and any(name.startswith("tests/") for name in changed), changed
    check = subprocess.run(
        [str(workspace.test_command.split()[0]), "-c", "from src.hello import greet; print(greet('Ada'))"],
        cwd=workspace.path, capture_output=True, text=True,
    )
    assert check.stdout.strip() == "Hello, Ada!", check.stderr
    tests = run_tests(workspace)
    assert tests.returncode == 0, tests.stderr
    assert "test_shout" in tests.stderr  # the original test still runs and passes
    assert subprocess.run(["git", "log", "--oneline"], cwd=workspace.path, capture_output=True, text=True).stdout.count("\n") == 1  # no commits


def test_an_agent_that_runs_out_of_turns_is_a_clean_failure(tmp_path, work_request_db):
    workspace = make_repo(tmp_path / "repo")
    store = WorkRequestStore(work_request_db)
    wr = _pending(store, GREET_TASK)

    done, executed = run_request(store, ClaudeCodeExecutor(max_turns=1), FixedWorkspace(workspace), wr)

    assert executed and done.status == "failed"
    assert done.result_message.startswith("The coding agent stopped: error_max_turns")


PROBE_TEST = """
import json, os, pathlib, socket, unittest

def attempt(fn):
    try:
        fn()
        return "allowed"
    except Exception as e:
        return "blocked: " + type(e).__name__

class Environment(unittest.TestCase):
    def test_environment(self):
        home = pathlib.Path({home!r})
        results = {{
            "read_workspace": attempt(lambda: pathlib.Path("README.md").read_text()),
            "read_home": attempt(lambda: os.listdir(home)),
            "read_ssh": attempt(lambda: os.listdir(home / ".ssh")),
            "read_neighbour": attempt(lambda: pathlib.Path({neighbour!r}).read_text()),
            "write_neighbour": attempt(lambda: pathlib.Path({neighbour!r}).write_text("changed")),
            "write_home": attempt(lambda: (home / "cycle-runner-escape-marker").write_text("x")),
            "network": attempt(lambda: socket.create_connection(("example.com", 443), timeout=5)),
            "credentials": sorted(
                name for name in os.environ
                if any(word in name.upper() for word in ("TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH"))
                and os.environ[name]
            ),
        }}
        pathlib.Path("probe_results.json").write_text(json.dumps(results, indent=1))
"""


def test_code_the_agent_runs_cannot_escape_the_workspace(tmp_path, work_request_db):
    neighbour = tmp_path / "neighbour" / "notes.txt"  # a sibling "repository" next to the workspace
    neighbour.parent.mkdir()
    neighbour.write_text("untouched")
    before = hashlib.sha256(neighbour.read_bytes()).hexdigest()
    marker = Path.home() / "cycle-runner-escape-marker"
    probe = textwrap.dedent(PROBE_TEST).format(home=str(Path.home()), neighbour=str(neighbour))
    workspace = make_repo(tmp_path / "repo", {"tests/test_environment.py": probe})
    store = WorkRequestStore(work_request_db)
    wr = _pending(store, "Run the test suite and report whether it passes. Do not change any code.")

    run_request(store, ClaudeCodeExecutor(max_budget_usd=0.5), FixedWorkspace(workspace), wr)

    results = json.loads((workspace.path / "probe_results.json").read_text())
    assert results["read_workspace"] == "allowed"
    for escape in ("read_home", "read_ssh", "read_neighbour", "write_neighbour", "write_home", "network"):
        assert results[escape].startswith("blocked"), (escape, results[escape])
    # No credential from the executor's own environment (the Claude login, and in
    # a normal shell OP_SERVICE_ACCOUNT_TOKEN, LINEAR_API_KEY, ...) reaches the
    # sandbox. The sandbox injects its own proxy settings (CLOUDSDK_PROXY_*,
    # GIT_CONFIG_*) pointing at its local proxy; those grant nothing.
    ours = {name for name in os.environ if any(w in name.upper() for w in ("TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH"))}
    assert "CLAUDE_CODE_OAUTH_TOKEN" in ours  # the check is meaningful: we do hold credentials
    assert sorted(set(results["credentials"]) & ours) == []
    assert hashlib.sha256(neighbour.read_bytes()).hexdigest() == before
    assert not marker.exists()
