"""Telegram channel adapter - long polling, or a webhook when
TELEGRAM_WEBHOOK_URL is set (so the service can scale to zero).

Thin by design: translates Telegram <-> the channel-agnostic orchestrator.
No Telegram-specific concept leaks into `orchestrator.py` or `mcp_client.py` -
adding a second channel (the eventual email add-on) means writing a sibling
adapter, not touching this logic.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sys

from anthropic import AsyncAnthropic
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from agent_hub import state
from agent_hub.approvals import ApprovalDecisionError, decide_approval, post_approval_route
from agent_hub.config import Settings, get_settings
from agent_hub.errors import user_message
from agent_hub.health_server import start_health_server
from agent_hub.mcp_client import McpToolHub
from agent_hub.telegram_form import serve_webhook
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

# 0 = report on every reply, including a plain conversational one with no
# tool calls - deliberately simple for now (explicit choice, not yet tuned):
# ahead of scheduled/ERP work where cost matters more, blanket visibility
# beats guessing at a "longer action" threshold. Raise this later if
# per-message cost lines turn out to be more noise than signal.
MIN_TOOL_CALLS_FOR_COST_REPORT = 0


def format_reply(result: OrchestratorResult) -> str:
    emoji = _STATUS_EMOJI[result.status]
    return f"{emoji} {result.text}" if result.text else emoji


def format_cost_line(result: OrchestratorResult) -> str | None:
    """A short, separate follow-up message with this turn's approximate
    cost, sent after every reply (see MIN_TOOL_CALLS_FOR_COST_REPORT) -
    None only when the configured model isn't in pricing.py's table
    (cost_usd is None there means "unknown", never "free" - reporting
    nothing is correct, reporting 0.00ct would be a lie). In cents,
    not dollars - a single reply's cost is normally well under $0.01, and
    "0.34ct" reads better than "$0.0034". "Robert" (not the tenant) always
    pays: every tenant's usage is billed to the same Anthropic account, on
    this bot's owner's card, whichever chat/tenant triggered it - see
    README's "Cost"."""
    if result.tool_call_count < MIN_TOOL_CALLS_FOR_COST_REPORT or result.cost_usd is None:
        return None
    return f"Robert zahlt: {result.cost_usd * 100:.2f}ct"


def approval_callback_data(action: str, server: str, approval_id: str) -> str:
    return _CALLBACK_SEP.join((action, server, approval_id))


def parse_approval_callback_data(data: str) -> tuple[str, str, str]:
    action, server, approval_id = data.split(_CALLBACK_SEP, 2)
    return action, server, approval_id


def approval_keyboard(pending: PendingApproval, form_base: str | None = None) -> InlineKeyboardMarkup:
    if pending.compact:
        return tile_keyboard(pending.server, pending.approval_id, form_base)
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


# A compact approval (bauakte-mcp) is a tile that is edited in place: the
# folder can be changed with a tap or a typed answer, and a rejection asks why.
# Its callback actions: "approve", "nein" (reject, then ask why), "ordner"
# (show folder choices), "o" (one of them; the folder number rides along after
# "|"), "frei" (type a folder), "zurueck" (back to the tile's buttons).
_FOLDER_NR = re.compile(r"^\s*([PBKZ])\s?(\d+(?:\.\d+)*)", re.IGNORECASE)


def tile_keyboard(server: str, approval_id: str, form_base: str | None = None) -> InlineKeyboardMarkup:
    """With a public URL (webhook mode) the tile opens the editable form
    (telegram_form.py); without one, the folder is changed with buttons."""
    decide = [
        InlineKeyboardButton("✅ Freigeben", callback_data=approval_callback_data("approve", server, approval_id)),
        InlineKeyboardButton("❌ Ablehnen", callback_data=approval_callback_data("nein", server, approval_id)),
    ]
    if form_base:
        edit = InlineKeyboardButton("📝 Vorschlag bearbeiten",
                                    web_app=WebAppInfo(url=f"{form_base.rstrip('/')}/kachel?id={approval_id}"))
        return InlineKeyboardMarkup([[edit], decide])
    return InlineKeyboardMarkup([
        decide,
        [InlineKeyboardButton("📁 Ordner ändern", callback_data=approval_callback_data("ordner", server, approval_id))],
    ])


