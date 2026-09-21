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
    "When asked to send an email: go ahead and create the draft and call "
    "request_send_approval as normal - those are safe, reversible steps that "
    "do not send anything on their own. The send only completes once a human "
    "approves it outside this conversation (e.g. by tapping a button in this "
    "chat); you will never be handed a valid approval_id unless that already "
    "happened, so you cannot complete a send purely on your own initiative - "
    "there is no need to refuse or hedge on the request itself. When a tool "
    "returns a file (e.g. an email attachment), mention it in your reply - "
    "the file itself is delivered to the user separately, you do not need to "
    "describe its raw contents unless asked.\n\n"
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


MAX_HISTORY_MESSAGES = 20  # ~10 turns of context; trimmed from the oldest end after each reply


async def handle_message(
    text: str,
    tool_hub: McpToolHub,
    claude: AsyncAnthropic,
    model: str,
    history: list[dict] | None = None,
    effort: str = "low",
) -> tuple[OrchestratorResult, list[dict]]:
    """`history` is the prior conversation (already-sent messages, in Claude's
    own `messages` shape) - pass back whatever this returns as `history` on
    the next call for that same chat to keep context. Omit it (or pass an
    empty list) for a fresh conversation. Trimmed to the most recent
    `MAX_HISTORY_MESSAGES` entries before returning, since Claude's API is
    stateless and resends the full history on every call - unbounded growth
    means unbounded per-message cost and latency.

    `effort` (low|medium|high|xhigh|max) is fixed for the whole conversation,
    not per-turn: changing it mid-conversation invalidates the prompt cache
    (see the cache_control comment below), so it must stay constant across a
    chat_id's history for the caching win to hold."""
    messages: list[dict] = [*(history or []), {"role": "user", "content": text}]
    tools = tool_hub.claude_tools()
    attachments: list[Attachment] = []
    pending_approvals: list[PendingApproval] = []
    status = Status.AUTONOMOUS

    for _ in range(MAX_TOOL_ITERATIONS):
        # tools/messages: schemas and history are built at runtime, not statically
        # typed against the SDK's TypedDicts. cache_control: caches system+tools+
        # history as one growing prefix; 1h (not the 5m default) because this loop
        # waits on a human typing in Telegram between turns - often minutes - and a
        # 1h write pays for itself on the very first prevented miss (a miss re-bills
        # the whole prefix at full price *and* re-writes it).
        response = await claude.messages.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            cache_control={"type": "ephemeral", "ttl": "1h"},
            output_config={"effort": effort},
        )

        if response.stop_reason != "tool_use":
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            messages.append({"role": "assistant", "content": response.content})
            result = OrchestratorResult(
                text=final_text, status=status, attachments=attachments, pending_approvals=pending_approvals
            )
            return result, messages[-MAX_HISTORY_MESSAGES:]

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_result, tool_status = await _run_tool(tool_hub, block, attachments, pending_approvals)
            tool_results.append(tool_result)
            status = _combine(status, tool_status)

        messages.append({"role": "user", "content": tool_results})

    result = OrchestratorResult(
        text="Sorry, that took too many steps to answer - try asking in a more specific way.",
        status=Status.ERROR,
        attachments=attachments,
        pending_approvals=pending_approvals,
    )
    return result, messages[-MAX_HISTORY_MESSAGES:]


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
        # Token hygiene: the tool's raw result_text carries the full base64
        # blob (a 512KB attachment is >150K input tokens Claude would
        # otherwise re-read on every future turn too, via history). Claude
        # never needs the bytes - the file goes straight to the user above -
        # so replace it with a short confirmation instead of resending it.
        result_text = f"Attachment {data['filename']!r} ({data['size_bytes']} bytes) retrieved and already delivered to the user directly - do not describe its contents unless asked."

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
