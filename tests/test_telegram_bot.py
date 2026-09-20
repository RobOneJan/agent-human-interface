from agent_hub.orchestrator import OrchestratorResult, Status
from agent_hub.telegram_bot import format_reply


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