def folder_keyboard(server: str, approval_id: str, options: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for option in options:
        match = _FOLDER_NR.match(option)
        if match:  # callback_data is at most 64 bytes: only the number travels
            nummer = f"{match[1].upper()} {match[2]}"
            rows.append([InlineKeyboardButton(option[:60], callback_data=approval_callback_data(
                "o", server, f"{approval_id}|{nummer}"))])
    rows.append([InlineKeyboardButton("✏️ Anderer Ordner", callback_data=approval_callback_data("frei", server, approval_id))])
    rows.append([InlineKeyboardButton("↩️ Zurück", callback_data=approval_callback_data("zurueck", server, approval_id))])
    return InlineKeyboardMarkup(rows)


def format_pending(pending: PendingApproval) -> str:
    """Telegram has no editable card. A compact approval shows only its
    message (and is edited with the buttons); otherwise the fields are listed
    read-only under the message, and a correction is a chat reply instead,
    which the agent turns into a new proposal."""
    lines = [f"{_STATUS_EMOJI[Status.PENDING_APPROVAL]} {pending.message}"]
    if not pending.compact:
        lines += [f"{key}: {value}" for key, value in pending.payload.items() if value]
    return "\n".join(lines)


ORDNER_FRAGE = (
    "Welcher Ordner? Die Nummer wie „B 5.1“, mit Unterordner wie „B 5.2/Firmen“, "
    "oder ein Name wie „Kontrollmessungen“. /weiter bricht ab."
)
GRUND_FRAGE = (
    "Warum war der Vorschlag falsch? Schreib es kurz, ich merke es mir als Regel "
    "für die nächsten Ablagen. /weiter = ohne Begründung."
)


RESUME_PROMPT = (
    "[System] A human just approved pending approval {approval_id}. Proceed now to "
    "complete the corresponding action (call the matching execute/send tool for it) "
    "and confirm briefly once done."
)


def build_application(settings: Settings) -> Application:
    chat_tenants = settings.parsed_chat_tenants()  # chat_id -> tenant_id; also the access-control list (its keys)
    mcp_servers = settings.parsed_mcp_servers()
    claude = AsyncAnthropic()
    # In-memory only, keyed by chat_id. Fine because this process is a
    # singleton (see cloudbuild.yaml's --max-instances=1) - there is never a
    # second instance that wouldn't share this state. Lost on restart, same
    # as drop_pending_updates already accepts for the Telegram side.
    chat_history: dict[int, list[dict]] = {}
    form_base = settings.telegram_webhook_url

    async def respond(message, chat_id: int, tenant_id: str, text: str) -> None:
        """One agent turn for this chat, and its reply, files and approval
        prompts posted below `message`."""
        try:
            async with McpToolHub(mcp_servers, tenant_id=tenant_id) as tool_hub:
                result, history = await handle_message(
                    text,
                    tool_hub,
                    claude,
                    settings.claude_model,
                    chat_history.get(chat_id),
                    effort=settings.claude_effort,
                )
            chat_history[chat_id] = history
        except Exception as exc:
            logger.exception("failed to handle message from chat_id=%s", chat_id)
            await message.reply_text(user_message(exc))
            return

        if result.text or not result.pending_approvals:
            await message.reply_text(format_reply(result))
        for attachment in result.attachments:
            await message.reply_document(
                document=io.BytesIO(attachment.data),
                filename=attachment.filename,
            )
        for pending in result.pending_approvals:
            tile = await message.reply_text(format_pending(pending), reply_markup=approval_keyboard(pending, form_base))
            if pending.compact:
                # For "Ordner ändern" and the form later - this service scales to zero, so not in memory.
                state.save("kachel", pending.approval_id, {
                    "options": pending.options, "server": pending.server, "chat_id": chat_id,
                    "message_id": getattr(tile, "message_id", None)})
        cost_line = format_cost_line(result)
        if cost_line is not None:
            await message.reply_text(cost_line)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.message
        if chat is None or message is None or not message.text:
            return
        tenant_id = chat_tenants.get(chat.id)
        if tenant_id is None:
            logger.warning("ignoring message from unauthorized chat_id=%s", chat.id)
            return
        # The answer to a tile's question (which folder / why rejected) goes
        # straight to the server, not to the agent.
        eingabe = state.load("eingabe", str(chat.id))
        if eingabe:
            state.save("eingabe", str(chat.id), None)
            if message.text.strip() == "/weiter":
                await message.reply_text("Ok.")
                return
            if not message.text.startswith("/"):
                await tile_answer(context, message, chat.id, tenant_id, eingabe)
                return
        # Commands ("/eingang") go to the agent as they are: what they mean is
        # written in the playbooks, not hardcoded here.
        await respond(message, chat.id, tenant_id, message.text)

    async def tile_answer(context, message, chat_id: int, tenant_id: str, eingabe: dict) -> None:
        server_url = mcp_servers.get(eingabe["server"])
        if server_url is None:
            return
        try:
            if eingabe["art"] == "ordner":
                antwort = await post_approval_route(server_url, eingabe["approval_id"], tenant_id, "ordner",
                                                    {"ordner": message.text})
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=eingabe["message_id"],
                    text=f"{_STATUS_EMOJI[Status.PENDING_APPROVAL]} {antwort['message']}",
                    reply_markup=tile_keyboard(eingabe["server"], eingabe["approval_id"], form_base),
                )
                await message.reply_text("📁 Ordner geändert - oben freigeben.")
            else:
                antwort = await post_approval_route(server_url, eingabe["approval_id"], tenant_id, "begruendung",
                                                    {"text": message.text})
                await message.reply_text(f"📝 Gemerkt: „{antwort['regel']}“")
        except ApprovalDecisionError as exc:
            logger.warning("tile answer failed: chat_id=%s detail=%s", chat_id, exc.detail)
            if eingabe["art"] == "ordner" and exc.status_code == 400:
                state.save("eingabe", str(chat_id), eingabe)  # let them try again
                await message.reply_text(f"{_STATUS_EMOJI[Status.ERROR]} {exc.detail}\nNochmal versuchen oder /weiter.")
            else:
                await message.reply_text(f"{_STATUS_EMOJI[Status.ERROR]} {exc.detail}")

    async def on_approval_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()  # dismiss Telegram's loading spinner regardless of outcome

        chat = update.effective_chat
        if chat is None:
            return
        tenant_id = chat_tenants.get(chat.id)
        if tenant_id is None:
            logger.warning("ignoring approval callback from unauthorized chat_id=%s", chat.id)
            return

        action, server, approval_id = parse_approval_callback_data(query.data)
        approval_id, _, nummer = approval_id.partition("|")
        server_url = mcp_servers.get(server)
        if server_url is None:
            await query.edit_message_text(f"{_STATUS_EMOJI[Status.ERROR]} Unknown server {server!r}.")
            return

        if action == "ordner":
            kachel = state.load("kachel", approval_id) or {}
            await query.edit_message_reply_markup(folder_keyboard(server, approval_id, kachel.get("options", [])))
            return
        if action == "zurueck":
            await query.edit_message_reply_markup(tile_keyboard(server, approval_id, form_base))
            return
        if action == "frei":
            state.save("eingabe", str(chat.id), {"art": "ordner", "server": server, "approval_id": approval_id,
                                                 "message_id": query.message.message_id})
            await query.edit_message_reply_markup(tile_keyboard(server, approval_id, form_base))
            await query.message.reply_text(ORDNER_FRAGE, reply_markup=ForceReply(selective=True))
            return
        if action == "o":
            try:
                antwort = await post_approval_route(server_url, approval_id, tenant_id, "ordner", {"ordner": nummer})
            except ApprovalDecisionError as exc:
                await query.message.reply_text(f"{_STATUS_EMOJI[Status.ERROR]} {exc.detail}")
                return
            await query.edit_message_text(f"{_STATUS_EMOJI[Status.PENDING_APPROVAL]} {antwort['message']}",
                                          reply_markup=tile_keyboard(server, approval_id, form_base))
            return

        approve = action == "approve"
        try:
            await decide_approval(server_url, approval_id, tenant_id, approve=approve)
        except ApprovalDecisionError as exc:
            logger.warning("approval decision failed: chat_id=%s detail=%s", chat.id, exc.detail)
            if exc.status_code == 409:
                await query.edit_message_text(f"{query.message.text}\n\nℹ️ Schon erledigt.")
            else:
                await query.message.reply_text(f"{_STATUS_EMOJI[Status.ERROR]} {exc.detail}")
            return

        emoji = _STATUS_EMOJI[Status.AUTONOMOUS] if approve else _STATUS_EMOJI[Status.ERROR]
        verb = "Freigegeben" if approve else "Abgelehnt"
        tile = query.message.text.removeprefix(_STATUS_EMOJI[Status.PENDING_APPROVAL]).strip()
        await query.edit_message_text(f"{tile}\n\n{emoji} {verb}.")
        if approve:
            # Same as Teams: without this, approving only changes the status on
            # the server; nothing tells the agent to carry it out.
            await respond(query.message, chat.id, tenant_id, RESUME_PROMPT.format(approval_id=approval_id))
        elif action == "nein":
            state.save("eingabe", str(chat.id), {"art": "grund", "server": server, "approval_id": approval_id,
                                                 "message_id": query.message.message_id})
            await query.message.reply_text(GRUND_FRAGE, reply_markup=ForceReply(selective=True))

    application = Application.builder().token(settings.telegram_bot_token).build()

    class _Chat:
        """Replies into a chat without a message to answer (after a form decision)."""

        def __init__(self, chat_id: int) -> None:
            self.chat_id = chat_id

        async def reply_text(self, text: str, **kwargs):
            return await application.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

        async def reply_document(self, **kwargs):
            return await application.bot.send_document(chat_id=self.chat_id, **kwargs)

    async def resume(chat_id: int, tenant_id: str, approval_id: str) -> None:
        await respond(_Chat(chat_id), chat_id, tenant_id, RESUME_PROMPT.format(approval_id=approval_id))

    application.bot_data.update(chat_tenants=chat_tenants, mcp_servers=mcp_servers, resume=resume)
    application.add_handler(MessageHandler(filters.TEXT, on_message))
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
    application = build_application(settings)
    if settings.telegram_webhook_url:
        if not settings.telegram_webhook_secret:
            sys.exit("TELEGRAM_WEBHOOK_SECRET is required with TELEGRAM_WEBHOOK_URL")
        logger.info("agent-hub starting (Telegram webhook): MCP servers: %s",
                    ", ".join(settings.parsed_mcp_servers()))
        # Telegram posts each update to <url>/telegram; our own aiohttp server
        # (telegram_form.py, which also serves the editable approval form)
        # answers 200 at once and rejects requests without the secret header,
        # so the public URL alone lets nobody inject updates. Answering first
        # and working afterwards needs CPU after the response - deploy with
        # --no-cpu-throttling.
        asyncio.run(serve_webhook(application, port=settings.port, public_url=settings.telegram_webhook_url,
                                  bot_token=settings.telegram_bot_token,
                                  webhook_secret=settings.telegram_webhook_secret))
        return
    start_health_server(settings.port)  # Cloud Run's health probe only; not a real API
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
