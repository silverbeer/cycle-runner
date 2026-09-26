import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CommandHandler, MessageHandler

from cycle_runner.telegram_adapter import (
    EMPTY_REPLY,
    ERROR_REPLY,
    TelegramAdapter,
    TelegramConfig,
    adk_identity,
    build_application,
    split_message,
    to_telegram_html,
)

ALLOWED_USER = 111
ALLOWED = frozenset({ALLOWED_USER})


def _update(text="hello", user_id=ALLOWED_USER, chat_id=222):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    return context


class FakeGateway:
    def __init__(self, reply="agent reply", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def handle_message(self, user_id, session_id, message):
        self.calls.append((user_id, session_id, message))
        if self.error:
            raise self.error
        return self.reply


def _sent_texts(update):
    return [call.args[0] for call in update.message.reply_text.await_args_list]


# --- configuration -----------------------------------------------------------


def test_config_reads_token_and_allowed_ids():
    config = TelegramConfig.from_env(
        {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USER_IDS": "1, 22 ,333"}
    )
    assert config.bot_token == "123:abc"
    assert config.allowed_user_ids == {1, 22, 333}


def test_config_without_allowed_ids_allows_nobody():
    config = TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert config.allowed_user_ids == frozenset()


@pytest.mark.parametrize("env", [{}, {"TELEGRAM_BOT_TOKEN": "  "}])
def test_config_requires_a_token(env):
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        TelegramConfig.from_env(env)


def test_config_rejects_non_numeric_user_ids():
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        TelegramConfig.from_env(
            {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USER_IDS": "me"}
        )


def test_config_repr_does_not_leak_the_token():
    config = TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": "123:secret"})
    assert "secret" not in repr(config)


# --- identity mapping --------------------------------------------------------


def test_identity_maps_user_and_chat_to_prefixed_ids():
    assert adk_identity(_update(user_id=111, chat_id=222)) == ("telegram:111", "telegram:222")


def test_identity_is_deterministic_for_the_same_chat():
    first = adk_identity(_update(text="one"))
    second = adk_identity(_update(text="two"))
    assert first == second


def test_identity_differs_per_chat():
    assert adk_identity(_update(chat_id=1))[1] != adk_identity(_update(chat_id=2))[1]


# --- message handling --------------------------------------------------------


def test_passes_received_text_to_gateway_with_mapped_identity():
    gateway = FakeGateway()
    update = _update(text="How is the cycle going?", user_id=ALLOWED_USER, chat_id=222)

    asyncio.run(TelegramAdapter(gateway, ALLOWED).on_message(update, _context()))

    assert gateway.calls == [
        (f"telegram:{ALLOWED_USER}", "telegram:222", "How is the cycle going?")
    ]


def test_sends_gateway_reply_back_through_telegram():
    update = _update()
    context = _context()

    asyncio.run(TelegramAdapter(FakeGateway("All good."), ALLOWED).on_message(update, context))

    assert _sent_texts(update) == ["All good."]
    context.bot.send_chat_action.assert_awaited_once()


def test_rejects_users_not_on_the_allowlist_without_calling_gateway():
    gateway = FakeGateway()
    update = _update(user_id=999)

    asyncio.run(TelegramAdapter(gateway, ALLOWED).on_message(update, _context()))

    assert gateway.calls == []
    (reply,) = _sent_texts(update)
    assert "999" in reply


def test_gateway_failure_becomes_a_polite_error():
    update = _update()
    gateway = FakeGateway(error=RuntimeError("ollama down"))

    asyncio.run(TelegramAdapter(gateway, ALLOWED).on_message(update, _context()))

    assert _sent_texts(update) == [ERROR_REPLY]


def test_empty_gateway_reply_is_replaced():
    update = _update()

    asyncio.run(TelegramAdapter(FakeGateway(""), ALLOWED).on_message(update, _context()))

    assert _sent_texts(update) == [EMPTY_REPLY]


def test_long_replies_are_split_to_fit_telegram():
    update = _update()

    asyncio.run(TelegramAdapter(FakeGateway("x" * 5000), ALLOWED).on_message(update, _context()))

    assert [len(t) for t in _sent_texts(update)] == [4096, 904]


def test_replies_are_sent_as_telegram_html():
    update = _update()

    asyncio.run(TelegramAdapter(FakeGateway("**Done**"), ALLOWED).on_message(update, _context()))

    call = update.message.reply_text.await_args
    assert call.args[0] == "<b>Done</b>"
    assert call.kwargs["parse_mode"] == ParseMode.HTML


def test_falls_back_to_plain_text_when_telegram_rejects_html():
    update = _update()
    update.message.reply_text.side_effect = [BadRequest("can't parse entities"), None]

    asyncio.run(TelegramAdapter(FakeGateway("**Done**"), ALLOWED).on_message(update, _context()))

    assert _sent_texts(update) == ["<b>Done</b>", "**Done**"]
    assert "parse_mode" not in update.message.reply_text.await_args.kwargs


# --- formatting --------------------------------------------------------------


def test_html_formats_a_typical_model_reply():
    reply = (
        "Issues:\n"
        "*   **DEMO-1: Build** (Done)\n"
        "*   **DEMO-2: Tools** (In Progress) — *current focus.*"
    )
    assert to_telegram_html(reply) == (
        "Issues:\n"
        "• <b>DEMO-1: Build</b> (Done)\n"
        "• <b>DEMO-2: Tools</b> (In Progress) — <i>current focus.</i>"
    )


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("- one", "• one"),
        ("  - nested", "  • nested"),
        ("## Status", "<b>Status</b>"),
        ("run `adk web`", "run <code>adk web</code>"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("2*3*4", "2*3*4"),
        ("**unclosed", "**unclosed"),
    ],
)
def test_html_conversion_cases(markdown, expected):
    assert to_telegram_html(markdown) == expected


def test_split_message_keeps_short_text_whole():
    assert split_message("hi") == ["hi"]


def test_start_command_greets_allowed_user_without_calling_gateway():
    gateway = FakeGateway()
    update = _update(text="/start")

    asyncio.run(TelegramAdapter(gateway, ALLOWED).on_start(update, _context()))

    assert gateway.calls == []
    assert len(_sent_texts(update)) == 1


def test_build_application_registers_start_and_text_handlers():
    config = TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": "123:abc"})

    application = build_application(config, FakeGateway())

    handler_types = [type(h) for h in application.handlers[0]]
    assert handler_types == [CommandHandler, MessageHandler]


# --- real Telegram (network) -------------------------------------------------


@pytest.mark.telegram
@pytest.mark.skipif(not os.environ.get("TELEGRAM_BOT_TOKEN"), reason="TELEGRAM_BOT_TOKEN not set")
def test_real_bot_token_is_accepted_by_telegram():
    config = TelegramConfig.from_env()
    application = build_application(config, FakeGateway())

    async def get_me():
        async with application.bot:
            return await application.bot.get_me()

    assert asyncio.run(get_me()).is_bot
