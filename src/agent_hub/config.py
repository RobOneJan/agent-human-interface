"""Environment-driven configuration.

`ANTHROPIC_API_KEY` is deliberately not a field here - the Anthropic SDK
resolves it (or an `ant auth login` profile) from the environment itself;
see README's "Credentials" section.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Mirrors email-mcp-server's own domain.identity.DEFAULT_TENANT_ID string -
# no import dependency between the two repos, just the same convention: "no
# explicit tenant" means the original single mailbox.
DEFAULT_TENANT_ID = "default"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Optional now that Teams (see teams_bot.py) is the primary channel -
    # telegram_bot.py is still importable, just not run by default (see
    # main.py). Left required=False rather than removed so a future redeploy
    # that reinstates it needs no Settings change.
    telegram_bot_token: str | None = None
    # Comma-separated Telegram chat ids allowed to talk to this bot, each
    # optionally mapped to a tenant_id: "<chat_id>" or "<chat_id>=<tenant_id>".
    # See parsed_chat_tenants(). Empty by default now that Telegram isn't run.
    telegram_allowed_chat_ids: str = ""

    # Real HTTP port under Teams (teams_bot.py's aiohttp app listens here);
    # was only a health-check placeholder under Telegram's long-polling
    # model. See main.py.
    port: int = Field(default=8080, gt=0)

    # Microsoft Teams / Bot Framework (see teams_bot.py). Same Azure AD app
    # registration as email-mcp-server's GRAPH_CLIENT_ID/SECRET/TENANT_ID -
    # this app doubles as both the Bot's own identity and the Graph
    # on-behalf-of client - configured here independently since this is a
    # separate deployment. See README's Azure setup steps.
    teams_app_id: str | None = None
    teams_app_password: str | None = None
    teams_app_tenant_id: str | None = None

    # "<name>=<url>,<name>=<url>,..." - one entry per MCP server this agent can
    # use as tools. Tool names are exposed to Claude as "<name>__<tool>" so two
    # servers can never collide. Start with just email; add erp=<url> etc. later
    # without touching orchestrator.py.
    mcp_servers: str

    # Stepped down from claude-opus-5 for cost - see README "Cost". Override
    # via env if quality doesn't hold up for your traffic.
    claude_model: str = "claude-sonnet-5"
    # low | medium | high | xhigh | max. This workload (short email Q&A,
    # usually 1-3 tool calls) is closest to the "research and knowledge
    # work" shape in Anthropic's own cost-optimization guide, where low
    # effort gave up little accuracy for real savings - see README "Cost".
    claude_effort: str = "low"

    def parsed_mcp_servers(self) -> dict[str, str]:
        servers: dict[str, str] = {}
        for entry in self.mcp_servers.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, url = entry.partition("=")
            if not name or not url:
                raise ValueError(f"invalid MCP_SERVERS entry: {entry!r} (expected name=url)")
            servers[name.strip()] = url.strip()
        if not servers:
            raise ValueError("MCP_SERVERS must list at least one name=url entry")
        return servers

    def parsed_chat_tenants(self) -> dict[int, str]:
        """chat_id -> tenant_id, from `telegram_allowed_chat_ids`'s
        "<chat_id>" or "<chat_id>=<tenant_id>" entries (see the field's own
        docstring). This is both the access-control list (its keys) and the
        tenant routing table - one source of truth instead of two lists that
        could drift apart."""
        tenants: dict[int, str] = {}
        for entry in self.telegram_allowed_chat_ids.split(","):
            entry = entry.strip()
            if not entry:
                continue
            chat_id_part, _, tenant_part = entry.partition("=")
            tenants[int(chat_id_part.strip())] = tenant_part.strip() or DEFAULT_TENANT_ID
        return tenants

    def parsed_allowed_chat_ids(self) -> set[int]:
        return set(self.parsed_chat_tenants().keys())


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields are loaded from the environment
