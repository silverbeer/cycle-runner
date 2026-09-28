"""Structured "what should we work on next?" recommendations.

The recommender is a small ADK agent with an output_schema. The root agent
calls it through AgentTool, so the conversation stays with the root agent
and only this one step is forced into a schema.

Code, not the recommender, reads the cycle: a before_tool_callback calls
get_cycle_status and puts the result in session state, and the recommender's
instruction receives it through ADK's {state_key} templating. With no tools,
ADK passes the schema to the model as a response format, which Ollama enforces
while decoding. (With tools, ADK has to fall back to a set_model_response tool
call, and gemma4:12b produced unparseable, empty or wrong-key output in about
a fifth of runs.)

Facts and judgment are kept apart by construction:
- the recommender model writes only RecommenderOutput: which issues, why, and
  what it couldn't know;
- code then builds the stored Recommendation, copying titles and facts from
  the same cycle data, and never passing on a pick that isn't an open,
  unblocked issue of that cycle.

Nothing here writes to Linear or starts any work.
"""

import logging
import uuid
from typing import Any

from google.adk.agents import Agent
from google.adk.models import BaseLlm, LlmResponse
from google.adk.tools import BaseTool, ToolContext
from google.genai import types
from pydantic import BaseModel, Field

from cycle_runner.linear_tools import get_cycle_status

log = logging.getLogger(__name__)

RECOMMENDER_NAME = "recommend_next_work"
EVIDENCE_KEY = "recommender_evidence"
OUTPUT_KEY = "recommender_output"
PENDING_KEY = "pending_recommendation"


# --- what the recommender model writes ----------------------------------------


class CandidateChoice(BaseModel):
    issue_id: str = Field(description="An open issue id from the cycle, like SB-123.")
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
    project: str | None = None  # Linear's repo label: which project the work belongs to


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
    corrections: list[str]  # where code overruled the recommender, and why
    unknowns: list[str]
    approval_question: str


class RecommendationRejected(Exception):
    """The recommender's output didn't survive validation against Linear's facts."""


# --- the recommender agent ----------------------------------------------------

INSTRUCTION = """\
You choose what a one-week engineering team should work on next. This is the
current cycle, as read from Linear:

{recommender_evidence}

Weigh the open issues. No single factor decides:
- In progress: finishing started work usually beats starting new work.
- blocked_by: an issue blocked by open work is not actionable and must not be
  recommended or listed as a candidate; its blocker might be a better pick.
- Priority, estimate and days_remaining: what fits the time left.
- age_days and labels, as supporting evidence.

Give up to three candidates, each an open, unblocked issue id with your
rationale. If nothing is actionable, give no candidates.

Answer with exactly these keys: "candidates" (a list of objects with
"issue_id" and "rationale"), "recommended_issue_id", and "unknowns".
recommended_issue_id must be exactly one of your candidates' ids, or null if
nothing is actionable. List in unknowns what the cycle data doesn't record
that would matter; if the cycle's goal is null, say the cycle has no goal set.
Use only ids that appear in the cycle above.
"""


def build_recommender(model: BaseLlm | str) -> Agent:
    return Agent(
        name=RECOMMENDER_NAME,
        model=model,
        description=(
            "Recommends what the team should work on next in the current cycle. "
            "Use it whenever the user asks what to work on or pick up next."
        ),
        instruction=INSTRUCTION,  # {recommender_evidence} is filled from session state
        output_schema=RecommenderOutput,
        output_key=OUTPUT_KEY,
    )


# --- validation and rendering -------------------------------------------------


def build_recommendation(output: dict[str, Any], cycle: dict[str, Any] | None) -> Recommendation:
    """Turn the recommender's decision into a Recommendation backed by Linear facts.

    cycle is the get_cycle_status result the recommender was given.
    Raises RecommendationRejected if there are no facts to check it against.
    """
    if not cycle:
        raise RecommendationRejected("the cycle was not read")
    if "error" in cycle:
        raise RecommendationRejected(cycle["error"])

    decision = RecommenderOutput.model_validate(output)
    open_issues = {issue["id"]: issue for issue in cycle["open_issues"]}

    candidates, corrections = [], []
    for choice in decision.candidates:
        issue = open_issues.get(choice.issue_id)
        if problem := _not_offerable(choice.issue_id, issue):
            corrections.append(f"{choice.issue_id} was suggested, but {problem}, so it isn't offered.")
            continue
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
                    project=issue.get("project"),
                ),
                rationale=choice.rationale,
            )
        )

    pick = decision.recommended_issue_id
    if pick is not None and pick not in {candidate.issue_id for candidate in candidates}:
        # Never pass on a pick that isn't an open, unblocked candidate. Say so instead.
        problem = _not_offerable(pick, open_issues.get(pick)) or "it wasn't among the candidates"
        corrections = [c for c in corrections if not c.startswith(f"{pick} ")]
        corrections.append(f"{pick} was picked, but {problem}, so it isn't recommended.")
        pick = None

    unknowns = list(decision.unknowns)
    if cycle["cycle"]["goal"] is None and not any("goal" in u.lower() for u in unknowns):
        unknowns.append("The cycle has no goal set in Linear.")

    return Recommendation(
        cycle_number=cycle["cycle"]["number"],
        candidates=candidates,
        recommended_issue_id=pick,
        blocked={
            issue_id: issue["blocked_by"]
            for issue_id, issue in open_issues.items()
            if issue["blocked_by"]
        },
        corrections=corrections,
        unknowns=unknowns,
        approval_question=(
            f'Reply "approve" or "yes, proceed" to approve it. Do you want to proceed with {pick}?'
            if pick
            else "Do you want me to look into what is blocking this work?"
        ),
    )


