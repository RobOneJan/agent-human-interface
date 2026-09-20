from __future__ import annotations

import base64
from dataclasses import dataclass
from types import SimpleNamespace

from agent_hub.orchestrator import MAX_TOOL_ITERATIONS, Status, handle_message

# --- Fakes -------------------------------------------------------------


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(tool_id: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


@dataclass
class FakeResponse:
    stop_reason: str
    content: list


class FakeMessages:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClaude:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.messages = FakeMessages(responses)


@dataclass
class FakeToolResult:
    content: list
    structured_content: object = None
    is_error: bool = False


class FakeToolHub:
    def __init__(self, results: dict[str, FakeToolResult] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []

    def claude_tools(self) -> list[dict]:
        return [{"name": "email__search_emails", "description": "", "input_schema": {}}]

    async def call_tool(self, name: str, arguments: dict) -> FakeToolResult:
        self.calls.append((name, arguments))
        if name not in self._results:
            raise RuntimeError(f"unconfigured tool in fake: {name}")
        return self._results[name]


# --- Tests ---------------------------------------------------------------


async def test_immediate_end_turn_returns_text_no_attachments() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("hi there")])])
    hub = FakeToolHub()

    result = await handle_message("hello", hub, claude, "claude-opus-5")

    assert result.text == "hi there"
    assert result.status == Status.AUTONOMOUS
    assert result.attachments == []
    assert hub.calls == []


async def test_single_tool_call_feeds_result_back_and_returns_final_text() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "email__search_emails", {"query": "invoice"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("found 1 email")]),
        ]
    )
    hub = FakeToolHub(
        {
            "email__search_emails": FakeToolResult(
                content=[SimpleNamespace(type="text", text='[{"id": "abc"}]')]
            )
        }
    )

    result = await handle_message("find the invoice", hub, claude, "claude-opus-5")

    assert result.text == "found 1 email"
    assert result.status == Status.AUTONOMOUS
    assert hub.calls == [("email__search_emails", {"query": "invoice"})]
    # the tool result was appended as the next user turn
    second_call_messages = claude.messages.calls[1]["messages"]
    tool_result_msg = second_call_messages[-1]
    assert tool_result_msg["content"][0]["content"] == '[{"id": "abc"}]'
    assert tool_result_msg["content"][0]["is_error"] is False


async def test_get_attachment_call_is_extracted_as_a_real_attachment() -> None:
    raw_bytes = b"%PDF-1.4 fake pdf content"
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "email__get_attachment", {"email_id": "e1", "attachment_id": "a1"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("here is the pdf")]),
        ]
    )
    hub = FakeToolHub(
        {
            "email__get_attachment": FakeToolResult(
                content=[SimpleNamespace(type="text", text="ok")],
                structured_content={
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": len(raw_bytes),
                    "content_base64": base64.b64encode(raw_bytes).decode(),
                },
            )
        }
    )

    result = await handle_message("show me the pdf", hub, claude, "claude-opus-5")

    assert result.text == "here is the pdf"
    assert result.status == Status.AUTONOMOUS
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.filename == "invoice.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.data == raw_bytes


async def test_tool_error_becomes_error_tool_result_and_loop_continues() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "email__search_emails", {"query": "x"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("couldn't find it")]),
        ]
    )
    hub = FakeToolHub()  # no configured result -> call_tool raises

    result = await handle_message("find x", hub, claude, "claude-opus-5")

    assert result.text == "couldn't find it"
    assert result.status == Status.ERROR
    second_call_messages = claude.messages.calls[1]["messages"]
    tool_result_msg = second_call_messages[-1]
    assert tool_result_msg["content"][0]["is_error"] is True
    assert "unconfigured tool in fake" in tool_result_msg["content"][0]["content"]


async def test_runaway_tool_loop_is_capped() -> None:
    responses = [
        FakeResponse(
            stop_reason="tool_use",
            content=[tool_use_block(f"t{i}", "email__search_emails", {"query": "x"})],
        )
        for i in range(MAX_TOOL_ITERATIONS + 5)
    ]
    claude = FakeClaude(responses)
    hub = FakeToolHub({"email__search_emails": FakeToolResult(content=[SimpleNamespace(type="text", text="ok")])})

    result = await handle_message("loop forever", hub, claude, "claude-opus-5")

    assert "too many steps" in result.text
    assert result.status == Status.ERROR
    assert len(hub.calls) == MAX_TOOL_ITERATIONS


async def test_request_send_approval_call_sets_pending_approval_status() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "email__request_send_approval", {"draft_id": "d1"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("waiting on a human to approve")]),
        ]
    )
    hub = FakeToolHub(
        {
            "email__request_send_approval": FakeToolResult(
                content=[SimpleNamespace(type="text", text='{"status": "pending"}')]
            )
        }
    )

    result = await handle_message("send it", hub, claude, "claude-opus-5")

    assert result.status == Status.PENDING_APPROVAL


async def test_error_outranks_pending_approval_in_the_same_turn() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[
                    tool_use_block("t1", "email__request_send_approval", {"draft_id": "d1"}),
                    tool_use_block("t2", "email__search_emails", {"query": "x"}),
                ],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("done")]),
        ]
    )
    hub = FakeToolHub(
        {
            "email__request_send_approval": FakeToolResult(
                content=[SimpleNamespace(type="text", text='{"status": "pending"}')]
            )
            # email__search_emails left unconfigured -> raises -> ERROR
        }
    )

    result = await handle_message("send it and search", hub, claude, "claude-opus-5")

    assert result.status == Status.ERROR
