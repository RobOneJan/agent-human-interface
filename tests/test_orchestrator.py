from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_hub import attachment_refs
from agent_hub import orchestrator as orchestrator_module
from agent_hub.orchestrator import (
    MAX_HISTORY_MESSAGES,
    MAX_TOOL_ITERATIONS,
    Status,
    _trim_history,
    handle_message,
)

# --- Fakes -------------------------------------------------------------


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(tool_id: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


@dataclass
class FakeResponse:
    stop_reason: str
    content: list
    usage: object = None  # None -> estimate_cost_usd treats it as "unknown", see pricing.py


def fake_usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation: object | None = None,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation=cache_creation,
    )


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
    tenant_id = "tenant-a"

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

    result, history = await handle_message("show me the pdf", hub, claude, "claude-opus-5")

    assert result.text == "here is the pdf"
    assert result.status == Status.AUTONOMOUS
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.filename == "invoice.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.data == raw_bytes
    encoded = base64.b64encode(raw_bytes).decode()

    # Within THIS turn, Claude must actually see the PDF (native document
    # understanding) so it can answer questions about it, not just
    # acknowledge it exists - see orchestrator._document_content_block.
    second_call_messages = claude.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert isinstance(tool_result_content, list)
    assert any(
        block.get("type") == "document" and block.get("source", {}).get("data") == encoded
        for block in tool_result_content
    )

    # Token hygiene: the raw base64 must NOT be carried into the *returned*
    # history - a future turn re-sending it on every call would be unbounded
    # cost for a file Claude only needed to read once.
    tool_result_message = next(m for m in history if m.get("role") == "user" and isinstance(m.get("content"), list))
    redacted_content = tool_result_message["content"][0]["content"]
    assert encoded not in redacted_content
    assert "invoice.pdf" in redacted_content


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
                content=[SimpleNamespace(type="text", text='{"status": "pending"}')],
                structured_content={"id": "approval-123", "status": "pending", "message": "Waiting."},
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


def _tool_result_message(tool_use_id: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok", "is_error": False}],
    }


def test_trim_history_skips_past_a_cut_that_would_orphan_a_tool_result(monkeypatch) -> None:
    # Regression test for a production incident (2026-09-21, 2026-09-23): a
    # plain messages[-N:] slice landed between an assistant tool_use message
    # and its matching user tool_result message, producing a history the API
    # rejects outright (400: orphaned tool_result) - and since the broken,
    # trimmed history is exactly what gets stored and resent on every future
    # message for that chat, it stayed broken until the process restarted.
    monkeypatch.setattr(orchestrator_module, "MAX_HISTORY_MESSAGES", 3)
    messages = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": [tool_use_block("t1", "email__search_emails", {})]},
        _tool_result_message("t1"),  # a naive messages[-3:] would start here - orphaned
        {"role": "assistant", "content": [text_block("done with turn 1")]},
        {"role": "user", "content": "turn 2"},
    ]

    trimmed = _trim_history(messages)

    assert trimmed == [{"role": "user", "content": "turn 2"}]


def test_trim_history_keeps_a_full_turn_even_if_it_alone_exceeds_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module, "MAX_HISTORY_MESSAGES", 2)
    messages = [
        {"role": "user", "content": "turn 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": [tool_use_block("t1", "email__search_emails", {})]},
        _tool_result_message("t1"),
        {"role": "assistant", "content": [tool_use_block("t2", "email__get_email", {})]},
        _tool_result_message("t2"),
        {"role": "assistant", "content": [text_block("done with turn 2")]},
    ]

    trimmed = _trim_history(messages)

    # Longer than the cap, but a complete, valid turn - never a corrupt one.
    assert trimmed[0] == {"role": "user", "content": "turn 2"}
    assert trimmed == messages[1:]


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