def _not_offerable(issue_id: str, issue: dict | None) -> str | None:
    """Why an issue can't be offered, from Linear's facts, or None if it can."""
    if issue is None:
        return "it isn't an open issue in this cycle"
    if issue["blocked_by"]:
        return f"Linear shows it blocked by {', '.join(issue['blocked_by'])}"
    return None


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
    if recommendation.corrections:
        lines += ["", "Corrected by Cycle Runner:"] + [f"- {c}" for c in recommendation.corrections]
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


async def read_cycle_for_recommender(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Before the recommender runs, read the cycle and hand it over via session state.

    AgentTool copies the caller's state into the recommender's session, where
    {recommender_evidence} in its instruction picks it up. The same data is
    later used to check the recommender's answer.
    """
    if tool.name != RECOMMENDER_NAME:
        return None
    # A new recommendation always replaces the previous pending one, even if it fails.
    tool_context.state[PENDING_KEY] = None
    cycle = await get_cycle_status()
    tool_context.state[EVIDENCE_KEY] = cycle
    if "error" in cycle:
        return {"message": _unavailable(cycle["error"])}  # skips the recommender entirely
    return None


def store_recommendation(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> dict | None:
    """Validate the recommender's output and make it the pending recommendation."""
    if tool.name != RECOMMENDER_NAME:
        return None
    if isinstance(tool_response, dict) and "message" in tool_response:
        return None  # already a final message (the cycle couldn't be read)
    # A real RecommenderOutput always has "candidates" (possibly empty). Anything
    # else is a failure: when the recommender's run fails, AgentTool returns its
    # error text, which ADK wraps as {"result": "..."}. Treating that as valid
    # output would turn a failure into "nothing is actionable".
    if not isinstance(tool_response, dict) or "candidates" not in tool_response:
        log.warning("recommender output rejected: %.300r", tool_response)
        return {"message": _unavailable("the recommender's answer didn't match the expected structure")}
    try:
        recommendation = build_recommendation(tool_response, tool_context.state.get(EVIDENCE_KEY))
    except (RecommendationRejected, ValueError) as exc:
        return {"message": _unavailable(str(exc))}
    if recommendation.recommended_issue_id:
        tool_context.state[PENDING_KEY] = {
            "recommendation": recommendation.model_dump(),
            "turn": tool_context.state.get(TURN_KEY, 0),
            # Identifies this one recommendation. Approving it creates at most one
            # work request (the store enforces UNIQUE on it), however often the
            # approval is processed.
            "recommendation_id": str(uuid.uuid4()),
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


def present_recommendation(callback_context, llm_request) -> LlmResponse | None:
    """before_model_callback: right after the recommender returns, answer without the model.

    Why this bypass is required: without it, the root model would read the
    recommender's result and write its own reply. It could paraphrase the
    candidates, drop the corrections, or even name a different issue, and then
    what the user reads would no longer match the stored Recommendation that
    "yes" approves. Replying with render()'s text guarantees that the user sees
    exactly what can be approved. (It also saves a model call: about 20s.)

    Every other model call goes ahead untouched.
    """
    message = _rendered_recommendation(llm_request)
    return _reply_without_model(message) if message else None


def _rendered_recommendation(llm_request) -> str | None:
    """The rendered text, if the latest content is the recommender tool's result."""
    if not llm_request.contents:
        return None
    for part in llm_request.contents[-1].parts or []:
        response = part.function_response
        if response is not None and response.name == RECOMMENDER_NAME:
            return (response.response or {}).get("message")
    return None


def _reply_without_model(text: str) -> LlmResponse:
    """A model response that no model produced. The only place this module fakes one."""
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _unavailable(reason: str) -> str:
    return f"I couldn't produce a recommendation: {reason.rstrip('.')}. Nothing is pending approval."
