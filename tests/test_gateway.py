import asyncio

import pytest
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from cycle_runner.agent import root_agent
from cycle_runner.gateway import AgentGateway


def _event(*parts: types.Part, role: str = "model") -> Event:
    return Event(author="fake_agent", content=types.Content(role=role, parts=list(parts)))


TOOL_TURN = [
    _event(types.Part(function_call=types.FunctionCall(name="get_thing", args={}))),
    _event(
        types.Part(
            function_response=types.FunctionResponse(
                name="get_thing", response={"thing": 1}
            )
        ),
        role="user",
    ),
    _event(types.Part(text="thinking out loud", thought=True), types.Part(text="Thing is 1.")),
]


class FakeRunner:
    """Stands in for google.adk.runners.Runner: a real session service, scripted events."""

    def __init__(self, events: list[Event]):
        self.app_name = "fake_app"
        self.session_service = InMemorySessionService()
        self.events = events
        self.calls = []

    async def run_async(self, *, user_id, session_id, new_message):
        # The real Runner raises if the session doesn't exist; so does the fake.
        session = await self.session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        assert session is not None, "gateway must create the session before running"
        self.calls.append((user_id, session_id, new_message))
        for event in self.events:
            yield event


def _session_ids(runner, user_id):
    listing = asyncio.run(
        runner.session_service.list_sessions(app_name=runner.app_name, user_id=user_id)
    )
    return [s.id for s in listing.sessions]


def test_returns_only_the_final_text_not_tool_events_or_thoughts():
    gateway = AgentGateway(FakeRunner(TOOL_TURN))

    reply = asyncio.run(gateway.handle_message("u1", "s1", "what's the thing?"))

    assert reply == "Thing is 1."


def test_sends_the_message_as_user_content_with_the_given_ids():
    runner = FakeRunner(TOOL_TURN)

    asyncio.run(AgentGateway(runner).handle_message("u1", "s1", "hello"))

    ((user_id, session_id, content),) = runner.calls
    assert (user_id, session_id) == ("u1", "s1")
    assert content.role == "user"
    assert content.parts[0].text == "hello"


def test_creates_the_session_once_and_reuses_it():
    runner = FakeRunner(TOOL_TURN)
    gateway = AgentGateway(runner)

    async def two_messages():
        await gateway.handle_message("u1", "s1", "first")
        await gateway.handle_message("u1", "s1", "second")

    asyncio.run(two_messages())

    assert _session_ids(runner, "u1") == ["s1"]
    assert [call[1] for call in runner.calls] == ["s1", "s1"]


def test_different_session_ids_get_different_sessions():
    runner = FakeRunner(TOOL_TURN)
    gateway = AgentGateway(runner)

    async def two_chats():
        await gateway.handle_message("u1", "chat-a", "hi")
        await gateway.handle_message("u1", "chat-b", "hi")

    asyncio.run(two_chats())

    assert sorted(_session_ids(runner, "u1")) == ["chat-a", "chat-b"]


def test_returns_empty_string_when_agent_has_no_final_text():
    tool_call_only = TOOL_TURN[:1]

    reply = asyncio.run(AgentGateway(FakeRunner(tool_call_only)).handle_message("u", "s", "hi"))

    assert reply == ""


@pytest.mark.ollama
def test_three_message_conversation_shares_one_session(ollama, fake_linear):
    runner = Runner(
        app_name="cycle_runner",
        agent=root_agent,
        session_service=InMemorySessionService(),
    )
    gateway = AgentGateway(runner)
    user_id, session_id = "telegram:1", "telegram:1"
    messages = ["What is my role?", "What are we working on?", "How is the cycle going?"]

    async def conversation():
        replies = [await gateway.handle_message(user_id, session_id, m) for m in messages]
        session = await runner.session_service.get_session(
            app_name="cycle_runner", user_id=user_id, session_id=session_id
        )
        return replies, session

    replies, session = asyncio.run(conversation())

    assert all(reply.strip() for reply in replies)
    user_texts = [
        e.content.parts[0].text
        for e in session.events
        if e.author == "user" and e.content.parts[0].text
    ]
    assert user_texts == messages

    # Everything after the third user message belongs to the third turn.
    third_turn = session.events[
        max(i for i, e in enumerate(session.events) if e.author == "user") :
    ]
    assert "get_cycle_status" in [
        call.name for e in third_turn for call in e.get_function_calls()
    ]
