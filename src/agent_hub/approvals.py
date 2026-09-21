"""Calls email-mcp-server's (or any configured server's) non-MCP
approve/reject HTTP routes - see that server's README, "Approving from a
chat channel".

Deliberately separate from `mcp_client.py`: this is not an MCP tool call and
must never be reachable through `McpToolHub`/the orchestrator's tool-use
loop, since that is exactly the path the LLM drives. This module is called
from exactly one place: a channel adapter's human-triggered callback (e.g.
`telegram_bot.py`'s button handler), never from `orchestrator.py`.
"""

from __future__ import annotations

import httpx2

from agent_hub.auth import fetch_auth_header


class ApprovalDecisionError(Exception):
    """The server refused the decision (already decided, unknown id, ...).
    `status_code` lets the caller distinguish 404 (unknown/wrong tenant)
    from 409 (already decided) if it wants to phrase the message differently."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _base_url(mcp_server_url: str) -> str:
    # MCP_SERVERS entries point at the MCP endpoint itself, e.g. ".../mcp" -
    # the approval routes live on the same origin, one level up.
    return mcp_server_url.rsplit("/mcp", 1)[0]


async def decide_approval(mcp_server_url: str, approval_id: str, *, approve: bool) -> dict:
    action = "approve" if approve else "reject"
    url = f"{_base_url(mcp_server_url)}/internal/approvals/{approval_id}/{action}"
    headers = fetch_auth_header(mcp_server_url)
    async with httpx2.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, content=b"")
    if response.status_code != 200:
        detail = response.json().get("error", response.text) if response.content else response.text
        raise ApprovalDecisionError(response.status_code, detail)
    return response.json()
