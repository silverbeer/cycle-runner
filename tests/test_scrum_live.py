"""Scrum Master against the real Linear and GitHub APIs, read-only. Opt-in:

    op run --env-file .env -- uv run pytest -m linear tests/test_scrum_live.py
    op run --env-file .env -- uv run pytest -m "linear and github_read" tests/test_scrum_live.py   # gh CLI's token

Nothing here writes: the same read-only clients as the CLI, and assertions on
the shape of what comes back, not on workspace contents.
"""

import asyncio
import os
from datetime import UTC, datetime

import pytest

from cycle_runner.linear_client import LinearClient
from cycle_runner.scrum import collect_linear
from cycle_runner.scrum.analyze import analyze
from cycle_runner.scrum.collect import collect, github_client
from cycle_runner.scrum.config import load
from cycle_runner.scrum.github_client import gh_cli_token


def _resolved(name: str) -> bool:
    value = os.environ.get(name, "")
    return bool(value) and not value.startswith("op://")


needs_linear = pytest.mark.skipif(not _resolved("LINEAR_CLIENT_ID"), reason="needs `op run --env-file .env`")
needs_github = pytest.mark.skipif(not (_resolved("SCRUM_GITHUB_TOKEN") or gh_cli_token()),
                                  reason="needs `gh auth login` or `op run --env-file .env.scrum`")


@pytest.mark.linear
@needs_linear
def test_real_linear_collection_is_complete_and_consistent():
    config = load()
    out = asyncio.run(collect_linear.collect(LinearClient.from_env().query, team=config.team))
    assert out.status().ok
    assert out.issues and all(i.identifier.startswith(f"{config.team}-") for i in out.issues.values())
    if out.cycle:
        assert set(out.cycle.issues) <= out.issues.keys()


@pytest.mark.linear
@pytest.mark.github_read
@needs_linear
@needs_github
def test_real_report_end_to_end():
    config = load()
    github, why = github_client(config)
    assert github is not None, why
    snapshot = asyncio.run(collect(config, linear_query=LinearClient.from_env().query, github=github))
    report = analyze(snapshot, config, now=datetime.now(UTC))
    github_status = next(s for s in report.sources if s.name == "github")
    assert github_status.ok, github_status.error
    assert len(report.top_actions) <= 3 and all(f.url for f in report.top_actions if f.section != "cycle")
