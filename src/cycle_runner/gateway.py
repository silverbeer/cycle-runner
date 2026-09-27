"""Generic agent gateway: one chat message in, one final reply out.

This is the seam between a human interface (Telegram today) and an ADK agent.
It knows about ADK's Runner, sessions and events, and nothing about Telegram or
about what the agent does. It must never import cycle_runner.agent.

Flow of one call to handle_message():

    get or create the session (app_name, user_id, session_id)
        -> runner.run_async(new_message=...)
            -> Event: model text or function_call      (logged)
            -> Event: function_response                 (logged)
            -> Event: final model text                  (logged, returned)
"""

import logging

from google.adk.events import Event
from google.adk.runners import Runner
from google.genai import types

log = logging.getLogger(__name__)


class AgentGateway:
    def __init__(self, runner: Runner):
        self.runner = runner

    async def handle_message(self, user_id: str, session_id: str, message: str) -> str:
        """Run one user turn through the agent and return its final text reply.

        Returns an empty string if the agent produced no final text.
        """
        await self._get_or_create_session(user_id, session_id)

        new_message = types.Content(role="user", parts=[types.Part(text=message)])
        reply = ""
        async for event in self.runner.run_async(
            user_id=user_id, session_id=session_id, new_message=new_message
        ):
            log.info("session=%s %s", session_id, describe_event(event))
            # A callback that only changes state still yields a "final" event with
            # no content, so keep the last final event that actually has text.
            if event.is_final_response() and (text := final_text(event)):
                reply = text
        return reply

    async def _get_or_create_session(self, user_id: str, session_id: str) -> None:
        # Runner.run_async raises if the session doesn't exist. Creating it here
        # (instead of Runner(auto_create_session=True)) keeps the lifecycle visible:
        # the first message from a chat creates it, every later one reuses it.
        sessions = self.runner.session_service
        app_name = self.runner.app_name
        existing = await sessions.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if existing is None:
            await sessions.create_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
            log.info("session=%s created for user=%s", session_id, user_id)


def final_text(event: Event) -> str:
    """The user-facing text of an event, leaving out any model reasoning parts."""
    if not event.content or not event.content.parts:
        return ""
    return "".join(p.text for p in event.content.parts if p.text and not p.thought)


def describe_event(event: Event) -> str:
    """One short log line per event: who produced it and what kind it is."""
    calls = [c.name for c in event.get_function_calls()]
    responses = [r.name for r in event.get_function_responses()]
    if calls:
        kind = f"function_call {calls}"
    elif responses:
        kind = f"function_response {responses}"
    else:
        kind = f"text ({len(final_text(event))} chars)"
    if event.is_final_response():
        kind += " [final]"
    return f"author={event.author} {kind}"
