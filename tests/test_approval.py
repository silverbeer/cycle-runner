"""Recommendation -> explicit approval -> recorded approval, end to end.

These run the real ADK Runner, AgentTool, callbacks and session state with a
scripted model (ScriptedLlm) and fake Linear (FakeLinear), so every step is
deterministic. The live-model versions are in test_recommendation.py.
"""

import asyncio
import json

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from conftest import FakeLinear, ScriptedLlm, call, fake_issue, say
from cycle_runner import linear_tools
from cycle_runner.agent import build_root_agent
from cycle_runner.approval import APPROVED_KEY, classify
from cycle_runner.recommendation import (
    EVIDENCE_KEY,
    OUTPUT_KEY,
    PENDING_KEY,
    RECOMMENDER_NAME,
    Recommendation,
    render,
)

ISSUES = [
    fake_issue("TEST-1", "Done thing", "Done", "completed"),
    fake_issue("TEST-2", "Fix the flaky login", "In Progress", "started", estimate=2, priority="High"),
    fake_issue("TEST-3", "Add rate limiting", "Todo", "unstarted", estimate=3, priority="Urgent"),
    fake_issue("TEST-5", "Rate-limit dashboards", "Todo", "unstarted", blocked_by=[("TEST-3", "unstarted")]),
]


def recommend(pick="TEST-2", candidates=("TEST-2", "TEST-3")):
    """The two model calls of a recommendation turn.

    Code reads the cycle in between (before_tool_callback), so there's no
    get_cycle_status model call, and rendering needs no model call either.
    """
    decision = {
        "candidates": [{"issue_id": c, "rationale": f"because {c}"} for c in candidates],
        "recommended_issue_id": pick,
        "unknowns": [],
    }
    return [
        call(RECOMMENDER_NAME, request="What should we work on next?"),  # root -> recommender
        say(json.dumps(decision)),  # recommender answers in the schema
    ]


class Conversation:
    """A Runner session driven turn by turn, with a scripted model and fake Linear."""

    def __init__(self, monkeypatch, issues=ISSUES):
        self.llm = ScriptedLlm()
        self.linear = FakeLinear(issues=issues)
        monkeypatch.setattr(linear_tools, "_client", lambda: self.linear)
        self.runner = Runner(
            app_name="cycle_runner",
            agent=build_root_agent(self.llm),
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )
        self.events = []

    def send(self, text, script=()):
        self.llm.script.extend(script)

        async def run():
            events = []
            message = types.Content(role="user", parts=[types.Part(text=text)])
            async for event in self.runner.run_async(user_id="u", session_id="s", new_message=message):
                events.append(event)
            return events

        self.events = asyncio.run(run())
        assert self.llm.script == [], "not every scripted model call happened"
        finals = [e for e in self.events if e.is_final_response() and e.content and e.content.parts]
        return "".join(part.text or "" for part in finals[-1].content.parts)

    @property
    def state(self):
        session = asyncio.run(
            self.runner.session_service.get_session(app_name="cycle_runner", user_id="u", session_id="s")
        )
        return session.state

    def pending_id(self):
        pending = self.state.get(PENDING_KEY)
        return pending and pending["recommendation"]["recommended_issue_id"]


@pytest.fixture
def chat(monkeypatch):
    return Conversation(monkeypatch)


# --- the recommendation -------------------------------------------------------


def test_recommendation_is_stored_in_session_state(chat):
    chat.send("What should we work on next?", recommend())

    state = chat.state
    stored = Recommendation.model_validate(state[PENDING_KEY]["recommendation"])
    assert stored.recommended_issue_id == "TEST-2"
    assert state[PENDING_KEY]["turn"] == 1
    assert state[OUTPUT_KEY]["recommended_issue_id"] == "TEST-2"  # the recommender's output_key
    assert {i["id"] for i in state[EVIDENCE_KEY]["open_issues"]} == {"TEST-2", "TEST-3", "TEST-5"}


def test_the_recommender_is_given_the_cycle_that_code_read(chat):
    chat.send("What should we work on next?", recommend())

    recommender_request = chat.llm.requests[1]
    instruction = str(recommender_request.config.system_instruction)
    assert "TEST-5" in instruction and "blocked_by" in instruction  # via {recommender_evidence}
    assert not recommender_request.config.tools  # no tools: the schema can be enforced natively
    assert recommender_request.config.response_schema is not None


def test_user_sees_exactly_the_stored_recommendation_without_an_extra_model_call(chat):
    reply = chat.send("What should we work on next?", recommend())

    stored = Recommendation.model_validate(chat.state[PENDING_KEY]["recommendation"])
    assert reply == render(stored)
    assert len(chat.llm.requests) == 2  # root, recommender; rendering used no model call


def test_a_blocked_pick_is_corrected_and_leaves_nothing_pending(chat):
    reply = chat.send("What should we work on next?", recommend(pick="TEST-5", candidates=("TEST-5",)))

    assert "TEST-5 was picked, but Linear shows it blocked by TEST-3" in reply
    assert "My recommendation: nothing is actionable right now." in reply
    assert chat.state.get(PENDING_KEY) is None


def test_a_recommender_that_never_answers_in_the_schema_leaves_nothing_pending(chat):
    script = [call(RECOMMENDER_NAME, request="next?"), say("I think TEST-2.")]
    reply = chat.send("What should we work on next?", script)

    assert "couldn't produce a recommendation" in reply
    assert chat.state.get(PENDING_KEY) is None


