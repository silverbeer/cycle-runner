"""The cycle_runner agent.

ADK discovers this module by convention: `adk run` / `adk web` import
`<package>.agent` and look for a module-level variable named `root_agent`.
"""

import os

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

# LiteLLM model string. The `ollama_chat/` prefix routes through Ollama's
# /api/chat endpoint; ADK docs warn that plain `ollama/` can loop on tool calls.
# LiteLLM reaches Ollama at OLLAMA_API_BASE (default http://localhost:11434).
MODEL = os.environ.get("CYCLE_RUNNER_MODEL", "ollama_chat/gemma4:12b")

INSTRUCTION = """\
You are Cycle Runner, the Product Owner and Scrum Master for a small software
team that works in one-week engineering cycles.

Your job is to run that weekly cycle: help plan what goes into it, keep the
backlog prioritised, track progress during the week, surface work that is at
risk, and review what was delivered at the end.

You have one tool, get_cycle_status, which returns the current cycle: its name,
goal, issues with their states, and the issue in focus. Call it whenever an
answer depends on the current cycle. Never guess or invent cycle names, issues,
states or dates; if the tool doesn't say it, you don't know it.

When you answer, keep facts from the tool separate from your own opinion or
advice, so the user can tell which is which.

You can't create or change issues, and you can't do engineering work. If asked
to, say so plainly and offer to talk it through instead. Keep answers short and
practical.
"""


def get_cycle_status() -> dict:
    """Get the current state of this week's engineering cycle.

    Use this whenever you need facts about the current cycle: its name or goal,
    which issues are in it, what state each issue is in, or what the team is
    focused on right now.

    Returns:
        dict: The cycle's name and goal, a list of issues (each with id,
        title and state: Todo, In Progress or Done), and the id of the issue
        currently in focus.
    """
    # Hard-coded for V0.2. A later version will read this from Linear.
    return {
        "cycle": {"name": "Week 39", "goal": "Build the next Cycle Runner milestone"},
        "issues": [
            {"id": "SB-1", "title": "Build ADK foundation", "state": "Done"},
            {"id": "SB-2", "title": "Add ADK tools", "state": "In Progress"},
            {"id": "SB-3", "title": "Add Telegram interface", "state": "Todo"},
        ],
        "current_focus": "SB-2",
    }


root_agent = Agent(
    name="cycle_runner",
    # Extra LiteLlm kwargs are passed straight to litellm.completion().
    # gemma4 "thinks" by default; for plain conversation that's slow and
    # `adk run` prints the whole reasoning trace ahead of the answer.
    model=LiteLlm(model=MODEL, reasoning_effort="none"),
    description="Weekly engineering-cycle Product Owner / Scrum Master.",
    instruction=INSTRUCTION,
    # A plain function is enough: ADK wraps it in a FunctionTool and builds the
    # declaration the model sees from its name, docstring and signature.
    tools=[get_cycle_status],
)
