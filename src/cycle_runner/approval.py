"""Explicit human approval of a pending recommendation.

This runs as the root agent's before_agent_callback, ahead of the model, on
every user message. Approval is decided here, by code, never by the model:

- A recommendation is pending only for the very next user message after it
  was shown. Anything else in between makes it stale. This is intentional: a
  "yes" is only unambiguous as the direct answer to "Do you want to proceed
  with SB-123?". The one exception is a soft reply ("sounds good"), which gets
  "reply approve to approve it" and keeps the same recommendation pending for
  the reply to that question.
- Only an explicit reply ("yes", "approve", "go ahead", "approve SB-123", ...)
  approves. The decision is a lookup in fixed word lists plus turn numbers in
  session state: the same message in the same state always gets the same
  answer.
- Approval creates a durable, pending work request (work_requests.py), at most
  once per recommendation. It changes nothing in Linear and starts no work.
  The store is the only thing approval touches beyond session state.

Why the model is bypassed: returning content from a before_agent_callback
makes ADK skip the root agent for that message, so neither the model nor any
tool runs. The model can't turn "sounds good" into an approval, can't claim
something was approved when it wasn't, and can't call a tool as a side effect
of approving. The confirmation text is written here, by code, too.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from google.genai import types

from cycle_runner.recommendation import PENDING_KEY, TURN_KEY
from cycle_runner.work_requests import open_store

log = logging.getLogger(__name__)

APPROVED_KEY = "approved_work_item"

EXPLICIT = {
    "yes", "yes proceed", "proceed", "go ahead", "yes go ahead", "do it", "yes do it",
    "approve", "approved", "i approve", "yes approve", "approve it", "yes approve it",
}
EXPLICIT_WITH_ID = re.compile(r"^(?:yes )?(?:approve|proceed with|go ahead with|do) ([a-z][a-z0-9]*-\d+)$")
# Positive-sounding but not explicit: ask the user to confirm. Anything else
# ("interesting", "tell me more") is an ordinary message for the model.
SOFT = {
    "ok", "okay", "k", "sure", "yep", "yeah", "yup", "fine", "good", "great", "cool", "nice",
    "perfect", "alright", "sounds good", "that sounds good", "makes sense", "that makes sense",
}

Kind = Literal["approve", "soft", "other"]


def classify(text: str) -> tuple[Kind, str | None]:
    """Is this reply an explicit approval, a soft reaction, or something else?

    Returns the kind and, for "approve SB-123" style replies, the issue id named.
    """
    normalized = re.sub(r"[^\w\s-]", " ", text.lower())
    normalized = " ".join(normalized.split())
    if normalized in EXPLICIT:
        return "approve", None
    if match := EXPLICIT_WITH_ID.match(normalized):
        return "approve", match.group(1).upper()
    if normalized in SOFT:
        return "soft", None
    return "other", None


def approval_gate(callback_context) -> types.Content | None:
    """Count the turn, and handle approval replies without calling the model."""
    state = callback_context.state
    turn = state.get(TURN_KEY, 0) + 1
    state[TURN_KEY] = turn
    kind, named_id = classify(_text(callback_context.user_content))

    pending = state.get(PENDING_KEY)
    if not pending:
        return _repeat_of_last_approval(state, turn) if kind == "approve" else None

    active = pending["turn"] == turn - 1
    issue_id = pending["recommendation"]["recommended_issue_id"]

    if kind == "approve":
        if not active:
            state[PENDING_KEY] = None
            return _reply(
                f"Not approved: the recommendation for {issue_id} is no longer pending, "
                "because the conversation moved on. No work request was created. "
                'Ask "what should we work on next?" for a fresh recommendation.'
            )
        if named_id and named_id != issue_id:
            state[PENDING_KEY] = None
            return _reply(
                f"Not approved: the pending recommendation is {issue_id}, not {named_id}. "
                "No work request was created."
            )
        return _approve(callback_context, pending, turn)

    if kind == "soft" and active:
        pending["turn"] = turn  # still pending for the next reply
        state[PENDING_KEY] = pending
        return _reply(
            f'To approve {issue_id}, reply "approve" or "yes, proceed". '
            "Anything else leaves it unapproved."
        )

    if not active:
        state[PENDING_KEY] = None  # stale: forget it so later replies aren't misread
    return None


def _approve(callback_context, pending: dict, turn: int) -> types.Content:
    """Turn a valid approval into a durable work request, exactly once."""
    state = callback_context.state
    recommendation = pending["recommendation"]
    issue_id = recommendation["recommended_issue_id"]
    candidate = next(c for c in recommendation["candidates"] if c["issue_id"] == issue_id)
    log.info("approval received for %s", issue_id)

    # The durable write comes first. If anything after it fails, a retry of the
    # same approval finds the same recommendation_id and gets the same request.
    try:
        request, created = open_store().create_for_approval(
            recommendation_id=pending["recommendation_id"],
            issue_id=issue_id,
            approved_by=callback_context.user_id,
            approved_at=datetime.now(UTC),
            cycle_number=recommendation["cycle_number"],
            title_at_approval=candidate["title"],
            rationale=candidate["rationale"],
            # Which project the work belongs to, as Linear labelled it. The
            # executor side maps it to a workspace; approval needs no config.
            project_id=candidate["facts"].get("project"),
        )
    except Exception:
        log.exception("could not record the work request for %s", issue_id)
        pending["turn"] = turn  # still pending, so the next reply can retry
        state[PENDING_KEY] = pending
        return _reply(
            f"Not approved: I couldn't record a work request for {issue_id}. "
            "Nothing was created. Reply \"approve\" to try again."
        )

    log.info(
        "work request %s %s for %s",
        request.work_request_id, "created" if created else "already existed", issue_id,
    )
    state[PENDING_KEY] = None
    state[APPROVED_KEY] = {
        "work_request_id": request.work_request_id,
        "issue_id": issue_id,
        "title": candidate["title"],
        "approved_turn": turn,
    }
    if not created:
        return _already_approved(request.work_request_id, issue_id)
    return _reply(
        f"Approved {issue_id}. Work request {request.work_request_id} created. "
        "No work has been started: nothing was changed in Linear."
    )


def _repeat_of_last_approval(state, turn: int) -> types.Content | None:
    """ "yes" again, right after an approval (a double send or a retried update)."""
    approved = state.get(APPROVED_KEY)
    if approved and approved["approved_turn"] == turn - 1:
        approved["approved_turn"] = turn  # a third "yes" gets the same answer
        state[APPROVED_KEY] = approved
        return _already_approved(approved["work_request_id"], approved["issue_id"])
    return None  # nothing pending: the model answers, and it can't approve anything


def _already_approved(work_request_id: str, issue_id: str) -> types.Content:
    return _reply(
        f"Already approved: {issue_id} is work request {work_request_id}. "
        "No new work request was created."
    )


def _text(content: types.Content | None) -> str:
    if content is None or not content.parts:
        return ""
    return " ".join(part.text for part in content.parts if part.text)


def _reply(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])
