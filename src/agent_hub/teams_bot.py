"""Microsoft Teams channel adapter (Bot Framework + Teams SSO).

Thin by design, same as `telegram_bot.py`: translates Teams <-> the
channel-agnostic orchestrator (`orchestrator.handle_message`,
`mcp_client.McpToolHub`) - neither of those needs to know Teams exists.

`tenant_id` here is the signed-in user's own Azure AD object id, not a
static allow-list entry - unlike Telegram's `TELEGRAM_ALLOWED_CHAT_IDS`,
there is no separate access-control list to maintain: only members of the
Azure AD tenant this bot is registered in can open/message it at all, and
Microsoft Graph's own delegated-permission model (see email-mcp-server's
`providers/graph/auth.py`) already scopes each signed-in user to exactly the
mailbox(es) they can see in Outlook - the org's own mailbox permissions ARE
the access control.

Teams SSO flow, in order:
1. `TeamsSSOTokenExchangeMiddleware` (registered on the adapter) transparently
   handles the `signin/tokenExchange` invoke Teams sends when it can silently
   exchange a token for this app's own `access_as_user` scope - after that,
   the Bot Framework Token Service has this user's token cached.
2. On a normal message, `_ensure_graph_bootstrap` asks that Token Service for
   the cached token (`UserTokenClient.get_user_token`). If present, it is
   this app's own `access_as_user`-scoped assertion - forwarded once to
   email-mcp-server's `POST /internal/graph/bootstrap`, which exchanges it
   for a Graph token via MSAL's on-behalf-of flow and caches a refresh token
   there. If absent, an OAuthCard (sign-in prompt) is sent instead and the
   message is not otherwise processed this turn.
3. Every later message for that tenant_id skips step 2 (see `_graph_ready`)
   and goes straight through McpToolHub/handle_message, identical to
   Telegram's own flow.
"""

from __future__ import annotations

import logging
import re
import secrets
import time

from aiohttp import web
from anthropic import AsyncAnthropic
from botbuilder.core import CardFactory, MemoryStorage, TurnContext
from botbuilder.core.teams import TeamsActivityHandler, TeamsSSOTokenExchangeMiddleware
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ActionTypes, Attachment as BotAttachment, CardAction, OAuthCard
from botframework.connector.auth.user_token_client import UserTokenClient

import httpx2
from agent_hub.approvals import ApprovalDecisionError, decide_approval
from agent_hub.auth import fetch_auth_header
from agent_hub.config import Settings, get_settings
from agent_hub.mcp_client import McpToolHub
from agent_hub.orchestrator import Attachment, OrchestratorResult, PendingApproval, Status, handle_message

logger = logging.getLogger(__name__)

_CONNECTION_NAME = "graph"

_STATUS_EMOJI = {
    Status.AUTONOMOUS: "\U0001f7e2",
    Status.PENDING_APPROVAL: "\U0001f7e1",
    Status.ERROR: "\U0001f534",
}

# See telegram_bot.py's own copy of this constant for the rationale - kept
# identical across both channel adapters on purpose.
MIN_TOOL_CALLS_FOR_COST_REPORT = 0


def format_reply(result: OrchestratorResult) -> str:
    emoji = _STATUS_EMOJI[result.status]
    return f"{emoji} {result.text}" if result.text else emoji


def format_cost_line(result: OrchestratorResult) -> str | None:
    """See telegram_bot.py's own copy for why `cost_usd is None` must not be
    reported as free. Phrased channel-neutrally here (no "Robert zahlt") -
    unlike the Telegram bot, this channel is reachable by other Ineos users,
    for whom that phrasing would be confusing even though the cost is
    factually still billed to whichever ANTHROPIC_API_KEY this service runs
    with."""
    if result.tool_call_count < MIN_TOOL_CALLS_FOR_COST_REPORT or result.cost_usd is None:
        return None
    return f"Kosten: {result.cost_usd * 100:.2f}ct"


def _approval_value(action: str, server: str, approval_id: str) -> dict:
    return {"action": action, "server": server, "approval_id": approval_id}


