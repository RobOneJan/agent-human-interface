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
    "you do not need to describe its raw contents unless asked."
)


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class OrchestratorResult:
    text: str
    attachments: list[Attachment] = field(default_factory=list)


async def handle_message(
    text: str, tool_hub: McpToolHub, claude: AsyncAnthropic, model: str
) -> OrchestratorResult:
    messages: list[dict] = [{"role": "user", "content": text}]
    tools = tool_hub.claude_tools()
    attachments: list[Attachment] = []

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
            return OrchestratorResult(text=final_text, attachments=attachments)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_results.append(await _run_tool(tool_hub, block, attachments))

        messages.append({"role": "user", "content": tool_results})

    return OrchestratorResult(
        text="Sorry, that took too many steps to answer - try asking in a more specific way.",
        attachments=attachments,
    )


async def _run_tool(tool_hub: McpToolHub, block, attachments: list[Attachment]) -> dict:
    try:
        result = await tool_hub.call_tool(block.name, block.input)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a tool_result error, loop must not crash
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": f"Error calling {block.name}: {exc}",
            "is_error": True,
        }

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

    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": result_text or "(empty result)",
        "is_error": result.is_error,
    }