def test_a_cycle_that_cannot_be_read_skips_the_recommender(monkeypatch):
    chat = Conversation(monkeypatch)
    chat.linear.cycle = None  # no active cycle

    reply = chat.send("What should we work on next?", [call(RECOMMENDER_NAME, request="next?")])

    assert reply == "I couldn't produce a recommendation: The team has no active cycle. Nothing is pending approval."
    assert len(chat.llm.requests) == 1  # the recommender never ran


def test_a_new_recommendation_replaces_the_pending_one(chat):
    chat.send("What should we work on next?", recommend(pick="TEST-2"))
    chat.send("And now?", recommend(pick="TEST-3"))

    assert chat.pending_id() == "TEST-3"
    chat.send("approve")
    assert chat.state[APPROVED_KEY]["issue_id"] == "TEST-3"


# --- approval -----------------------------------------------------------------


def test_explicit_approval_records_the_pending_recommendation(chat):
    chat.send("What should we work on next?", recommend())

    reply = chat.send("Yes, proceed")

    approved = chat.state[APPROVED_KEY]
    assert (approved["issue_id"], approved["title"], approved["cycle_number"]) == (
        "TEST-2", "Fix the flaky login", 42,
    )
    assert reply.startswith("Approved. I have recorded your approval for TEST-2")
    assert "No action has been taken yet" in reply
    assert chat.state.get(PENDING_KEY) is None


def test_approval_is_decided_by_code_not_the_model(chat):
    chat.send("What should we work on next?", recommend())
    calls_before = len(chat.llm.requests)

    chat.send("go ahead")

    assert len(chat.llm.requests) == calls_before  # the model was never asked


def test_approval_writes_nothing_to_linear_and_starts_nothing(chat):
    chat.send("What should we work on next?", recommend())
    linear_calls_before = len(chat.linear.documents)

    chat.send("approve")

    assert len(chat.linear.documents) == linear_calls_before  # approval didn't touch Linear
    assert all("mutation" not in doc for doc in chat.linear.documents)
    assert not any(event.get_function_calls() for event in chat.events)  # no tool, no agent run


def test_approval_naming_the_pending_issue_works(chat):
    chat.send("What should we work on next?", recommend())
    chat.send("approve test-2")
    assert chat.state[APPROVED_KEY]["issue_id"] == "TEST-2"


def test_approval_naming_a_different_issue_is_refused(chat):
    chat.send("What should we work on next?", recommend())

    reply = chat.send("approve TEST-3")

    assert reply.startswith("Not approved")
    assert APPROVED_KEY not in chat.state


@pytest.mark.parametrize("reply", ["Sounds good", "ok", "That makes sense"])
def test_soft_replies_ask_for_explicit_confirmation(chat, reply):
    chat.send("What should we work on next?", recommend())

    answer = chat.send(reply)

    assert APPROVED_KEY not in chat.state
    assert 'reply "approve"' in answer
    assert chat.pending_id() == "TEST-2"  # still pending for the next reply
    chat.send("yes")
    assert chat.state[APPROVED_KEY]["issue_id"] == "TEST-2"


@pytest.mark.parametrize("reply", ["Interesting", "Tell me more"])
def test_non_approving_replies_go_to_the_model_and_approve_nothing(chat, reply):
    chat.send("What should we work on next?", recommend())

    answer = chat.send(reply, [say("Here is more detail.")])

    assert answer == "Here is more detail."
    assert APPROVED_KEY not in chat.state


def test_approval_after_the_conversation_moved_on_is_stale(chat):
    chat.send("What should we work on next?", recommend(pick="TEST-2"))
    chat.send("Tell me about TEST-3", [call("get_issue", issue_id="TEST-3"), say("TEST-3 is about rate limiting.")])

    reply = chat.send("Yes, proceed")

    assert reply.startswith("Not approved: the recommendation for TEST-2 is no longer pending")
    assert APPROVED_KEY not in chat.state
    assert chat.state.get(PENDING_KEY) is None


def test_approval_with_nothing_pending_approves_nothing(chat):
    answer = chat.send("Yes, proceed", [say("There's nothing waiting for approval.")])

    assert answer == "There's nothing waiting for approval."  # the model answered, not the gate
    assert APPROVED_KEY not in chat.state


def test_nothing_actionable_leaves_nothing_to_approve(chat):
    chat.send("What should we work on next?", recommend(pick=None, candidates=()))

    assert chat.state.get(PENDING_KEY) is None
    answer = chat.send("yes", [say("Nothing is waiting for approval.")])
    assert APPROVED_KEY not in chat.state and answer == "Nothing is waiting for approval."


# --- normal conversation still works ------------------------------------------


def test_normal_questions_are_answered_by_the_model(chat):
    assert chat.send("What is my role?", [say("You're the decision maker.")]) == "You're the decision maker."
    assert chat.send("How is the cycle going?", [call("get_cycle_status"), say("Half done.")]) == "Half done."
    assert APPROVED_KEY not in chat.state and chat.state.get(PENDING_KEY) is None


# --- classifying replies ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes", ("approve", None)),
        ("Yes, proceed.", ("approve", None)),
        ("Go ahead!", ("approve", None)),
        ("do it", ("approve", None)),
        ("Approve", ("approve", None)),
        ("approve SB-1127", ("approve", "SB-1127")),
        ("Yes, proceed with sb-1127", ("approve", "SB-1127")),
        ("Sounds good", ("soft", None)),
        ("ok", ("soft", None)),
        ("That makes sense", ("soft", None)),
        ("Interesting", ("other", None)),
        ("Tell me more", ("other", None)),
        ("yes but check SB-2 first", ("other", None)),
        ("don't do it", ("other", None)),
        ("no", ("other", None)),
        ("", ("other", None)),
    ],
)
def test_classify(text, expected):
    assert classify(text) == expected
