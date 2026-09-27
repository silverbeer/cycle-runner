"""Structured "what should we work on next?" recommendations.

The recommender is a small ADK agent with an output_schema. The root agent
calls it through AgentTool, so the conversation stays with the root agent
and only this one step is forced into a schema.

Facts and judgment are kept apart by construction:
- the recommender model writes only RecommenderOutput: which issues, why, and
  what it couldn't know;
- code then builds the stored Recommendation, copying titles and facts from
  the get_cycle_status result the recommender actually saw, and rejecting
  anything that isn't an open, unblocked issue of that cycle.

Nothing here writes to Linear or starts any work.
"""

from typing import Any

from google.adk.agents import Agent
from google.adk.models import BaseLlm
from google.adk.tools import BaseTool, ToolContext
from pydantic import BaseModel, Field

from cycle_runner.linear_tools import get_cycle_status

RECOMMENDER_NAME = "recommend_next_work"
EVIDENCE_KEY = "recommender_evidence"
OUTPUT_KEY = "recommender_output"
PENDING_KEY = "pending_recommendation"


# --- what the recommender model writes ----------------------------------------


class CandidateChoice(BaseModel):
    issue_id: str = Field(description="An open issue id from get_cycle_status, like SB-123.")
    rationale: str = Field(description="Why this issue: your reasoning, not a restatement of facts.")


class RecommenderOutput(BaseModel):
    candidates: list[CandidateChoice] = Field(default_factory=list, max_length=3)
    recommended_issue_id: str | None = Field(
        default=None, description="One of the candidates' ids, or null if nothing is actionable."
    )
    unknowns: list[str] = Field(
        default_factory=list,
        description="What you could not consider because the cycle data doesn't record it.",
    )


# --- what is stored and shown -------------------------------------------------


class IssueFacts(BaseModel):
    """Copied from get_cycle_status by code. The model never writes these."""

    status: str
    priority: str
    estimate: float | None
    age_days: int
    blocked_by: list[str]


class Candidate(BaseModel):
    issue_id: str
    title: str
    facts: IssueFacts
    rationale: str


class Recommendation(BaseModel):
    cycle_number: int
    candidates: list[Candidate]
    recommended_issue_id: str | None
    blocked: dict[str, list[str]]  # open issue -> the open issues blocking it (from Linear)
    unknowns: list[str]
    approval_question: str


class RecommendationRejected(Exception):
    """The recommender's output didn't survive validation against Linear's facts."""


# --- the recommender agent ----------------------------------------------------

INSTRUCTION = """\
You choose what a one-week engineering team should work on next. Call
get_cycle_status once, then answer with the structured result.

Weigh the open issues. No single factor decides:
- In progress: finishing started work usually beats starting new work.
- blocked_by: an issue blocked by open work is not actionable and must not be
  recommended or listed as a candidate; its blocker might be a better pick.
- Priority, estimate and days_remaining: what fits the time left.
- age_days and labels, as supporting evidence.

Give up to three candidates, each an open, unblocked issue id with your
rationale. If nothing is actionable, give no candidates.
recommended_issue_id must be exactly one of your candidates' ids, or null if
nothing is actionable. List in unknowns what the cycle data doesn't record
that would matter; if the cycle's goal is null, say the cycle has no goal set.
Use only ids that appear in the tool result.
"""