def approval_card(pending: PendingApproval) -> BotAttachment:
    """Adaptive Card, not a HeroCard: every `payload` field (e.g. an email's
    to/cc/subject/body_preview, or every field of a SevDesk create) renders as
    an editable Input.Text, pre-filled with the server's original value - a
    human can correct something (a typo'd address, a wrong price) right here
    before approving. Editing is optional: tapping Freigeben unchanged just
    resubmits the original values. Structural like the rest of the approval
    plumbing - whatever keys a server's `payload` has are what shows up here,
    nothing hardcoded to one server's field names. Edited values arrive back
    in the same Action.Submit `data` dict (Adaptive Cards merge input values
    into the submitted payload automatically) - see
    `TeamsBot._handle_approval_callback`."""
    body: list[dict] = [{"type": "TextBlock", "text": pending.message, "wrap": True}]
    for key, value in pending.payload.items():
        label = key.replace("_", " ").strip().capitalize()
        body.append({"type": "TextBlock", "text": label, "weight": "bolder", "size": "small", "spacing": "medium"})
        body.append({"type": "Input.Text", "id": key, "value": value, "isMultiline": len(value) > 80})
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Freigeben",
                "data": _approval_value("approve", pending.server, pending.approval_id),
            },
            {
                "type": "Action.Submit",
                "title": "❌ Ablehnen",
                "data": _approval_value("reject", pending.server, pending.approval_id),
            },
        ],
    }
    return CardFactory.adaptive_card(card)


