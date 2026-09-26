"""Run Cycle Runner behind Telegram: `uv run python -m cycle_runner`.

This is the only module that knows both sides. It picks the agent, builds the
ADK Runner and in-memory SessionService around it, and hands the resulting
gateway to the Telegram adapter.
"""

import logging
import os

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from cycle_runner.agent import root_agent
from cycle_runner.gateway import AgentGateway
from cycle_runner.store import db_path_from_env
from cycle_runner.telegram_adapter import TelegramConfig, build_application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    # httpx logs every Telegram API URL at INFO, and those URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = TelegramConfig.from_env()

    # Sessions live in this process's memory: restart the bot and every
    # conversation starts over.
    runner = Runner(
        app_name="cycle_runner",
        agent=root_agent,
        session_service=InMemorySessionService(),
    )
    application = build_application(config, AgentGateway(runner))

    log = logging.getLogger(__name__)
    # Application state lives here and outlives this process; sessions don't.
    log.info("cycle store: %s", os.path.abspath(db_path_from_env()))
    log.info("polling Telegram; allowed user ids: %s", sorted(config.allowed_user_ids))
    # drop_pending_updates: ignore messages sent while the bot was down.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
