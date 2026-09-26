"""Live checks of the "what should we work on next?" behaviour.

Real model (Ollama), fake Linear (fixed scenarios from conftest). The model's
exact words vary run to run, so these assert only the structure the
instruction requires: fresh facts, facts kept apart from the recommendation,
blocked work not picked, a question at the end, and no write attempted.
The facts themselves are covered deterministically in test_linear_tools.py.
"""

import asyncio
import re

import pytest
from google.adk.runners import InMemoryRunner

from conftest import FakeLinear, fake_issue
from cycle_runner import linear_tools
from cycle_runner.agent import root_agent

pytestmark = pytest.mark.ollama

ACTIONABLE = [
    fake_issue("TEST-1", "Ship the widget", "Done", "completed"),
    fake_issue("TEST-2", "Fix the flaky login", "In Progress", "started", estimate=2, priority="High"),
    fake_issue("TEST-3", "Add rate limiting to the API", "Todo", "unstarted", estimate=3, priority="Urgent",
               created="2029-12-01"),
    fake_issue("TEST-5", "Enable rate-limit dashboards", "Todo", "unstarted", priority="Urgent",
               blocked_by=[("TEST-3", "unstarted")]),
    fake_issue("TEST-6", "Tidy README", "Todo", "unstarted", priority="Low"),
]

NOTHING_ACTIONABLE = [
    fake_issue("TEST-1", "Migrate database", "Todo", "unstarted", priority="High",
               blocked_by=[("TEST-9", "started")]),
    fake_issue("TEST-2", "Switch reads to new database", "Todo", "unstarted", priority="High",
               blocked_by=[("TEST-1", "unstarted")]),
    fake_issue("TEST-3", "Done thing", "Done", "completed"),
]


def _ask_next(monkeypatch, issues):
    fake = FakeLinear(issues=issues)
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    runner = InMemoryRunner(agent=root_agent, app_name="cycle_runner")
    events = asyncio.run(runner.run_debug("What should we work on next?", quiet=True))
    calls = [call.name for event in events for call in event.get_function_calls()]
    reply = "".join(part.text or "" for part in events[-1].content.parts)
    return fake, calls, reply


def _picked(reply):
    """The first issue id named after the "recommendation" marker."""
    after = re.split(r"my recommendation", reply, maxsplit=1, flags=re.I)
    assert len(after) == 2, "no separate 'My recommendation' line"
    ids = re.findall(r"TEST-\d+", after[1])
    return ids[0] if ids else None


def _assert_read_only(fake):
    assert fake.documents
    assert all("mutation" not in document for document in fake.documents)


def test_recommends_from_fresh_facts_and_keeps_them_apart(ollama, monkeypatch):
    fake, calls, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert calls == ["get_cycle_status"]
    assert "linear facts" in reply.lower()
    assert _picked(reply) in {"TEST-2", "TEST-3"}
    assert reply.rstrip().endswith("?")
    _assert_read_only(fake)


def test_does_not_pick_work_that_is_blocked(ollama, monkeypatch):
    _, _, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert _picked(reply) != "TEST-5"


def test_says_so_when_nothing_is_actionable(ollama, monkeypatch):
    fake, calls, reply = _ask_next(monkeypatch, NOTHING_ACTIONABLE)

    assert calls == ["get_cycle_status"]
    assert "blocked" in reply.lower()
    assert reply.rstrip().endswith("?")
    _assert_read_only(fake)


def test_names_the_missing_cycle_goal_instead_of_inventing_one(ollama, monkeypatch):
    _, _, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert "goal" in reply.lower()
