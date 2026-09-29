"""V1.4 end to end against a real, disposable GitHub repository (silverbeer/cycle-runner-sandbox).

Local only (marker `github`). Needs only the sandbox's fine-grained token:

    op run --env-file .env.github -- uv run pytest tests/test_github_live.py -s

    local change ─► local commit (V1.3) ─► review ─► approve --commit <sha> ─► deliver ─► push + draft PR

Everything else is temporary: the database, the workspace root and the
"project repository" (a fresh clone of the sandbox). The draft PR it opens
is left for a human to look at and is never merged. No Claude, no Linear.
"""

import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime

import httpx
import pytest

from cycle_runner import executor as executor_module
from cycle_runner.executor import ExecutionResult, run_request
from cycle_runner.git_delivery import LocalGitDelivery
from cycle_runner.projects import WorkspaceResolver, load_projects
from cycle_runner.work_requests import WorkRequestStore

SANDBOX = "silverbeer/cycle-runner-sandbox"
TOKEN = os.environ.get("CYCLE_RUNNER_GITHUB_TOKEN", "")

pytestmark = [
    pytest.mark.github,
    pytest.mark.skipif(not TOKEN or TOKEN.startswith("op://"), reason="no GitHub token (use op run --env-file .env.github)"),
]


class SmallChange:
    """A deterministic stand-in for the coding agent: one real, checkable change."""

    name = "sandbox-change"

    def execute(self, task, workspace):
        (workspace.path / "src" / "greet.py").write_text(
            f'"""Added by Cycle Runner live test {task.work_request_id}."""\n\n\n'
            "def greet(name: str) -> str:\n    return f\"Hello, {name}!\"\n"
        )
        return ExecutionResult(
            outcome="changed", message="Added src/greet.py.", files_changed=["src/greet.py"],
            details={"summary": "Added greet(name) in src/greet.py for the V1.4 live delivery test.",
                     "tests_observed": 0},
        )


def _api(path):
    response = httpx.get(f"https://api.github.com/repos/{SANDBOX}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    response.raise_for_status()
    return response.json()


def test_a_local_commit_is_approved_pushed_and_opened_as_a_draft_pr(tmp_path, work_request_db, monkeypatch, capsys):
    # The "project repository": a fresh clone of the sandbox (read with the same token, via the environment).
    origin = tmp_path / "sandbox"
    subprocess.run(["gh", "repo", "clone", SANDBOX, str(origin), "--", "-q"], check=True,
                   env={**os.environ, "GH_TOKEN": TOKEN})
    config = tmp_path / "projects.toml"
    config.write_text(
        f'workspace_root = "{tmp_path / "workspaces"}"\n[projects.SANDBOX]\nrepository = "{origin}"\n'
        f'test_command = "true"\nbranch = "main"\ngithub = "{SANDBOX}"\n'
    )
    monkeypatch.setenv("CYCLE_RUNNER_PROJECTS", str(config))
    store = WorkRequestStore(work_request_db)  # temporary
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    # A work request number of its own per run (WR-0xxxxx), so branches from earlier runs don't collide.
    with sqlite3.connect(work_request_db) as db:
        db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('work_requests', ?)", (int(time.time()) % 900_000,))
    request, _ = store.create_for_approval(
        recommendation_id=f"live-{stamp}", issue_id="SANDBOX-1", approved_by="tests", approved_at=datetime.now(UTC),
        cycle_number=0, title_at_approval=f"Add greet() (live delivery test {stamp})", rationale="V1.4 live test.",
        project_id="SANDBOX",
    )
    # The work request number repeats across temporary databases, so an earlier run's branch
    # could exist on GitHub. Delivery must refuse that rather than push over it; skip instead.
    try:
        _api(f"/git/ref/heads/cycle-runner/{request.work_request_id}")
        pytest.skip(f"cycle-runner/{request.work_request_id} already exists on the sandbox from an earlier run")
    except httpx.HTTPStatusError:
        pass

    done, _ = run_request(store, SmallChange(), WorkspaceResolver(load_projects()), request.work_request_id,
                          deliverer=LocalGitDelivery(environ={}))
    assert (done.outcome, done.branch) == ("changed", f"cycle-runner/{request.work_request_id}")

    assert executor_module.main(["review", done.work_request_id]) == 0
    assert "delivery:  review_pending" in capsys.readouterr().out
    assert executor_module.main(["approve", done.work_request_id, "--commit", done.commit_sha[:12]]) == 0
    capsys.readouterr()

    assert executor_module.main(["deliver", done.work_request_id]) == 0
    print(capsys.readouterr().out)
    approval = store.live_approval(done.work_request_id)
    assert approval.status == "pr_created" and approval.commit_sha == done.commit_sha

    # Independently, from GitHub: the branch is at the approved commit, and the PR is a draft for it.
    assert _api(f"/git/ref/heads/{done.branch}")["object"]["sha"] == done.commit_sha
    pull = _api(f"/pulls/{approval.pr_number}")
    assert pull["draft"] is True and pull["head"]["sha"] == done.commit_sha and pull["base"]["ref"] == "main"
    assert pull["state"] == "open" and not pull["merged"]
    assert "Cycle Runner" in pull["body"] and done.commit_sha in pull["body"]
    assert "No test run was observed" in pull["body"]  # the stand-in ran none; the PR says so

    # Again: nothing new is pushed or opened.
    assert executor_module.main(["deliver", done.work_request_id]) == 0
    same = _api(f"/pulls?head=silverbeer:{done.branch}&state=all")
    assert [p["number"] for p in same] == [approval.pr_number]
    assert _api(f"/git/ref/heads/{done.branch}")["object"]["sha"] == done.commit_sha
    # The workspace still has no remote: the push went to a URL, not a configured remote.
    assert subprocess.run(["git", "-C", str(tmp_path / "workspaces" / done.work_request_id), "remote"],
                          capture_output=True, text=True).stdout == ""
