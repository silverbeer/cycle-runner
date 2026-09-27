"""Live checks of the "what should we work on next?" behaviour, and of approving it.

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
from cycle_runner.approval import APPROVED_KEY
from cycle_runner.work_requests import open_store
from cycle_runner.recommendation import PENDING_KEY, RECOMMENDER_NAME

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


def _ask(runner, text):
    events = asyncio.run(runner.run_debug(text, quiet=True))
    calls = [call.name for event in events for call in event.get_function_calls()]
    finals = [e for e in events if e.is_final_response() and e.content and e.content.parts]
    return calls, "".join(part.text or "" for part in finals[-1].content.parts)


def _state(runner):
    session = asyncio.run(
        runner.session_service.get_session(
            app_name="cycle_runner", user_id="debug_user_id", session_id="debug_session_id"
        )
    )
    return session.state


def _ask_next(monkeypatch, issues, runner=None):
    fake = FakeLinear(issues=issues)
    monkeypatch.setattr(linear_tools, "_client", lambda: fake)
    runner = runner or InMemoryRunner(agent=root_agent, app_name="cycle_runner")
    calls, reply = _ask(runner, "What should we work on next?")
    # The root agent delegates; the recommender's own get_cycle_status read shows
    # up in fake Linear's request log, not in the root agent's events.
    assert calls == [RECOMMENDER_NAME]
    assert any("activeCycle" in document for document in fake.documents)
    return fake, runner, reply


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
    fake, _, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert "linear facts" in reply.lower()
    assert _picked(reply) in {"TEST-2", "TEST-3"}
    assert reply.rstrip().endswith("?")
    _assert_read_only(fake)


def test_does_not_pick_work_that_is_blocked(ollama, monkeypatch):
    _, runner, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert _picked(reply) != "TEST-5"
    assert _state(runner)[PENDING_KEY]["recommendation"]["recommended_issue_id"] != "TEST-5"


def test_says_so_when_nothing_is_actionable(ollama, monkeypatch):
    fake, runner, reply = _ask_next(monkeypatch, NOTHING_ACTIONABLE)

    assert "blocked" in reply.lower()
    assert _state(runner).get(PENDING_KEY) is None  # nothing to approve
    assert reply.rstrip().endswith("?")
    _assert_read_only(fake)


def test_names_the_missing_cycle_goal_instead_of_inventing_one(ollama, monkeypatch):
    _, _, reply = _ask_next(monkeypatch, ACTIONABLE)

    assert "goal" in reply.lower()


def test_explicit_approval_of_a_live_recommendation_is_recorded(ollama, monkeypatch):
    _, runner, _ = _ask_next(monkeypatch, ACTIONABLE)
    pick = _state(runner)[PENDING_KEY]["recommendation"]["recommended_issue_id"]

    calls, reply = _ask(runner, "Yes, proceed")

    assert calls == []  # decided by code, no model or tool involved
    assert _state(runner)[APPROVED_KEY]["issue_id"] == pick
    assert reply.startswith(f"Approved {pick}. Work request WR-000001 created.")
    (request,) = open_store().list_all()
    assert (request.issue_id, request.status) == (pick, "pending")


def test_soft_reply_to_a_live_recommendation_is_not_approval(ollama, monkeypatch):
    _, runner, _ = _ask_next(monkeypatch, ACTIONABLE)

    _, reply = _ask(runner, "Sounds good")

    assert APPROVED_KEY not in _state(runner)
    assert 'reply "approve"' in reply


def test_normal_question_after_a_recommendation_is_answered_normally(ollama, monkeypatch):
    fake, runner, _ = _ask_next(monkeypatch, ACTIONABLE)

    calls, reply = _ask(runner, "What is my role?")

    assert calls == [] and reply.strip()
    assert not reply.startswith(("Approved", "Not approved", "To approve"))
    assert APPROVED_KEY not in _state(runner)

