"""Connects to one or more MCP servers and exposes them as a single, unified
tool surface for the Claude tool-use loop.

Tool names are namespaced as "<server_name>__<tool_name>" so two servers can
never collide on a tool name - adding a server (e.g. `erp=<url>`) later is a
config change (`MCP_SERVERS`), not a code change here.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Self

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_hub.auth import fetch_auth_header

TOOL_SEPARATOR = "__"
TENANT_HEADER = "X-Tenant-Id"


def qualify_tool_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}{TOOL_SEPARATOR}{tool_name}"


def split_tool_name(qualified_name: str) -> tuple[str, str]:
    server_name, sep, tool_name = qualified_name.partition(TOOL_SEPARATOR)
    if not sep:
        raise ValueError(f"not a qualified tool name (missing {TOOL_SEPARATOR!r}): {qualified_name!r}")
    return server_name, tool_name


class McpToolHub:
    """Async context manager. On entry, connects to every configured MCP
    server and lists their tools; on exit, closes every connection."""

    def __init__(self, servers: dict[str, str], tenant_id: str) -> None:
        self._server_urls = servers
        self._tenant_id = tenant_id
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._claude_tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        for name, url in self._server_urls.items():
            # X-Tenant-Id tells a multi-tenant MCP server (see
            # email-mcp-server's mcp/tools.py:_resolve_tenant_id) which of
            # this bot's own already-authorized chats a call is for. It is
            # not itself an authentication mechanism - the target server only
            # trusts it because the whole connection is already gated by
            # fetch_auth_header's Cloud Run IAM token (or a trusted local
            # proxy); a server that isn't multi-tenant-aware just ignores it.
            headers = {**fetch_auth_header(url), TENANT_HEADER: self._tenant_id}
            http_client = httpx2.AsyncClient(headers=headers, timeout=30.0)
            read_stream, write_stream = await self._stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )
            session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self._sessions[name] = session

            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                self._claude_tools.append(
                    {
                        "name": qualify_tool_name(name, tool.name),
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                    }
                )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    def claude_tools(self) -> list[dict[str, Any]]:
        return self._claude_tools

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> Any:
        server_name, tool_name = split_tool_name(qualified_name)
        session = self._sessions.get(server_name)
        if session is None:
            raise ValueError(f"unknown MCP server {server_name!r} (from tool {qualified_name!r})")
        return await session.call_tool(tool_name, arguments)