async def test_approval_detection_works_for_a_differently_named_tool() -> None:
    """The whole point of shape-based detection: an ERP-style tool that isn't
    literally called request_send_approval still gets picked up as a pending
    approval, as long as its structured output matches the same shape."""
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "erp__book_invoice", {"invoice_id": "inv-1"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("waiting on approval")]),
        ]
    )
    hub = FakeToolHub(
        {
            "erp__book_invoice": FakeToolResult(
                content=[SimpleNamespace(type="text", text="ok")],
                structured_content={"id": "erp-approval-1", "status": "pending", "message": "Booking needs approval."},
            )
        }
    )

    result, _history = await handle_message("book the invoice", hub, claude, "claude-opus-5")

    assert result.status == Status.PENDING_APPROVAL
    assert len(result.pending_approvals) == 1
    pending = result.pending_approvals[0]
    assert pending.server == "erp"
    assert pending.approval_id == "erp-approval-1"


async def test_attachment_detection_works_for_a_differently_named_tool() -> None:
    raw_bytes = b"fake invoice pdf"
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "erp__get_invoice_pdf", {"invoice_id": "inv-1"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("here it is")]),
        ]
    )
    hub = FakeToolHub(
        {
            "erp__get_invoice_pdf": FakeToolResult(
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

    result, _history = await handle_message("get the invoice pdf", hub, claude, "claude-opus-5")

    assert len(result.attachments) == 1
    assert result.attachments[0].filename == "invoice.pdf"
    assert result.attachments[0].data == raw_bytes


async def test_cost_and_tool_call_count_are_summed_across_the_turn() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "email__search_emails", {"query": "x"})],
                usage=fake_usage(input_tokens=1_000, output_tokens=100),
            ),
            FakeResponse(
                stop_reason="end_turn",
                content=[text_block("done")],
                usage=fake_usage(input_tokens=500, output_tokens=50),
            ),
        ]
    )
    hub = FakeToolHub({"email__search_emails": FakeToolResult(content=[SimpleNamespace(type="text", text="ok")])})

    result, _history = await handle_message("find x", hub, claude, "claude-sonnet-5")

    assert result.tool_call_count == 1
    expected = (1_000 + 500) * (2.00 / 1_000_000) + (100 + 50) * (10.00 / 1_000_000)
    assert result.cost_usd == pytest.approx(expected)


async def test_a_no_tool_call_turn_has_zero_tool_calls_but_known_cost() -> None:
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="end_turn",
                content=[text_block("hi")],
                usage=fake_usage(input_tokens=100, output_tokens=10),
            )
        ]
    )
    hub = FakeToolHub()

    result, _history = await handle_message("hello", hub, claude, "claude-sonnet-5")

    assert result.tool_call_count == 0
    assert result.cost_usd == pytest.approx(100 * (2.00 / 1_000_000) + 10 * (10.00 / 1_000_000))


async def test_cost_is_none_for_an_unpriced_model() -> None:
    claude = FakeClaude(
        [FakeResponse(stop_reason="end_turn", content=[text_block("hi")], usage=fake_usage(input_tokens=10))]
    )
    hub = FakeToolHub()

    result, _history = await handle_message("hello", hub, claude, "some-future-model")

    assert result.cost_usd is None


# --- Handing a file from one server to another ------------------------------


def _attachment_result(raw: bytes) -> FakeToolResult:
    return FakeToolResult(
        content=[SimpleNamespace(type="text", text="ok")],
        structured_content={
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(raw),
            "content_base64": base64.b64encode(raw).decode(),
        },
    )


def _ref_shown_to_claude(claude: FakeClaude) -> str:
    content = claude.messages.calls[1]["messages"][-1]["content"][0]["content"]
    text = content[0]["text"] if isinstance(content, list) else content
    return re.search(r"attachment-ref:[0-9a-f]+", text).group(0)


