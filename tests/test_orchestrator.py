from __future__ import annotations

import base64
from dataclasses import dataclass
from types import SimpleNamespace

from agent_hub.orchestrator import MAX_HISTORY_MESSAGES, MAX_TOOL_ITERATIONS, Status, handle_message

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
        # Snapshot messages: a real HTTP client has already serialized the
        # request body by the time create() returns, so later in-place
        # mutation of the same list (handle_message appends to it after the
        # call, to build the returned history) must not retroactively change
        # what was "sent".
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
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

    result, history = await handle_message("hello", hub, claude, "claude-opus-5")

    assert result.text == "hi there"
    assert result.status == Status.AUTONOMOUS
    assert result.attachments == []
    assert hub.calls == []
    assert history[0] == {"role": "user", "content": "hello"}


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

    result, _history = await handle_message("find the invoice", hub, claude, "claude-opus-5")

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

    result, _history = await handle_message("show me the pdf", hub, claude, "claude-opus-5")

    assert result.text == "here is the pdf"
    assert result.status == Status.AUTONOMOUS
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.filename == "invoice.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.data == raw_bytes
    # token hygiene: the raw base64 must not be resent to Claude - Claude
    # never needed it, and it would also become permanent history cost
    second_call_messages = claude.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert base64.b64encode(raw_bytes).decode() not in tool_result_content
    assert "invoice.pdf" in tool_result_content


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

    result, _history = await handle_message("find x", hub, claude, "claude-opus-5")

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

    result, _history = await handle_message("loop forever", hub, claude, "claude-opus-5")

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
                content=[SimpleNamespace(type="text", text='{"status": "pending"}')],
                structured_content={
                    "id": "approval-123",
                    "status": "pending",
                    "resource_id": "d1",
                    "message": "Waiting for human approval.",
                },
            )
        }
    )

    result, _history = await handle_message("send it", hub, claude, "claude-opus-5")

    assert result.status == Status.PENDING_APPROVAL
    assert len(result.pending_approvals) == 1
    pending = result.pending_approvals[0]
    assert pending.server == "email"
    assert pending.approval_id == "approval-123"
    assert pending.message == "Waiting for human approval."


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

    result, _history = await handle_message("send it and search", hub, claude, "claude-opus-5")

    assert result.status == Status.ERROR


async def test_prior_history_is_sent_with_the_next_message() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("Bob, you told me")])])
    hub = FakeToolHub()
    prior_history = [
        {"role": "user", "content": "my name is Bob"},
        {"role": "assistant", "content": [text_block("Nice to meet you, Bob")]},
    ]

    result, new_history = await handle_message(
        "what's my name?", hub, claude, "claude-opus-5", history=prior_history
    )

    assert result.text == "Bob, you told me"
    sent_messages = claude.messages.calls[0]["messages"]
    assert sent_messages[0] == prior_history[0]
    assert sent_messages[1] == prior_history[1]
    assert sent_messages[2] == {"role": "user", "content": "what's my name?"}
    # the new turn is appended onto the returned history, not lost
    assert new_history[-2] == {"role": "user", "content": "what's my name?"}
    assert new_history[-1]["role"] == "assistant"


async def test_history_is_trimmed_to_max_history_messages() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("ok")])])
    hub = FakeToolHub()
    long_history = [{"role": "user", "content": f"message {i}"} for i in range(MAX_HISTORY_MESSAGES + 10)]

    _result, new_history = await handle_message(
        "one more", hub, claude, "claude-opus-5", history=long_history
    )

    assert len(new_history) == MAX_HISTORY_MESSAGES
    # the trim keeps the most recent messages, not the oldest
    assert new_history[0]["content"] != "message 0"


async def test_effort_defaults_to_low_and_is_sent_as_output_config() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("ok")])])
    hub = FakeToolHub()

    await handle_message("hi", hub, claude, "claude-sonnet-5")

    assert claude.messages.calls[0]["output_config"] == {"effort": "low"}


async def test_effort_is_overridable() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("ok")])])
    hub = FakeToolHub()

    await handle_message("hi", hub, claude, "claude-sonnet-5", effort="medium")

    assert claude.messages.calls[0]["output_config"] == {"effort": "medium"}


async def test_cache_control_is_set_on_every_call() -> None:
    claude = FakeClaude([FakeResponse(stop_reason="end_turn", content=[text_block("ok")])])
    hub = FakeToolHub()

    await handle_message("hi", hub, claude, "claude-sonnet-5")

    assert claude.messages.calls[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
