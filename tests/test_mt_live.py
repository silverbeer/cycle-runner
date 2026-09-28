"""V1.2 end to end on the real MissingTable project, with nothing real at stake.

    SB-640 (read from Linear) + MT (projects.toml) ─► fresh clone ─► setup ─► Claude ─► MT's unit tests

Local only (markers `claude` and `linear`). Needs the MT checkout, network
for setup, and both credentials:

    CLAUDE_CODE_OAUTH_TOKEN=$(op read op://agents/cycle-runner-claude/token) \\
        op run --env-file .env -- uv run pytest tests/test_mt_live.py -s

Uses a temporary database (a fresh request, not WR-000001) and a temporary
workspace root. The MT checkout is only cloned; the test proves it's
unchanged. Linear is only read. Real model calls: about a dollar or two.
"""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cycle_runner.claude_executor import ClaudeCodeExecutor
from cycle_runner.executor import ExecutionWorkspace, details_path, run_request
from cycle_runner.issue_context import LinearIssueSource
from cycle_runner.projects import WorkspaceResolver, load_projects
from cycle_runner.work_requests import WorkRequestStore

REPO_CONFIG = Path(__file__).parent.parent / "projects.toml"
ISSUE = "SB-640"

pytestmark = [
    pytest.mark.claude,
    pytest.mark.linear,
    pytest.mark.skipif(
        not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")),
        reason="no Claude credential",
    ),
    pytest.mark.skipif(not os.environ.get("LINEAR_CLIENT_SECRET"), reason="no Linear credentials (use op run)"),
]


def _checkout_state(repo: Path) -> tuple[str, str, str]:
    """Every ref, the working tree's status (untracked included) and the stash: enough to see any change."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

    return git("for-each-ref", "--format=%(refname) %(objectname)"), git("status", "--porcelain", "-uall"), git("stash", "list")


@pytest.fixture
def mt_config(tmp_path):
    """The committed MT configuration, with workspaces under this test's temp directory."""
    body = REPO_CONFIG.read_text().replace(
        'workspace_root = "~/.local/share/cycle-runner/workspaces"', f'workspace_root = "{tmp_path / "workspaces"}"'
    )
    path = tmp_path / "projects.toml"
    path.write_text(body)
    config = load_projects(path)
    if not (config.projects["MT"].repository / ".git").exists():
        pytest.skip("no MissingTable checkout")
    return config


def _request(store, issue_id, title, rationale="V1.2 live check."):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{issue_id}", issue_id=issue_id, approved_by="tests", approved_at=datetime.now(UTC),
        cycle_number=0, title_at_approval=title, rationale=rationale, project_id="MT",
    )
    return request


def _git(clone, *args):
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True).stdout


def test_sb_640_on_a_fresh_mt_clone(mt_config, work_request_db):
    # SB-640's code items (rate limiting, a password policy) are already on MT's
    # main (MT #612); what's left is production work. Seen live: usually "no
    # change made"; once the agent went on to edit local seed scripts and ran
    # out of turns. So this checks the plumbing and the record, not the choice.
    origin = mt_config.projects["MT"].repository
    before = _checkout_state(origin)
    store = WorkRequestStore(work_request_db)  # a temporary database; never data/cycle-runner.db
    request = _request(store, ISSUE, "Weak admin password and rate limiting disabled")

    done, executed = run_request(
        store, ClaudeCodeExecutor(max_turns=60, max_budget_usd=3.0), WorkspaceResolver(mt_config),
        request.work_request_id, LinearIssueSource.from_env(),
    )

    print(done.result_message)
    clone = mt_config.workspace_root / request.work_request_id
    assert executed and done.started_at is not None  # setup and the Linear read succeeded; the agent ran
    assert f"[workspace: {clone}]" in done.result_message
    assert (clone / "backend" / ".venv" / "bin" / "python").resolve().is_relative_to(clone.resolve())
    assert " raised " not in done.result_message  # finished or failed cleanly, never crashed
    record = json.loads(details_path(ExecutionWorkspace(path=clone, test_command="")).read_text())
    assert record["details"]["turns"] > 0 and (clone.parent / f"{clone.name}.claude").is_dir()
    changed = [line for line in _git(clone, "status", "--porcelain").splitlines() if not line.startswith("??")]
    if done.result_message.startswith("No change made."):
        assert changed == []
    if done.status == "completed":
        assert record["details"]["tests_observed"] >= 1
        assert any(line.endswith(".py") for line in changed), changed
    assert _git(clone, "remote") == ""
    assert _checkout_state(origin) == before  # the real checkout was only read


def test_a_small_change_to_mt_passes_mts_tests_in_the_sandbox(mt_config, work_request_db):
    # The agent changes MT's code and runs MT's unit tests inside the sandbox;
    # the executor accepts it only if it saw a passing run after the last edit.
    origin = mt_config.projects["MT"].repository
    before = _checkout_state(origin)
    store = WorkRequestStore(work_request_db)
    request = _request(
        store, "TEST-MT-1",
        "Add is_acceptable_password(password: str, username: str) -> bool to backend/constants/passwords.py: "
        "True when the existing password policy accepts the password for that username, False when it rejects "
        "it, with unit tests in backend/tests/unit/test_password_policy.py.",
    )

    done, executed = run_request(
        store, ClaudeCodeExecutor(max_turns=40, max_budget_usd=3.0), WorkspaceResolver(mt_config),
        request.work_request_id,  # the approval alone: no Linear issue for this one
    )

    print(done.result_message)
    clone = mt_config.workspace_root / request.work_request_id
    assert executed and done.status == "completed", done.result_message
    changed = _git(clone, "status", "--porcelain")
    assert "backend/constants/passwords.py" in changed and "test_password_policy.py" in changed, changed
    assert "def is_acceptable_password" in (clone / "backend" / "constants" / "passwords.py").read_text()
    assert _git(clone, "log", "--oneline", "-1") == _git(origin, "log", "--oneline", "-1", "main")  # no commits
    assert _checkout_state(origin) == before