async def test_file_reaches_the_next_server_without_passing_through_claude() -> None:
    raw = b"%PDF-1.4 real invoice"
    encoded = base64.b64encode(raw).decode()

    claude = FakeClaude(
        [
            FakeResponse(stop_reason="tool_use", content=[tool_use_block("t1", "email__get_attachment", {})]),
            FakeResponse(stop_reason="end_turn", content=[text_block("placeholder")]),
        ]
    )
    hub = FakeToolHub({"email__get_attachment": _attachment_result(raw)})
    await handle_message("get it", hub, claude, "claude-opus-5")
    ref = _ref_shown_to_claude(claude)

    # A later turn, as after the user confirmed: only the reference is in the call.
    tool_input = {"rechnung": {"anhaenge": [{"dateiname": "a.pdf", "inhalt_base64": ref}]}}
    claude_next = FakeClaude(
        [
            FakeResponse(stop_reason="tool_use", content=[tool_use_block("t2", "terbox__store", tool_input)]),
            FakeResponse(stop_reason="end_turn", content=[text_block("done")]),
        ]
    )
    hub_next = FakeToolHub(
        {"terbox__store": FakeToolResult(content=[SimpleNamespace(type="text", text="stored")])}
    )
    _result, history = await handle_message("store it", hub_next, claude_next, "claude-opus-5")

    sent = hub_next.calls[0][1]
    assert sent["rechnung"]["anhaenge"][0]["inhalt_base64"] == encoded
    # Claude and the kept history only ever hold the reference.
    assert tool_input["rechnung"]["anhaenge"][0]["inhalt_base64"] == ref
    assert encoded not in str(claude_next.messages.calls)
    assert encoded not in str(history)


async def test_reference_from_another_tenant_is_refused_and_never_sent() -> None:
    ref = attachment_refs.remember("tenant-b", b"%PDF-1.4 someone else's")
    claude = FakeClaude(
        [
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t1", "terbox__store", {"inhalt_base64": ref})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("could not")]),
        ]
    )
    hub = FakeToolHub({"terbox__store": FakeToolResult(content=[])})

    result, _history = await handle_message("store it", hub, claude, "claude-opus-5")

    assert hub.calls == []
    assert result.status == Status.ERROR
    tool_result = claude.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "fetch the file again" in tool_result["content"]


def test_only_a_whole_string_counts_as_a_reference() -> None:
    ref = attachment_refs.remember("t", b"x")
    text = f"please look at {ref}"
    assert attachment_refs.resolve_refs({"body": text}, "t") == {"body": text}
    assert attachment_refs.resolve_refs({"a": [ref, 1, None]}, "t") == {"a": ["eA==", 1, None]}


def test_expired_reference_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = attachment_refs.remember("t", b"x")
    real_monotonic = attachment_refs.time.monotonic
    monkeypatch.setattr(
        attachment_refs.time, "monotonic", lambda: real_monotonic() + attachment_refs.TTL_SECONDS + 1
    )
    with pytest.raises(attachment_refs.UnknownAttachmentRef):
        attachment_refs.resolve_refs(ref, "t")


async def test_file_handed_to_another_tool_is_not_also_posted_in_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"%PDF-1.4 invoice for the erp"
    monkeypatch.setattr(attachment_refs.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    claude = FakeClaude(
        [
            FakeResponse(stop_reason="tool_use", content=[tool_use_block("t1", "email__get_attachment", {})]),
            FakeResponse(
                stop_reason="tool_use",
                content=[tool_use_block("t2", "terbox__store", {"inhalt_base64": "attachment-ref:fixed"})],
            ),
            FakeResponse(stop_reason="end_turn", content=[text_block("Rechnung abgelegt.")]),
        ]
    )
    hub = FakeToolHub(
        {
            "email__get_attachment": _attachment_result(raw),
            "terbox__store": FakeToolResult(content=[SimpleNamespace(type="text", text="stored")]),
        }
    )

    result, _history = await handle_message("file the invoice", hub, claude, "claude-opus-5")

    assert hub.calls[1][1]["inhalt_base64"] == base64.b64encode(raw).decode()
    assert result.attachments == []
    assert result.text == "Rechnung abgelegt."
