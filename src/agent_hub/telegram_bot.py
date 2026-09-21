"""Telegram channel adapter - polling, no public webhook needed.

Thin by design: translates Telegram <-> the channel-agnostic orchestrator.
No Telegram-specific concept leaks into `orchestrator.py` or `mcp_client.py` -
adding a second channel (the eventual email add-on) means writing a sibling
adapter, not touching this logic.
"""

from __future__ import annotations

import io
import logging
import sys

from anthropic import AsyncAnthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from agent_hub.approvals import ApprovalDecisionError, decide_approval
from agent_hub.config import Settings, get_settings
from agent_hub.health_server import start_health_server
from agent_hub.mcp_client import McpToolHub
from agent_hub.orchestrator import OrchestratorResult, PendingApproval, Status, handle_message

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

_CALLBACK_SEP = ":"


def format_reply(result: OrchestratorResult) -> str:
    emoji = _STATUS_EMOJI[result.status]
    return f"{emoji} {result.text}" if result.text else emoji


def approval_callback_data(action: str, server: str, approval_id: str) -> str:
    return _CALLBACK_SEP.join((action, server, approval_id))


def parse_approval_callback_data(data: str) -> tuple[str, str, str]:
    action, server, approval_id = data.split(_CALLBACK_SEP, 2)
    return action, server, approval_id


def approval_keyboard(pending: PendingApproval) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Freigeben",
                    callback_data=approval_callback_data("approve", pending.server, pending.approval_id),
                ),
                InlineKeyboardButton(
                    "❌ Ablehnen",
                    callback_data=approval_callback_data("reject", pending.server, pending.approval_id),
                ),
            ]
        ]
    )


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
        for pending in result.pending_approvals:
            await message.reply_text(
                f"{_STATUS_EMOJI[Status.PENDING_APPROVAL]} {pending.message}",
                reply_markup=approval_keyboard(pending),
            )

    async def on_approval_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()  # dismiss Telegram's loading spinner regardless of outcome

        chat = update.effective_chat
        if chat is None or chat.id not in allowed_chat_ids:
            logger.warning("ignoring approval callback from unauthorized chat_id=%s", chat.id if chat else None)
            return

        action, server, approval_id = parse_approval_callback_data(query.data)
        server_url = mcp_servers.get(server)
        if server_url is None:
            await query.edit_message_text(f"{_STATUS_EMOJI[Status.ERROR]} Unknown server {server!r}.")
            return

        try:
            decision = await decide_approval(server_url, approval_id, approve=action == "approve")
        except ApprovalDecisionError as exc:
            logger.warning("approval decision failed: chat_id=%s detail=%s", chat.id, exc.detail)
            await query.edit_message_text(f"{_STATUS_EMOJI[Status.ERROR]} Couldn't {action}: {exc.detail}")
            return

        emoji = _STATUS_EMOJI[Status.AUTONOMOUS] if action == "approve" else _STATUS_EMOJI[Status.ERROR]
        verb = "Freigegeben" if action == "approve" else "Abgelehnt"
        await query.edit_message_text(f"{emoji} {verb} ({decision['status']}).")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    application.add_handler(CallbackQueryHandler(on_approval_callback))
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
    # drop_pending_updates: on every restart (redeploy, Cloud Run recycling an
    # instance), start clean rather than replaying a backlog - each message is
    # a stateless one-shot request here, nothing depends on old updates.
    #
    # This call blocks until the polling loop stops, which should only happen
    # via SIGTERM/SIGINT during an orderly shutdown. Telegram's getUpdates
    # allows only one active poller per bot token (see cloudbuild.yaml), so a
    # brief overlap during a redeploy reliably produces 409 Conflict on
    # whichever instance loses the race - if that ever makes run_polling()
    # return on its own instead of via a real shutdown signal, exit non-zero
    # so Cloud Run sees a crashed container and restarts it, instead of
    # silently leaving a dead instance marked healthy (min-instances keeps
    # the container running, but the health probe only checks the port, not
    # whether polling is still alive).
    application.run_polling(drop_pending_updates=True)
    logger.error("run_polling() returned unexpectedly - exiting so Cloud Run restarts this instance")
    sys.exit(1)
