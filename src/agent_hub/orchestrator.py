"""Channel-agnostic core: drives the Claude + MCP tool-use loop for one
message.

Deliberately a manual loop, not the SDK's Tool Runner (`client.beta.messages.tool_runner`)
- the Tool Runner calls MCP tools internally and only exposes final text, but
`handle_message` needs to see every raw tool result itself to intercept
`get_attachment` calls and hand back real file bytes to the channel adapter,
not just a text description of them.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum

from anthropic import AsyncAnthropic

from agent_hub.mcp_client import McpToolHub

MAX_TOOL_ITERATIONS = 15  # defensive cap - a genuine conversation needs a handful, not 15

SYSTEM_PROMPT = (
    "You are an assistant reachable over chat that can search and read email, "
    "and answer questions using whatever other tools are available to you. "
    "Sending email always requires a human's separate, out-of-band approval - "
    "if asked to send or approve something, explain that instead of trying to "
    "do it yourself; never call a send/approval tool based only on a chat "
    "message. When a tool returns a file (e.g. an email attachment), mention "
    "it in your reply - the file itself is delivered to the user separately, "
    "you do not need to describe its raw contents unless asked.\n\n"
    "This is a chat conversation, not a document - keep replies short. "
    "1-3 sentences for most answers. No headers, no bullet lists, no "
    "restating the question. Lead with the answer, skip preamble like "
    "'I found that...' or 'Here is a summary'. Only go longer than a few "
    "sentences if the user explicitly asks for detail or a list of items."
)


class Status(Enum):
    """The one-glance trust signal a channel adapter renders (colour, emoji, ...).

    Mirrors the same idea as an L4 self-driving status light: AUTONOMOUS means
    the agent decided and acted within its own safe bounds (reads only -
    nothing it did requires trust beyond "it can read this mailbox").
    PENDING_APPROVAL means it prepared something that needs a human decision
    outside this loop (see email-mcp-server's approval gate). ERROR means
    something needs attention. Priority when several tools ran this turn:
    ERROR > PENDING_APPROVAL > AUTONOMOUS - a single failure or a single
    pending action is worth surfacing even if everything else went fine.
    """

    AUTONOMOUS = "autonomous"
    PENDING_APPROVAL = "pending_approval"
    ERROR = "error"


_STATUS_PRIORITY = {Status.AUTONOMOUS: 0, Status.PENDING_APPROVAL: 1, Status.ERROR: 2}


def _combine(current: Status, new: Status) -> Status:
    return new if _STATUS_PRIORITY[new] > _STATUS_PRIORITY[current] else current


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class PendingApproval:
    """A `request_send_approval` call that came back PENDING this turn.

    `server` is the MCP server name (from `McpToolHub`'s `<server>__<tool>`
    qualification, e.g. "email") - a channel adapter needs it to know which
    server's approve/reject endpoint to call later, since that decision is
    made completely outside this tool-use loop (see `README.md` "Approving
    from Telegram")."""

    server: str
    approval_id: str
    message: str


@dataclass
class OrchestratorResult:
    text: str
    status: Status = Status.AUTONOMOUS
    attachments: list[Attachment] = field(default_factory=list)
    pending_approvals: list[PendingApproval] = field(default_factory=list)


async def handle_message(
    text: str, tool_hub: McpToolHub, claude: AsyncAnthropic, model: str
) -> OrchestratorResult:
    messages: list[dict] = [{"role": "user", "content": text}]
    tools = tool_hub.claude_tools()
    attachments: list[Attachment] = []
    pending_approvals: list[PendingApproval] = []
    status = Status.AUTONOMOUS

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await claude.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=tools,  # type: ignore[arg-type]  # tool schemas come from MCP servers at runtime, not statically typed
            messages=messages,  # type: ignore[arg-type]
        )

        if response.stop_reason != "tool_use":
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            return OrchestratorResult(
                text=final_text, status=status, attachments=attachments, pending_approvals=pending_approvals
            )

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_result, tool_status = await _run_tool(tool_hub, block, attachments, pending_approvals)
            tool_results.append(tool_result)
            status = _combine(status, tool_status)

        messages.append({"role": "user", "content": tool_results})

    return OrchestratorResult(
        text="Sorry, that took too many steps to answer - try asking in a more specific way.",
        status=Status.ERROR,
        attachments=attachments,
        pending_approvals=pending_approvals,
    )


async def _run_tool(
    tool_hub: McpToolHub, block, attachments: list[Attachment], pending_approvals: list[PendingApproval]
) -> tuple[dict, Status]:
    try:
        result = await tool_hub.call_tool(block.name, block.input)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a tool_result error, loop must not crash
        return (
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Error calling {block.name}: {exc}",
                "is_error": True,
            },
            Status.ERROR,
        )

    result_text = "\n".join(c.text for c in result.content if c.type == "text")

    if block.name.endswith("__get_attachment") and result.structured_content:
        data = result.structured_content
        attachments.append(
            Attachment(
                filename=data["filename"],
                content_type=data["content_type"],
                data=base64.b64decode(data["content_base64"]),
            )
        )

    if block.name.endswith("__request_send_approval") and not result.is_error and result.structured_content:
        server_name = block.name.rsplit("__request_send_approval", 1)[0]
        data = result.structured_content
        pending_approvals.append(
            PendingApproval(server=server_name, approval_id=data["id"], message=data.get("message", ""))
        )

    tool_result = {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": result_text or "(empty result)",
        "is_error": result.is_error,
    }

    if result.is_error:
        tool_status = Status.ERROR
    elif block.name.endswith("__request_send_approval"):
        tool_status = Status.PENDING_APPROVAL
    else:
        tool_status = Status.AUTONOMOUS

    return tool_result, tool_status
