"""Telegram adapter: Telegram in, Telegram out, a gateway in the middle.

Everything Telegram-specific lives here: the bot token, who may talk to the
bot, how a Telegram user/chat becomes an ADK user_id/session_id, the typing
indicator, message length limits and Telegram errors.

Nothing agent-specific lives here. The adapter hands text to any object with

    async def handle_message(user_id: str, session_id: str, message: str) -> str

and sends back whatever it returns. It must never import cycle_runner.agent.
"""

import html
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from telegram import Update
from telegram.constants import ChatAction, MessageLimit, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

log = logging.getLogger(__name__)

EMPTY_REPLY = "Sorry, I don't have an answer for that."
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again."


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "TelegramConfig":
        """Read TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS (comma-separated)."""
        token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")

        raw_ids = environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
        try:
            allowed = frozenset(int(i) for i in raw_ids.split(",") if i.strip())
        except ValueError:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric user ids"
            ) from None
        return cls(bot_token=token, allowed_user_ids=allowed)


def adk_identity(update: Update) -> tuple[str, str]:
    """Map a Telegram update to an ADK (user_id, session_id).

    user_id    = "telegram:<Telegram user id>"   the person
    session_id = "telegram:<Telegram chat id>"   the conversation

    Both ids are stable for the life of a Telegram account/chat, so every
    message in the same chat lands in the same ADK session. The prefix keeps
    them from colliding with ids from other interfaces later.
    """
    return (
        f"telegram:{update.effective_user.id}",
        f"telegram:{update.effective_chat.id}",
    )


def split_message(text: str, limit: int = MessageLimit.MAX_TEXT_LENGTH) -> list[str]:
    """Telegram rejects messages over 4096 characters; send long replies in pieces."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def to_telegram_html(text: str) -> str:
    """Convert the Markdown that models usually write into Telegram's HTML subset.

    Telegram renders only a few tags (<b>, <i>, <code>, ...) and has no lists or
    headings, so bullets become "•" and headings become bold lines. Anything
    unrecognised stays as literal text.
    """
    text = html.escape(text, quote=False)
    lines = []
    for line in text.split("\n"):
        if bullet := re.match(r"^(\s*)[*+-]\s+(.*)$", line):
            line = f"{bullet[1]}• {bullet[2]}"
        elif heading := re.match(r"^#{1,6}\s+(.*)$", line):
            line = f"**{heading[1]}**"
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", text)
    return text


class TelegramAdapter:
    def __init__(self, gateway, allowed_user_ids: frozenset[int]):
        self.gateway = gateway
        self.allowed_user_ids = allowed_user_ids

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_not_allowed(update):
            return
        await update.message.reply_text("Connected. Send a message to talk to the agent.")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._reject_if_not_allowed(update):
            return

        user_id, session_id = adk_identity(update)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            reply = await self.gateway.handle_message(
                user_id, session_id, update.message.text
            )
        except Exception:
            log.exception("gateway failed for session=%s", session_id)
            reply = ERROR_REPLY

        for chunk in split_message(reply or EMPTY_REPLY):
            await self._send(update, chunk)

    async def _send(self, update: Update, text: str) -> None:
        # Telegram's 4096-character limit counts text after tags are parsed, so
        # converting each chunk after splitting keeps it within the limit.
        try:
            await update.message.reply_text(to_telegram_html(text), parse_mode=ParseMode.HTML)
        except BadRequest:
            # Telegram rejected the HTML (e.g. "can't parse entities"): send it raw.
            log.warning("Telegram rejected HTML reply; sending plain text")
            await update.message.reply_text(text)

    async def _reject_if_not_allowed(self, update: Update) -> bool:
        user = update.effective_user
        if user.id in self.allowed_user_ids:
            return False
        log.warning("rejected Telegram user id=%s", user.id)
        await update.message.reply_text(
            f"Sorry, you're not allowed to use this bot. Your Telegram user id is {user.id}."
        )
        return True


async def _log_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Telegram error: %s", context.error, exc_info=context.error)


def build_application(config: TelegramConfig, gateway) -> Application:
    adapter = TelegramAdapter(gateway, config.allowed_user_ids)
    application = Application.builder().token(config.bot_token).build()
    application.add_handler(CommandHandler("start", adapter.on_start))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, adapter.on_message)
    )
    application.add_error_handler(_log_telegram_error)
    return application