def capture_evidence(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> None:
    """Remember what the recommender saw, so its output can be checked against it.

    AgentTool forwards this state change from the recommender's own session
    into the caller's session.
    """
    if tool.name != "get_cycle_status" or not isinstance(tool_response, dict):
        return None
    if "error" in tool_response:
        tool_context.state[EVIDENCE_KEY] = {"error": tool_response["error"]}
        return None
    tool_context.state[EVIDENCE_KEY] = {
        "cycle_number": tool_response["cycle"]["number"],
        "cycle_goal": tool_response["cycle"]["goal"],
        "open_issues": {issue["id"]: issue for issue in tool_response["open_issues"]},
    }
    return None


def build_recommender(model: BaseLlm | str) -> Agent:
    return Agent(
        name=RECOMMENDER_NAME,
        model=model,
        description=(
            "Recommends what the team should work on next in the current cycle. "
            "Use it whenever the user asks what to work on or pick up next."
        ),
        instruction=INSTRUCTION,
        tools=[get_cycle_status],
        output_schema=RecommenderOutput,
        output_key=OUTPUT_KEY,
        after_tool_callback=capture_evidence,
    )


# --- validation and rendering -------------------------------------------------


def build_recommendation(output: dict[str, Any], evidence: dict[str, Any] | None) -> Recommendation:
    """Turn the recommender's decision into a Recommendation backed by Linear facts.

    Raises RecommendationRejected if the decision can't be trusted.
    """
    if not evidence:
        raise RecommendationRejected("the recommender did not read the cycle")
    if "error" in evidence:
        raise RecommendationRejected(evidence["error"])

    decision = RecommenderOutput.model_validate(output)
    open_issues = evidence["open_issues"]

    candidates = []
    for choice in decision.candidates:
        issue = open_issues.get(choice.issue_id)
        if issue is None or issue["blocked_by"]:
            continue  # not an open issue of this cycle, or blocked: never offered
        candidates.append(
            Candidate(
                issue_id=choice.issue_id,
                title=issue["title"],
                facts=IssueFacts(
                    status=issue["status"],
                    priority=issue["priority"],
                    estimate=issue["estimate"],
                    age_days=issue["age_days"],
                    blocked_by=issue["blocked_by"],
                ),
                rationale=choice.rationale,
            )
        )

    pick = decision.recommended_issue_id
    if pick is not None and pick not in {candidate.issue_id for candidate in candidates}:
        raise RecommendationRejected(
            f"the recommended issue {pick} is not an open, unblocked candidate in this cycle"
        )

    unknowns = list(decision.unknowns)
    if evidence["cycle_goal"] is None and not any("goal" in u.lower() for u in unknowns):
        unknowns.append("The cycle has no goal set in Linear.")

    return Recommendation(
        cycle_number=evidence["cycle_number"],
        candidates=candidates,
        recommended_issue_id=pick,
        blocked={
            issue_id: issue["blocked_by"]
            for issue_id, issue in open_issues.items()
            if issue["blocked_by"]
        },
        unknowns=unknowns,
        approval_question=(
            f'Reply "approve" or "yes, proceed" to approve it. Do you want to proceed with {pick}?'
            if pick
            else "Do you want me to look into what is blocking this work?"
        ),
    )


def render(recommendation: Recommendation) -> str:
    """The recommendation as the user sees it: facts, reasoning and pick kept apart."""
    if not recommendation.candidates:
        lines = [f"Based on Cycle {recommendation.cycle_number}, nothing looks actionable right now.", ""]
    else:
        lines = [f"Based on Cycle {recommendation.cycle_number}, I would consider:", ""]
    for number, candidate in enumerate(recommendation.candidates, start=1):
        facts = candidate.facts
        blocked = ", ".join(facts.blocked_by) or "nothing"
        lines += [
            f"{number}. {candidate.issue_id}: {candidate.title}",
            f"   Linear facts: {facts.status}, priority {facts.priority}, "
            f"estimate {_estimate(facts.estimate)}, {facts.age_days} days old, blocked by {blocked}.",
            f"   Why: {candidate.rationale}",
            "",
        ]
    if recommendation.recommended_issue_id:
        lines.append(f"My recommendation: {recommendation.recommended_issue_id}.")
    else:
        lines.append("My recommendation: nothing is actionable right now.")
    if recommendation.blocked:
        lines += ["", "Not considered, blocked by open work (Linear facts):"] + [
            f"- {issue_id}, blocked by {', '.join(blockers)}"
            for issue_id, blockers in recommendation.blocked.items()
        ]
    if recommendation.unknowns:
        lines += ["", "What I couldn't consider:"] + [f"- {u}" for u in recommendation.unknowns]
    lines += ["", recommendation.approval_question]
    return "\n".join(lines)


def _estimate(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:g}"


# --- root-agent callbacks around the recommender tool --------------------------
#
# The root agent calls the recommender as a tool. These callbacks make that call
# produce a validated, stored Recommendation, and show it to the user exactly as
# stored, without asking the model to paraphrase it.

TURN_KEY = "turn"


def clear_evidence(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> None:
    """Before each recommendation, forget what an earlier run saw."""
    if tool.name == RECOMMENDER_NAME:
        tool_context.state[EVIDENCE_KEY] = None
    return None


def store_recommendation(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> dict | None:
    """Validate the recommender's output and make it the pending recommendation."""
    if tool.name != RECOMMENDER_NAME:
        return None
    # A new recommendation always replaces the previous pending one, even if it fails.
    tool_context.state[PENDING_KEY] = None
    if not isinstance(tool_response, dict):
        return {"message": _unavailable(f"the recommender answered in the wrong shape ({tool_response!r:.80})")}
    try:
        recommendation = build_recommendation(tool_response, tool_context.state.get(EVIDENCE_KEY))
    except (RecommendationRejected, ValueError) as exc:
        return {"message": _unavailable(str(exc))}
    if recommendation.recommended_issue_id:
        tool_context.state[PENDING_KEY] = {
            "recommendation": recommendation.model_dump(),
            "turn": tool_context.state.get(TURN_KEY, 0),
        }
    return {"message": render(recommendation)}


def recommender_failed(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception
) -> dict | None:
    """The recommender itself raised (e.g. its final answer wasn't valid JSON)."""
    if tool.name != RECOMMENDER_NAME:
        return None
    tool_context.state[PENDING_KEY] = None
    return {"message": _unavailable(f"the recommender's answer didn't validate ({type(error).__name__})")}


def present_recommendation(callback_context, llm_request) -> Any:
    """Right after the recommender returns, reply with its rendered text: no model call.

    What the user reads is then exactly what was stored and can be approved.
    """
    from google.adk.models import LlmResponse
    from google.genai import types

    if not llm_request.contents:
        return None
    last = llm_request.contents[-1]
    for part in last.parts or []:
        response = part.function_response
        if response is not None and response.name == RECOMMENDER_NAME:
            message = (response.response or {}).get("message")
            if message:
                return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=message)]))
    return None


def _unavailable(reason: str) -> str:
    return f"I couldn't produce a recommendation: {reason}. Nothing is pending approval."
