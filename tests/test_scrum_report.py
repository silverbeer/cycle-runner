"""Scrum Master output and CLI: stable Markdown and JSON, partial reports, the doctor, and no secrets anywhere.

The golden files under docs/scrum-master/ are the documented samples. When a
deliberate change alters them, regenerate with:

    SCRUM_UPDATE_GOLDEN=1 uv run pytest tests/test_scrum_report.py
"""

import asyncio
import io
import json
import os
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from scrum_factory import (
    NOW,
    ago,
    branch,
    cycle,
    day,
    issue,
    local_branch,
    local_repo,
    ok,
    pr,
    project,
    snapshot,
)
from typer.testing import CliRunner

from cycle_runner.linear_client import LinearClient
from cycle_runner.scrum import doctor, github_client, render
from cycle_runner.scrum.analyze import analyze
from cycle_runner.scrum.cli import app
from cycle_runner.scrum.config import Config, RepoConfig
from cycle_runner.scrum.github_client import GitHubClient
from cycle_runner.scrum.models import Report, Snapshot, SourceStatus

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "scrum-master"
CONFIG = Config(team="TEST", repos=[RepoConfig(github="acme/app", local="/src/app")])
SECRET = "lin_" + "q" * 30
GH_TOKEN = "github_pat_" + "w" * 40


def sample_snapshot(*, github_ok: bool = True) -> Snapshot:
    """A small workspace with one of nearly everything the report can say."""
    issues = [
        issue(1, "completed", estimate=3, title="Ship the login page"),
        issue(2, "started", created=20, updated=9, priority=2, estimate=2, title="Fix the flaky checkout test"),
        issue(3, "started", priority=2, blocked_by=["TEST-4"], estimate=3, title="Wire payments to the new API"),
        issue(4, "unstarted", created=12, estimate=1, title="Payments API credentials", assignee=None),
        issue(5, "unstarted", created=2, estimate=None, due_date=day(1), title="Release notes",
              added_to_cycle_at=ago(2)),
        issue(6, "backlog", created=60, title="Dark mode", project_id="p2"),
        issue(7, "completed", created=50, title="Search: index", parent="TEST-8", project_id="p2"),
        issue(8, "unstarted", created=50, title="Search", children=["TEST-7"], project_id="p2"),
    ]
    prs = [
        pr(11, created=16, updated=1, title="Payments client", checks="SUCCESS", linear_ids=["TEST-3"]),
        pr(12, created=3, updated=2, commit=2, checks="FAILURE", title="Flaky test fix", linear_ids=["TEST-2"]),
        pr(13, created=30, updated=20, is_draft=True, title="Spike: GraphQL"),
    ]
    branches = [branch("old-feature", prs=[(9, "MERGED")]), branch("experiment", ahead=4)]
    local = [local_repo([local_branch("old-feature", merged=True), local_branch("hotfix", gone=True)])]
    sources = ok() if github_ok else [*ok("linear", "git"),
                                      SourceStatus(name="github", ok=False, complete=False, error="token missing")]
    return snapshot(
        issues=issues, cycle_=cycle(["TEST-1", "TEST-2", "TEST-3", "TEST-4", "TEST-5"], carried_in=["TEST-2"]),
        projects=[project("p1", ["TEST-1", "TEST-3"]), project("p2", ["TEST-6", "TEST-7", "TEST-8"], status="planned")],
        prs=prs if github_ok else [], branches=branches if github_ok else [], local=local, sources=sources,
    )


def sample_report(**kwargs) -> Report:
    return analyze(sample_snapshot(**kwargs), CONFIG, now=NOW)


def terminal_text(report: Report, section="all") -> str:
    buffer = io.StringIO()
    render.to_terminal(report, section, console=Console(file=buffer, width=120, color_system=None))
    return buffer.getvalue()


