"""Telegram channel adapter - polling, no public webhook needed.

Thin by design: translates Telegram <-> the channel-agnostic orchestrator.
No Telegram-specific concept leaks into `orchestrator.py` or `mcp_client.py` -
adding a second channel (the eventual email add-on) means writing a sibling
adapter, not touching this logic.
"""

from __future__ import annotations

import io
import logging

from anthropic import AsyncAnthropic
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from agent_hub.config import Settings, get_settings
from agent_hub.health_server import start_health_server
from agent_hub.mcp_client import McpToolHub
from agent_hub.orchestrator import OrchestratorResult, Status, handle_message

logger = logging.getLogger(__name__)

# The one-glance trust signal from orchestrator.Status, in the one UI surface
# Telegram actually gives a bot: coloured emoji. Same L4-autonomy idea the
# eventual email add-on's own colour palette will reuse - see
# orchestrator.Status's docstring for what each level means.
_STATUS_EMOJI = {
    Status.AUTONOMOUS: "\U0001f7e2",  # green circle
    Status.PENDING_APPROVAL: "\U0001f7e1",  # yellow circle
    Status.ERROR: "\U0001f534",  # red circle
}


def format_reply(result: OrchestratorResult) -> str:
    emoji = _STATUS_EMOJI[result.status]
    return f"{emoji} {result.text}" if result.text else emoji


def build_application(settings: Settings) -> Application:
    allowed_chat_ids = settings.parsed_allowed_chat_ids()
    mcp_servers = settings.parsed_mcp_servers()
    claude = AsyncAnthropic()

    async def on_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.message
        if chat is None or message is None or not message.text:
            return
        if chat.id not in allowed_chat_ids:
            logger.warning("ignoring message from unauthorized chat_id=%s", chat.id)
            return

        try:
            async with McpToolHub(mcp_servers) as tool_hub:
                result = await handle_message(message.text, tool_hub, claude, settings.claude_model)
        except Exception:
            logger.exception("failed to handle message from chat_id=%s", chat.id)
            await message.reply_text("Sorry, something went wrong answering that. Try again shortly.")
            return

        await message.reply_text(format_reply(result))
        for attachment in result.attachments:
            await message.reply_document(
                document=io.BytesIO(attachment.data),
                filename=attachment.filename,
            )

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return application


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx (python-telegram-bot's HTTP client) logs full request URLs at INFO,
    # and Telegram's API puts the bot token directly in the URL path - never
    # let that reach Cloud Logging. Everything else stays at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = get_settings()
    start_health_server(settings.port)  # Cloud Run's health probe only; not a real API
    application = build_application(settings)
    logger.info(
        "agent-hub starting: %d authorized chat(s), MCP servers: %s",
        len(settings.parsed_allowed_chat_ids()),
        ", ".join(settings.parsed_mcp_servers()),
    )
    application.run_polling()
