"""Channel-agnostic core: drives the Claude + MCP tool-use loop for one
message.

Deliberately a manual loop, not the SDK's Tool Runner (`client.beta.messages.tool_runner`)
- the Tool Runner calls MCP tools internally and only exposes final text, but
`handle_message` needs to see every raw tool result itself to detect file
attachments and pending approvals by their *shape* (see
`_looks_like_attachment`/`_looks_like_pending_approval`), not by hardcoding
one server's tool names - so a future MCP server (e.g. an ERP system) gets
the same file-delivery and approval-gate handling automatically, as long as
its tools return the same result shapes email-mcp-server's do.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum

from anthropic import AsyncAnthropic

from agent_hub.mcp_client import McpToolHub, split_tool_name
from agent_hub.pricing import estimate_cost_usd

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
    # Summed across every claude.messages.create() call this turn made (a
    # multi-tool-call turn calls it more than once). None means the
    # configured model isn't in pricing.py's table, not "free" - a channel
    # adapter must not report $0.00 in that case, see telegram_bot.py.
    cost_usd: float | None = None
    tool_call_count: int = 0


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
    tool_call_count = 0
    # None means "unknown", not "free" - one call on an unpriced model makes
    # the whole turn's cost unknown, since a partial sum would understate it.
    cost_usd: float | None = 0.0

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
        if cost_usd is not None:
            call_cost = estimate_cost_usd(model, response.usage)
            cost_usd = None if call_cost is None else cost_usd + call_cost

        if response.stop_reason != "tool_use":
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            messages.append({"role": "assistant", "content": response.content})
            result = OrchestratorResult(
                text=final_text,
                status=status,
                attachments=attachments,
                pending_approvals=pending_approvals,
                cost_usd=cost_usd,
                tool_call_count=tool_call_count,
            )
            return result, _trim_history(messages)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_result, tool_status = await _run_tool(tool_hub, block, attachments, pending_approvals)
            tool_results.append(tool_result)
            status = _combine(status, tool_status)
            tool_call_count += 1

        messages.append({"role": "user", "content": tool_results})

    result = OrchestratorResult(
        text="Sorry, that took too many steps to answer - try asking in a more specific way.",
        status=Status.ERROR,
        attachments=attachments,
        pending_approvals=pending_approvals,
        cost_usd=cost_usd,
        tool_call_count=tool_call_count,
    )
    return result, _trim_history(messages)


def _trim_history(messages: list[dict]) -> list[dict]:
    """Trim to (approximately) the most recent MAX_HISTORY_MESSAGES entries,
    but ONLY ever cut at a turn boundary - a fresh user text message, never
    a `{"role": "user", "content": [...tool_result...]}` message.

    A plain `messages[-MAX_HISTORY_MESSAGES:]` slice can land inside a
    multi-tool-call turn, between an assistant `tool_use` message and its
    matching user `tool_result` message. That produces a history starting
    with an orphaned tool_result, which the API rejects outright (400:
    "unexpected tool_use_id ... no corresponding tool_use block") - and
    since the trimmed (broken) history is what gets stored and resent on
    every subsequent message for that chat, it stays broken until the
    process restarts and the in-memory history is lost. This happened in
    production (2026-09-21, 2026-09-23) before this fix existed.
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    turn_starts = [
        i for i, m in enumerate(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    min_cut = len(messages) - MAX_HISTORY_MESSAGES
    cut = next((i for i in turn_starts if i >= min_cut), turn_starts[-1])
    return messages[cut:]


def _looks_like_attachment(data: object) -> bool:
    """Structural check, not a tool-name check - any MCP server's tool (not
    just email's `get_attachment`) gets file-delivery treatment as long as
    its structured output matches this shape. Adding a new server (e.g. an
    ERP system returning an invoice PDF) needs no change here."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("filename"), str)
        and isinstance(data.get("content_type"), str)
        and isinstance(data.get("content_base64"), str)
    )


def _looks_like_pending_approval(data: object) -> bool:
    """Structural check, not a tool-name check (e.g. not hardcoded to
    "request_send_approval") - any MCP server's approval-gated tool gets
    picked up as long as it returns this shape (id + status=pending +
    message), the same contract email-mcp-server's ApprovalRequestDTO
    already uses. A future server (e.g. an ERP "book this invoice" action)
    needs no change here, only to return the same shape - see
    email-mcp-server's README, "Approving from a chat channel"."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("id"), str)
        and data.get("status") == "pending"
        and isinstance(data.get("message"), str)
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
    data = result.structured_content
    tool_status = Status.ERROR if result.is_error else Status.AUTONOMOUS

    if not result.is_error and _looks_like_attachment(data):
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
        result_text = f"Attachment {data['filename']!r} ({data.get('size_bytes', '?')} bytes) retrieved and already delivered to the user directly - do not describe its contents unless asked."

    elif not result.is_error and _looks_like_pending_approval(data):
        server_name, _tool_name = split_tool_name(block.name)
        pending_approvals.append(PendingApproval(server=server_name, approval_id=data["id"], message=data["message"]))
        tool_status = Status.PENDING_APPROVAL

    tool_result = {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": result_text or "(empty result)",
        "is_error": result.is_error,
    }

    return tool_result, tool_status
