"""Approval -> durable WorkRequest(PENDING), through the real ADK flow.

Scripted model, fake Linear, a temporary SQLite file per test (conftest).
"""

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from conftest import say
from cycle_runner import approval
from cycle_runner.approval import APPROVED_KEY
from cycle_runner.recommendation import PENDING_KEY
from cycle_runner.work_requests import open_store
from test_approval import Conversation, recommend


@pytest.fixture
def chat(monkeypatch):
    return Conversation(monkeypatch)


def _requests():
    return open_store().list_all()


def test_an_approved_recommendation_creates_exactly_one_pending_work_request(chat):
    chat.send("What should we work on next?", recommend())
    recommendation_id = chat.state[PENDING_KEY]["recommendation_id"]
    before = datetime.now(UTC)

    reply = chat.send("Yes, proceed")

    (request,) = _requests()
    assert reply.startswith("Approved TEST-2. Work request WR-000001 created.")
    assert request.work_request_id == "WR-000001"
    assert request.issue_id == "TEST-2"
    assert request.status == "pending"
    assert request.approved_by == "u"  # the ADK user_id; from Telegram, "telegram:<user id>"
    assert before - timedelta(seconds=1) <= request.approved_at <= datetime.now(UTC)
    assert request.recommendation_id == recommendation_id
    assert (request.cycle_number, request.title_at_approval, request.rationale) == (
        42, "Fix the flaky login", "because TEST-2",
    )


def test_approval_with_nothing_pending_creates_nothing(chat):
    chat.send("Yes, proceed", [say("There's nothing to approve.")])
    assert _requests() == []


def test_a_stale_recommendation_creates_nothing(chat):
    chat.send("What should we work on next?", recommend())
    chat.send("Tell me more", [say("More.")])

    chat.send("yes")

    assert _requests() == []


def test_a_mismatched_issue_id_creates_nothing(chat):
    chat.send("What should we work on next?", recommend())
    chat.send("approve TEST-3")
    assert _requests() == []


@pytest.mark.parametrize(
    "malformed",
    ["approve SB-", "approve 1234", "yes!!! but wait", "approve TEST-2 and TEST-3", "yess", "procede"],
)
def test_a_malformed_approval_creates_nothing(chat, malformed):
    chat.send("What should we work on next?", recommend())
    chat.send(malformed, [say("Could you clarify?")])
    assert _requests() == []


# --- duplicates and idempotency -----------------------------------------------


def test_saying_yes_twice_creates_one_work_request(chat):
    chat.send("What should we work on next?", recommend())
    chat.send("Yes, proceed")

    second = chat.send("Yes, proceed")
    third = chat.send("yes")

    assert len(_requests()) == 1
    for reply in (second, third):
        assert reply == (
            "Already approved: TEST-2 is work request WR-000001, still pending. "
            "No new work request was created."
        )


def test_reprocessing_an_approval_after_a_crash_finds_the_same_request(chat):
    # Simulate: the work request was written, then the process died before the
    # session state was updated, so the same approval is processed again.
    chat.send("What should we work on next?", recommend())
    pending = chat.state[PENDING_KEY]
    open_store().create_for_approval(
        recommendation_id=pending["recommendation_id"], issue_id="TEST-2", approved_by="u",
        approved_at=datetime.now(UTC), cycle_number=42, title_at_approval="Fix the flaky login",
        rationale="because TEST-2",
    )

    reply = chat.send("Yes, proceed")

    assert len(_requests()) == 1
    assert reply.startswith("Already approved: TEST-2 is work request WR-000001")


def test_a_new_recommendation_of_the_same_issue_is_a_separate_approval(chat):
    chat.send("What should we work on next?", recommend(pick="TEST-2"))
    chat.send("yes")
    chat.send("What should we work on next?", recommend(pick="TEST-2"))
    chat.send("yes")

    assert [(r.work_request_id, r.issue_id) for r in _requests()] == [
        ("WR-000001", "TEST-2"), ("WR-000002", "TEST-2"),
    ]


def test_if_the_store_fails_nothing_is_approved_and_the_user_can_retry(chat, monkeypatch):
    chat.send("What should we work on next?", recommend())

    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(approval, "open_store", broken)
    reply = chat.send("yes")

    assert reply.startswith("Not approved: I couldn't record a work request for TEST-2")
    assert chat.state[PENDING_KEY] is not None  # still pending for one retry
    monkeypatch.undo()  # the store works again (and the per-test DB path is gone)


# --- restart ------------------------------------------------------------------


def test_a_work_request_survives_a_restart(monkeypatch, work_request_db):
    first_run = Conversation(monkeypatch)
    first_run.send("What should we work on next?", recommend())
    first_run.send("Yes, proceed")
    created = open_store().get("WR-000001")

    # "Restart": a new Runner, a new (empty) session service, a new store
    # object on the same file. Nothing survives from the first run but the file.
    second_run = Conversation(monkeypatch)
    reply = second_run.send("Yes, proceed", [say("There's nothing waiting for approval.")])

    assert reply == "There's nothing waiting for approval."  # the conversation is gone
    assert second_run.state.get(PENDING_KEY) is None and APPROVED_KEY not in second_run.state
    assert open_store().list_all() == [created]  # still there, still one, still pending
    assert created.status == "pending"


def test_another_process_sees_the_pending_work_request(chat, work_request_db):
    chat.send("What should we work on next?", recommend())
    chat.send("yes")

    script = textwrap.dedent(
        f"""
        from cycle_runner.work_requests import WorkRequestStore
        for r in WorkRequestStore({str(work_request_db)!r}).list_all():
            print(r.work_request_id, r.issue_id, r.status)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["WR-000001", "TEST-2", "pending"]


# --- the only new side effect -------------------------------------------------


def test_approval_writes_nothing_to_linear(chat):
    chat.send("What should we work on next?", recommend())
    reads_before = len(chat.linear.documents)

    chat.send("yes")

    assert len(chat.linear.documents) == reads_before
    assert all("mutation" not in document for document in chat.linear.documents)


def test_approval_starts_no_agent_process_or_tool(chat, monkeypatch):
    chat.send("What should we work on next?", recommend())

    def forbidden(*args, **kwargs):
        raise AssertionError("approval tried to start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    calls_before = len(chat.llm.requests)

    chat.send("yes")

    assert len(chat.llm.requests) == calls_before  # no model: no agent ran
    assert not any(event.get_function_calls() for event in chat.events)  # no tool
    assert len(_requests()) == 1  # the one side effect that did happen
