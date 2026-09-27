"""Explicit human approval of a pending recommendation.

This runs as the root agent's before_agent_callback, ahead of the model, on
every user message. Approval is decided here, by code, never by the model:

- A recommendation is pending only for the very next user message after it
  was shown. Anything else in between makes it stale.
- Only an explicit reply ("yes", "approve", "go ahead", "approve SB-123", ...)
  approves. Soft replies ("sounds good", "ok") get a request to confirm.
- Approval records the issue in session state. It changes nothing in Linear
  and starts no work.
"""

import re
from datetime import UTC, datetime
from typing import Literal

from google.genai import types

from cycle_runner.recommendation import PENDING_KEY, TURN_KEY

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

    pending = state.get(PENDING_KEY)
    if not pending:
        return None  # nothing to approve: the model answers as usual

    kind, named_id = classify(_text(callback_context.user_content))
    active = pending["turn"] == turn - 1
    issue_id = pending["recommendation"]["recommended_issue_id"]

    if kind == "approve":
        state[PENDING_KEY] = None  # an approval attempt always consumes the pending item
        if not active:
            return _reply(
                f"Not approved: the recommendation for {issue_id} is no longer pending, "
                "because the conversation moved on. Nothing was recorded. "
                'Ask "what should we work on next?" for a fresh recommendation.'
            )
        if named_id and named_id != issue_id:
            return _reply(
                f"Not approved: the pending recommendation is {issue_id}, not {named_id}. "
                "Nothing was recorded."
            )
        title = _title(pending["recommendation"], issue_id)
        state[APPROVED_KEY] = {
            "issue_id": issue_id,
            "title": title,
            "cycle_number": pending["recommendation"]["cycle_number"],
            "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "approved_turn": turn,
        }
        return _reply(
            f"Approved. I have recorded your approval for {issue_id}: {title}. "
            "No action has been taken yet: nothing was changed in Linear and no work was started."
        )

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


def _text(content: types.Content | None) -> str:
    if content is None or not content.parts:
        return ""
    return " ".join(part.text for part in content.parts if part.text)


def _title(recommendation: dict, issue_id: str) -> str:
    return next(c["title"] for c in recommendation["candidates"] if c["issue_id"] == issue_id)


def _reply(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])
