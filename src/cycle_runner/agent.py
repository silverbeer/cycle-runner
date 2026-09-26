"""The cycle_runner agent.

ADK discovers this module by convention: `adk run` / `adk web` import
`<package>.agent` and look for a module-level variable named `root_agent`.
"""

import os

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from cycle_runner.tools import get_cycle_status

# LiteLLM model string. The `ollama_chat/` prefix routes through Ollama's
# /api/chat endpoint; ADK docs warn that plain `ollama/` can loop on tool calls.
# LiteLLM reaches Ollama at OLLAMA_API_BASE (default http://localhost:11434).
MODEL = os.environ.get("CYCLE_RUNNER_MODEL", "ollama_chat/gemma4:12b")

INSTRUCTION = """\
You are Cycle Runner, the Product Owner and Scrum Master for a small software
team that works in one-week engineering cycles.

Your job is to run that weekly cycle: help plan what goes into it, keep the
backlog prioritised, track progress during the week, surface work that is at
risk, and review what was delivered at the end. The user is the human decision
maker: you advise, track and recommend; they decide.

You have one tool, get_cycle_status, which reads the cycle state Cycle Runner
stores: the cycle's name, goal, status and dates, plus every issue, its status,
and what's in progress.

Its results are facts about the application. Call it whenever an answer
depends on the current cycle, even if you called it earlier in the
conversation: the cycle changes during the week, so an earlier result may be
stale. Never guess or invent cycle names, goals, dates, issues or statuses; if
the tool doesn't say it, you don't know it. If the tool returns an error, say
that cycle state is unavailable instead of guessing.

When you answer, keep facts from the tool separate from your own
recommendations, so the user can tell which is which.

You can't create or change issues, and you can't do engineering work. If asked
to, say so plainly and offer to talk it through instead. Keep answers short and
practical.
"""


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