class _AttachmentStore:
    """In-memory token -> bytes map so Teams file delivery can use a real
    HTTPS content_url instead of a data: URI, which Teams silently drops (no
    error back to the bot - just never renders, see README's known gaps
    before this fix). Matches this process's existing single-instance
    assumption (--max-instances=1, same as `_chat_history`/`_graph_ready`).
    A short TTL is enough: the only reader is the same Teams conversation the
    file was just delivered to, within seconds of the attachment message."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, tuple[bytes, str, str, float]] = {}

    def put(self, data: bytes, content_type: str, filename: str) -> str:
        self._gc()
        token = secrets.token_urlsafe(24)
        self._items[token] = (data, content_type, filename, time.monotonic() + self._ttl)
        return token

    def get(self, token: str) -> tuple[bytes, str, str] | None:
        item = self._items.get(token)
        if item is None:
            return None
        data, content_type, filename, expiry = item
        if time.monotonic() > expiry:
            del self._items[token]
            return None
        return data, content_type, filename

    def _gc(self) -> None:
        now = time.monotonic()
        for token in [t for t, item in self._items.items() if item[3] < now]:
            del self._items[token]


def _tenant_id_of(turn_context: TurnContext) -> str | None:
    from_property = turn_context.activity.from_property
    if from_property is None:
        return None
    # aad_object_id is populated by the Teams channel specifically; falling
    # back to the channel account id keeps this working against the Bot
    # Framework Emulator (which has no AAD identity) for local testing.
    return from_property.aad_object_id or from_property.id


def _log_assertion_claims(token: str) -> None:
    """Log the unverified header/claims of the Teams SSO token before it's
    forwarded for the Graph OBO exchange - diagnostic only, never used for
    any actual auth decision. Safe to log: aud/iss/appid/tid/kid identify
    which app+tenant issued the token for whom, not a credential themselves;
    the signature (the actual secret part of the JWT) is never decoded or
    logged. Added while chasing a persistent AADSTS50013 "assertion failed
    signature validation" from email-mcp-server's OBO exchange that survived
    three separate, confirmed-correct Application-ID-URI fixes - this shows
    what the token itself actually claims instead of guessing a fourth one."""
    try:
        import jwt

        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        logger.info(
            "teams SSO assertion: kid=%s alg=%s aud=%s iss=%s appid=%s tid=%s ver=%s",
            header.get("kid"),
            header.get("alg"),
            claims.get("aud"),
            claims.get("iss"),
            claims.get("appid") or claims.get("azp"),
            claims.get("tid"),
            claims.get("ver"),
        )
    except Exception:
        logger.exception("failed to decode assertion for diagnostic logging")


async def _bootstrap_graph(mcp_servers: dict[str, str], tenant_id: str, user_assertion: str) -> None:
    """POST the just-obtained Teams SSO token to email-mcp-server's Graph
    provider bootstrap route (see that repo's `providers/graph/auth.py`).
    Only the "email" MCP server is Graph-backed; any other configured server
    (e.g. "terbox") has nothing to bootstrap and is left alone."""
    email_url = mcp_servers.get("email")
    if email_url is None:
        return
    base_url = email_url.rsplit("/mcp", 1)[0]
    headers = fetch_auth_header(email_url)
    async with httpx2.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base_url}/internal/graph/bootstrap",
            json={"tenant_id": tenant_id, "user_assertion": user_assertion},
            headers=headers,
        )
    if response.status_code >= 400:
        # email-mcp-server's own JSONResponse body has the real reason (e.g.
        # the MSAL error_description) - raise_for_status() alone discards it.
        logger.error(
            "graph bootstrap call failed: status=%s body=%s", response.status_code, response.text
        )
    response.raise_for_status()


class TeamsBot(TeamsActivityHandler):
    def __init__(
        self, settings: Settings, mcp_servers: dict[str, str], attachment_store: "_AttachmentStore"
    ) -> None:
        self._settings = settings
        self._mcp_servers = mcp_servers
        self._attachment_store = attachment_store
        self._claude = AsyncAnthropic()
        # In-memory only, keyed by tenant_id - same reasoning as
        # telegram_bot.py's chat_history: this process is a singleton (see
        # cloudbuild.yaml's --max-instances=1), so there is never a second
        # instance that wouldn't share this state.
        self._chat_history: dict[str, list[dict]] = {}
        # tenant_ids that have already completed the Graph OBO bootstrap this
        # process's lifetime - avoids re-forwarding a fresh SSO token (and
        # re-doing an OBO exchange against Azure AD) on every single message
        # once a tenant's Graph refresh token is already cached server-side.
        # Cleared for a tenant on a bootstrap failure so the next message
        # retries (see on_message_activity).
        self._graph_ready: set[str] = set()

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        tenant_id = _tenant_id_of(turn_context)
        if tenant_id is None:
            logger.warning("ignoring message with no resolvable tenant id")
            return

        value = turn_context.activity.value
        if isinstance(value, dict) and "action" in value:
            await self._handle_approval_callback(turn_context, tenant_id, value)
            return

        text = turn_context.activity.text
        if not text:
            return

        if text.strip().lower() in ("logout", "abmelden"):
            await self._sign_out(turn_context, tenant_id)
            return

        if tenant_id not in self._graph_ready:
            # If the user is typing back the magic code from the sign-in
            # popup (see _ensure_graph_bootstrap), forward it to the Token
            # Service so it can complete the exchange - without this, a typed
            # code was silently ignored and the bot just re-sent the same
            # sign-in card forever.
            magic_code = text.strip() if re.fullmatch(r"\d{4,8}", text.strip()) else None
            ready = await self._ensure_graph_bootstrap(turn_context, tenant_id, magic_code)
            if not ready:
                return  # sign-in card already sent; wait for the user to complete it and message again

        try:
            async with McpToolHub(self._mcp_servers, tenant_id=tenant_id) as tool_hub:
                result, history = await handle_message(
                    text,
                    tool_hub,
                    self._claude,
                    self._settings.claude_model,
                    self._chat_history.get(tenant_id),
                    effort=self._settings.claude_effort,
                )
            self._chat_history[tenant_id] = history
        except Exception:
            logger.exception("failed to handle message from tenant_id=%s", tenant_id)
            await turn_context.send_activity("Sorry, something went wrong answering that. Try again shortly.")
            return

        await self._send_result(turn_context, result)

    async def _send_result(self, turn_context: TurnContext, result: OrchestratorResult) -> None:
        await turn_context.send_activity(format_reply(result))
        for attachment in result.attachments:
            await turn_context.send_activity(Activity(attachments=[self._file_attachment(attachment)]))
        for pending in result.pending_approvals:
            await turn_context.send_activity(
                Activity(
                    text=f"{_STATUS_EMOJI[Status.PENDING_APPROVAL]} {pending.message}",
                    attachments=[approval_card(pending)],
                )
            )
        cost_line = format_cost_line(result)
        if cost_line is not None:
            await turn_context.send_activity(cost_line)

    def _file_attachment(self, attachment: Attachment) -> BotAttachment:
        base_url = self._settings.public_base_url
        if not base_url:
            logger.error("PUBLIC_BASE_URL is not configured - cannot deliver %r to Teams", attachment.filename)
            return BotAttachment(name=attachment.filename, content_type=attachment.content_type)
        token = self._attachment_store.put(attachment.data, attachment.content_type, attachment.filename)
        return BotAttachment(
            name=attachment.filename,
            content_type=attachment.content_type,
            content_url=f"{base_url}/attachments/{token}",
        )

    async def _resume_after_approval(self, turn_context: TurnContext, tenant_id: str, approval_id: str) -> None:
        """Continue the SAME conversation right after a human approves via the
        Teams card - without this, approving only updated the server-side
        approval status; nothing ever told the LLM to actually act on it, so
        the send/execute tool call never happened until the user separately
        asked (e.g. "did you send it?") in a brand new turn. This resumes
        with an explicit system-style instruction so the model reliably calls
        the follow-up tool instead of maybe re-answering from stale context."""
        prompt = (
            f"[System] A human just approved pending approval {approval_id}. "
            "Proceed now to complete the corresponding action (call the "
            "matching execute/send tool for it) and confirm briefly once done."
        )
        try:
            async with McpToolHub(self._mcp_servers, tenant_id=tenant_id) as tool_hub:
                result, history = await handle_message(
                    prompt,
                    tool_hub,
                    self._claude,
                    self._settings.claude_model,
                    self._chat_history.get(tenant_id),
                    effort=self._settings.claude_effort,
                )
            self._chat_history[tenant_id] = history
        except Exception:
            logger.exception("failed to resume after approval for tenant_id=%s", tenant_id)
            await turn_context.send_activity(
                "Approved, but I couldn't complete the follow-up action automatically - ask me to proceed."
            )
            return
        await self._send_result(turn_context, result)

    async def _ensure_graph_bootstrap(
        self, turn_context: TurnContext, tenant_id: str, magic_code: str | None = None
    ) -> bool:
        user_token_client: UserTokenClient | None = turn_context.turn_state.get(UserTokenClient.__name__)
        if user_token_client is None:
            logger.error("no UserTokenClient in turn_state - adapter/auth misconfigured")
            await turn_context.send_activity("Sign-in isn't configured correctly on this bot yet.")
            return False

        token_response = await user_token_client.get_user_token(
            turn_context.activity.from_property.id,
            _CONNECTION_NAME,
            turn_context.activity.channel_id,
            magic_code,
        )
        if token_response and token_response.token:
            _log_assertion_claims(token_response.token)
            try:
                await _bootstrap_graph(self._mcp_servers, tenant_id, token_response.token)
            except Exception:
                logger.exception("Graph bootstrap failed for tenant_id=%s", tenant_id)
                await turn_context.send_activity("Couldn't connect your mailbox just now - try again shortly.")
                return False
            self._graph_ready.add(tenant_id)
            return True

        sign_in_resource = await user_token_client.get_sign_in_resource(
            _CONNECTION_NAME, turn_context.activity, None
        )
        card = OAuthCard(
            text="Melde dich an, um dein Postfach zu verbinden.",
            connection_name=_CONNECTION_NAME,
            buttons=[
                CardAction(type=ActionTypes.signin, title="Anmelden", value=sign_in_resource.sign_in_link)
            ],
            token_exchange_resource=sign_in_resource.token_exchange_resource,
        )
        # Plain-text fallback alongside the OAuthCard: some Teams surfaces
        # (older desktop builds, some mobile clients) silently drop an
        # unrenderable OAuthCard attachment with no error back to the bot -
        # the text line guarantees the user sees *something* and can still
        # reach the sign-in link even if the card itself never appears.
        await turn_context.send_activity(
            f"🔑 Melde dich an, um dein Postfach zu verbinden: {sign_in_resource.sign_in_link}"
        )
        await turn_context.send_activity(Activity(attachments=[CardFactory.oauth_card(card)]))
        return False

    async def _sign_out(self, turn_context: TurnContext, tenant_id: str) -> None:
        """Clear the Bot Framework Token Service's cached token for this
        user+connection (typed "logout"/"abmelden") - the next sign-in is
        then guaranteed fresh, not served from cache. Useful whenever the
        Azure AD app registration's config (e.g. the Application ID URI)
        changes after a user already completed the SSO bootstrap once."""
        user_token_client: UserTokenClient | None = turn_context.turn_state.get(UserTokenClient.__name__)
        if user_token_client is None:
            await turn_context.send_activity("Sign-in isn't configured correctly on this bot yet.")
            return
        await user_token_client.sign_out_user(
            turn_context.activity.from_property.id,
            _CONNECTION_NAME,
            turn_context.activity.channel_id,
        )
        self._graph_ready.discard(tenant_id)
        await turn_context.send_activity("🔓 Abgemeldet. Schick eine neue Nachricht für einen frischen Sign-in.")

    async def _handle_approval_callback(self, turn_context: TurnContext, tenant_id: str, value: dict) -> None:
        action = value.get("action")
        server = value.get("server")
        approval_id = value.get("approval_id")
        server_url = self._mcp_servers.get(server) if server else None
        if server_url is None:
            await turn_context.send_activity(f"{_STATUS_EMOJI[Status.ERROR]} Unknown server {server!r}.")
            return

        # Any other key in the Adaptive Card's submitted `data` is a payload
        # field (see `approval_card`'s Input.Text ids) - a human may have
        # edited it right there before tapping approve. Applied server-side,
        # atomically with the approve decision (never through the LLM) - see
        # `decide_approval`'s own docstring for why that boundary matters.
        edits = {k: v for k, v in value.items() if k not in ("action", "server", "approval_id")}

        try:
            decision = await decide_approval(
                server_url,
                approval_id,
                tenant_id,
                approve=action == "approve",
                edits=edits if action == "approve" and edits else None,
            )
        except ApprovalDecisionError as exc:
            logger.warning("approval decision failed: tenant_id=%s detail=%s", tenant_id, exc.detail)
            await turn_context.send_activity(f"{_STATUS_EMOJI[Status.ERROR]} Couldn't {action}: {exc.detail}")
            return

        if action == "approve":
            await turn_context.send_activity(f"{_STATUS_EMOJI[Status.AUTONOMOUS]} Freigegeben – schließe ab...")
            await self._resume_after_approval(turn_context, tenant_id, approval_id)
            return

        await turn_context.send_activity(f"{_STATUS_EMOJI[Status.ERROR]} Abgelehnt ({decision['status']}).")


class _BotFrameworkConfig:
    """Duck-typed config object `ConfigurationServiceClientCredentialFactory`
    reads via getattr - see that class's own source for the exact attribute
    names it looks for. `SingleTenant` matches this app's own Entra ID app
    registration (see README's Azure setup steps), not the AAD multi-tenant
    default."""

    def __init__(self, settings: Settings) -> None:
        self.APP_ID = settings.teams_app_id
        self.APP_PASSWORD = settings.teams_app_password
        self.APP_TYPE = "SingleTenant"
        self.APP_TENANTID = settings.teams_app_tenant_id


def build_adapter(settings: Settings) -> CloudAdapter:
    auth = ConfigurationBotFrameworkAuthentication(_BotFrameworkConfig(settings))
    adapter = CloudAdapter(auth)

    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.exception("unhandled error in Teams turn", exc_info=error)
        await context.send_activity("Sorry, something went wrong.")

    adapter.on_turn_error = on_error
    # Deduplicates/handles the `signin/tokenExchange` invoke Teams sends for
    # silent SSO - see this module's own docstring. MemoryStorage matches
    # this project's existing single-instance assumption (--max-instances=1).
    adapter.use(TeamsSSOTokenExchangeMiddleware(MemoryStorage(), _CONNECTION_NAME))
    return adapter


def build_app(settings: Settings) -> web.Application:
    mcp_servers = settings.parsed_mcp_servers()
    adapter = build_adapter(settings)
    attachment_store = _AttachmentStore()
    bot = TeamsBot(settings, mcp_servers, attachment_store)

    async def messages(req: web.Request) -> web.Response:
        return await adapter.process(req, bot)

    async def health(_req: web.Request) -> web.Response:
        # Cloud Run's startup/liveness probe only - see telegram_bot.py's own
        # (now-unused-for-this-channel) health_server.py for the equivalent
        # under polling. Unauthenticated by design, same reasoning as
        # email-mcp-server's own /health route.
        return web.json_response({"status": "ok"})

    async def get_attachment(req: web.Request) -> web.Response:
        # Unauthenticated by design, same as /health: the token itself (a
        # 24-byte random string, see _AttachmentStore.put) is the only secret
        # - guessing one is as infeasible as guessing a signed URL, and
        # that's exactly what this is standing in for. Short TTL limits
        # exposure further.
        item = attachment_store.get(req.match_info["token"])
        if item is None:
            return web.Response(status=404)
        data, content_type, filename = item
        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    app.router.add_get("/attachments/{token}", get_attachment)
    return app


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    app = build_app(settings)
    logger.info(
        "agent-hub (Teams) starting: MCP servers: %s",
        ", ".join(settings.parsed_mcp_servers()),
    )
    web.run_app(app, host="0.0.0.0", port=settings.port)
