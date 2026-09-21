from agent_hub.orchestrator import OrchestratorResult, PendingApproval, Status
from agent_hub.telegram_bot import (
    approval_callback_data,
    approval_keyboard,
    format_reply,
    parse_approval_callback_data,
)


def test_autonomous_gets_green_circle() -> None:
    result = OrchestratorResult(text="found it", status=Status.AUTONOMOUS)
    assert format_reply(result) == "\U0001f7e2 found it"


def test_pending_approval_gets_yellow_circle() -> None:
    result = OrchestratorResult(text="waiting for approval", status=Status.PENDING_APPROVAL)
    assert format_reply(result) == "\U0001f7e1 waiting for approval"


def test_error_gets_red_circle() -> None:
    result = OrchestratorResult(text="something broke", status=Status.ERROR)
    assert format_reply(result) == "\U0001f534 something broke"


def test_empty_text_still_returns_just_the_emoji() -> None:
    result = OrchestratorResult(text="", status=Status.AUTONOMOUS)
    assert format_reply(result) == "\U0001f7e2"


def test_approval_callback_data_roundtrips() -> None:
    data = approval_callback_data("approve", "email", "approval-123")
    assert parse_approval_callback_data(data) == ("approve", "email", "approval-123")


def test_approval_callback_data_survives_dashes_in_a_uuid() -> None:
    # a real approval_id is a uuid4 - no colons in it, but let's be sure the
    # split(":", 2) limit doesn't get confused by hyphens
    data = approval_callback_data("reject", "email", "a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert parse_approval_callback_data(data) == ("reject", "email", "a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def test_approval_keyboard_has_approve_and_reject_buttons_with_matching_callback_data() -> None:
    pending = PendingApproval(server="email", approval_id="approval-123", message="Waiting for approval.")

    keyboard = approval_keyboard(pending)

    row = keyboard.inline_keyboard[0]
    assert len(row) == 2
    assert row[0].callback_data == "approve:email:approval-123"
    assert row[1].callback_data == "reject:email:approval-123"
