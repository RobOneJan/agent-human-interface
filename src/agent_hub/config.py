"""Environment-driven configuration.

`ANTHROPIC_API_KEY` is deliberately not a field here - the Anthropic SDK
resolves it (or an `ant auth login` profile) from the environment itself;
see README's "Credentials" section.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str

    # Only used to satisfy Cloud Run's health-check port when hosted - this
    # process is a long-polling worker, not an HTTP service; nothing else
    # should ever call it. See main.py.
    port: int = Field(default=8080, gt=0)

    # "<name>=<url>,<name>=<url>,..." - one entry per MCP server this agent can
    # use as tools. Tool names are exposed to Claude as "<name>__<tool>" so two
    # servers can never collide. Start with just email; add erp=<url> etc. later
    # without touching orchestrator.py.
    mcp_servers: str

    # Comma-separated Telegram chat ids allowed to talk to this bot. Anyone not
    # on this list is ignored - there is no self-serve signup flow (see README).
    telegram_allowed_chat_ids: str

    claude_model: str = "claude-opus-5"

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

    def parsed_allowed_chat_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields are loaded from the environment