def golden(name: str, text: str) -> None:
    path = SAMPLES / name
    if os.environ.get("SCRUM_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.read_text() == text, f"{name} changed; regenerate with SCRUM_UPDATE_GOLDEN=1 if intended"


# --- output ----------------------------------------------------------------------------------


def test_markdown_is_stable():
    golden("sample-report.md", render.to_markdown(sample_report()))


def test_json_is_stable():
    golden("sample-report.json", render.to_json(sample_report()))


def test_terminal_is_stable():
    golden("sample-terminal.txt", terminal_text(sample_report()))


def test_snapshot_sample_is_stable():
    golden("sample-snapshot.json", sample_snapshot().model_dump_json(indent=2) + "\n")


def test_json_schemas_are_documented():
    golden("report.schema.json", json.dumps(Report.model_json_schema(), indent=2) + "\n")
    golden("snapshot.schema.json", json.dumps(Snapshot.model_json_schema(), indent=2) + "\n")


def test_report_json_round_trips():
    report = sample_report()
    assert Report.model_validate_json(render.to_json(report)) == report


def test_report_has_every_section_in_order():
    text = render.to_markdown(sample_report())
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Cycle 5", "## Top actions today", "## Aging and inactive issues",
                        "## Blockers and overdue work", "## Epics and parent issues", "## Open pull requests",
                        "## Branch cleanup candidates", "## Data quality"]


def test_top_actions_link_to_their_sources():
    report = sample_report()
    assert len(report.top_actions) == 3 and all(f.url for f in report.top_actions)


def test_partial_report_says_so_in_every_format():
    report = sample_report(github_ok=False)
    assert not report.complete
    assert "PARTIAL REPORT" in render.to_markdown(report)
    assert "PARTIAL REPORT" in terminal_text(report)
    assert json.loads(render.to_json(report))["complete"] is False
    assert "Not available (GitHub not collected)." in render.to_markdown(report, "prs")


@pytest.mark.parametrize("section", ["cycle", "aging", "epics", "prs", "branches"])
def test_each_section_renders_alone(section):
    text = render.to_markdown(sample_report(), section)
    assert "## Data quality" in text and "## Top actions today" not in text
    terminal_text(sample_report(), section)


# --- CLI -------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def files(tmp_path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(sample_snapshot().model_dump_json())
    partial = tmp_path / "partial.json"
    partial.write_text(sample_snapshot(github_ok=False).model_dump_json())
    config = tmp_path / "scrum.toml"
    config.write_text('team = "TEST"\n[[repos]]\ngithub = "acme/app"\nlocal = "/src/app"\n')
    return snap, partial, config


def test_cli_offline_report_formats(files, tmp_path):
    snap, _, config = files
    for fmt in ("markdown", "json", "terminal"):
        out = tmp_path / f"report.{fmt}"
        result = runner.invoke(app, ["report", "--offline", str(snap), "--config", str(config), "-f", fmt, "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert out.read_text()


def test_cli_strict_fails_on_partial(files):
    _, partial, config = files
    result = runner.invoke(app, ["report", "--offline", str(partial), "--config", str(config), "-f", "json", "--strict"])
    assert result.exit_code == 2
    assert json.loads(result.stdout.split("scrum-master:")[0])["complete"] is False
    assert runner.invoke(app, ["report", "--offline", str(partial), "--config", str(config), "-f", "json"]).exit_code == 0


def test_cli_stages_compose(files, tmp_path):
    snap, _, config = files
    report = tmp_path / "report.json"
    result = runner.invoke(app, ["analyze", str(snap), "--config", str(config), "--now", "2030-01-10T15:00:00+00:00",
                                 "-o", str(report)])
    assert result.exit_code == 0, result.output
    assert Report.model_validate_json(report.read_text()) == analyze(sample_snapshot(), CONFIG, now=NOW)
    rendered = runner.invoke(app, ["render", str(report), "-f", "markdown", "--section", "prs"])
    assert rendered.exit_code == 0 and "## Open pull requests" in rendered.stdout


def test_cli_without_linear_credentials_fails_clearly(files, monkeypatch):
    _, _, config = files
    result = runner.invoke(app, ["report", "--config", str(config)])
    assert result.exit_code == 1
    assert "Linear is unavailable, so there is no report" in result.output


def test_cli_rejects_a_bad_snapshot_and_config(tmp_path, files):
    _, _, config = files
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert runner.invoke(app, ["report", "--offline", str(bad), "--config", str(config)]).exit_code == 1
    worse = tmp_path / "bad.toml"
    worse.write_text('timezone = "Mars/Olympus"\n')
    assert runner.invoke(app, ["config", "--config", str(worse)]).exit_code == 1


def test_local_config_adds_private_repos(tmp_path):
    from cycle_runner.scrum.config import load

    (tmp_path / "scrum.toml").write_text('team = "TEST"\n[[repos]]\ngithub = "acme/public"\n')
    (tmp_path / "scrum.local.toml").write_text('timezone = "UTC"\n[[repos]]\ngithub = "acme/private"\n')
    config = load(tmp_path / "scrum.toml")
    assert [r.github for r in config.repos] == ["acme/public", "acme/private"]
    assert (config.team, config.timezone) == ("TEST", "UTC")


def test_config_shows_no_secret_values(files, monkeypatch):
    _, _, config = files
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("SCRUM_GITHUB_TOKEN", GH_TOKEN)
    result = runner.invoke(app, ["config", "--config", str(config), "--repo", "acme/other"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["credentials"]["github"] == ["SCRUM_GITHUB_TOKEN"]
    assert [r["github"] for r in data["repos"]] == ["acme/other"]
    assert SECRET not in result.output and GH_TOKEN not in result.output


# --- doctor ----------------------------------------------------------------------------------


def _fake_linear(viewer_ok=True):
    def handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "t" * 30, "expires_in": 100, "scope": "read"})
        if not viewer_ok:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"data": {"viewer": {"name": "Cycle Runner"},
                                                  "teams": {"nodes": [{"name": "Test", "activeCycle": {"number": 5}}]}}})

    async def no_sleep(_):
        return None

    return LinearClient("id", SECRET, transport=httpx.MockTransport(handler), sleep=no_sleep)


