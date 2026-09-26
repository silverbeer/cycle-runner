import asyncio
import inspect

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool

from cycle_runner.agent import MODEL, get_cycle_status, root_agent


def test_root_agent_is_an_llm_agent_named_cycle_runner():
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "cycle_runner"


def test_root_agent_uses_ollama_through_litellm():
    assert isinstance(root_agent.model, LiteLlm)
    assert root_agent.model.model == MODEL
    assert MODEL.startswith("ollama_chat/")


def test_instruction_sets_role_and_limits():
    instruction = root_agent.instruction
    assert "Product Owner" in instruction
    assert "Scrum Master" in instruction
    assert "one-week" in instruction
    assert "get_cycle_status" in instruction
    assert "Never guess or invent" in instruction


def test_get_cycle_status_returns_a_consistent_cycle():
    status = get_cycle_status()

    assert status["cycle"]["name"]
    assert status["cycle"]["goal"]
    assert status["issues"]
    for issue in status["issues"]:
        assert issue.keys() == {"id", "title", "state"}
        assert issue["state"] in {"Todo", "In Progress", "Done"}
    assert status["current_focus"] in {issue["id"] for issue in status["issues"]}


def test_root_agent_registers_get_cycle_status_and_nothing_else():
    assert root_agent.tools == [get_cycle_status]
    assert root_agent.sub_agents == []


def test_tool_declaration_is_built_from_function_name_and_docstring():
    # canonical_tools() is how ADK resolves `tools=[...]` before each model call.
    (tool,) = asyncio.run(root_agent.canonical_tools())
    assert isinstance(tool, FunctionTool)

    declaration = tool._get_declaration()
    assert declaration.name == "get_cycle_status"
    assert declaration.description == inspect.getdoc(get_cycle_status)
    assert declaration.parameters is None


def _run_live(message):
    runner = InMemoryRunner(agent=root_agent, app_name="cycle_runner")
    return asyncio.run(runner.run_debug(message, quiet=True))


@pytest.mark.ollama
def test_agent_replies_using_local_model(ollama):
    events = _run_live("In one sentence, what is your role?")

    reply = "".join(
        part.text
        for event in events
        if event.author == "cycle_runner" and event.content
        for part in event.content.parts
        if part.text
    )
    assert reply.strip()


@pytest.mark.ollama
def test_model_chooses_to_call_the_tool_and_uses_its_result(ollama):
    events = _run_live("What is the status of my cycle?")

    calls = [call.name for event in events for call in event.get_function_calls()]
    responses = [
        response.name
        for event in events
        for response in event.get_function_responses()
    ]
    assert calls == ["get_cycle_status"]
    assert responses == ["get_cycle_status"]

    final = events[-1]
    assert final.is_final_response()
    reply = "".join(part.text for part in final.content.parts if part.text)
    assert "Week 39" in reply
    assert "DEMO-2" in reply