def _fake_github(scopes=""):
    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, headers={"x-oauth-scopes": scopes}, json={"login": "me"})
        return httpx.Response(200, json={"default_branch": "main"})

    return GitHubClient(token=GH_TOKEN, transport=httpx.MockTransport(handler))


def _doctor(environ, **clients):
    config = Config(team="TEST", repos=[RepoConfig(github="acme/app")])
    return asyncio.run(doctor.run(config, environ=environ, **clients))


def test_doctor_all_good_prints_no_secrets():
    env = {"LINEAR_CLIENT_ID": "id", "LINEAR_CLIENT_SECRET": SECRET, "SCRUM_GITHUB_TOKEN": GH_TOKEN}
    results = _doctor(env, linear=_fake_linear(), github=_fake_github())
    assert [r.status for r in results] == ["ok"] * len(results)
    text = " ".join(f"{r.name} {r.detail}" for r in results)
    assert SECRET not in text and GH_TOKEN not in text
    assert "active cycle 5" in text


def test_doctor_warns_about_a_write_capable_token():
    env = {"LINEAR_CLIENT_ID": "id", "LINEAR_CLIENT_SECRET": SECRET, "SCRUM_GITHUB_TOKEN": GH_TOKEN}
    results = _doctor(env, linear=_fake_linear(), github=_fake_github(scopes="repo, read:org"))
    token = next(r for r in results if r.name == "github token")
    assert token.status == "warn" and "write scopes (repo)" in token.detail


def test_doctor_reports_missing_and_unresolved_credentials():
    results = _doctor({"LINEAR_CLIENT_ID": "op://agents/x/id", "LINEAR_CLIENT_SECRET": "op://agents/x/s"})
    by_name = {r.name: r for r in results}
    assert by_name["linear credentials"].status == "fail" and "op run" in by_name["linear credentials"].detail
    assert by_name["github token"].status == "fail" and "is not set" in by_name["github token"].detail


def test_doctor_names_the_gh_cli_fallback_without_showing_it(monkeypatch):
    monkeypatch.setattr(github_client, "gh_cli_token", lambda: GH_TOKEN)
    monkeypatch.setattr(GitHubClient, "get", _fake_github(scopes="repo, workflow").get)
    results = _doctor({"LINEAR_CLIENT_ID": "id", "LINEAR_CLIENT_SECRET": SECRET}, linear=_fake_linear())
    token = next(r for r in results if r.name == "github token")
    assert token.status == "warn" and token.detail.startswith("`gh auth token` is a classic token")
    assert GH_TOKEN not in " ".join(r.detail for r in results)


def test_doctor_reports_linear_failure():
    env = {"LINEAR_CLIENT_ID": "id", "LINEAR_CLIENT_SECRET": SECRET, "SCRUM_GITHUB_TOKEN": GH_TOKEN}
    results = _doctor(env, linear=_fake_linear(viewer_ok=False), github=_fake_github())
    assert next(r for r in results if r.name == "linear access").status == "fail"


def test_cli_doctor_exit_code_on_failure(files):
    _, _, config = files
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 3 and "✗ linear credentials" in result.output
