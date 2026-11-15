from __future__ import annotations

import asyncio

import pytest
from botbuilder.schema import Activity, ChannelAccount, ConversationAccount

from agent_hub import morning_report, teams_bot
from agent_hub.approvals import ApprovalDecisionError
from agent_hub.orchestrator import OrchestratorResult, PendingApproval
from tests.test_jobs import NOW, TENANT, _bot, _clean_state, _FakeAdapter  # noqa: F401 - fixture
from tests.test_morning_report import REPORT


class _CardTurn:
    """A turn carrying a tap on an approval card (reply_to_id = the card's message)."""

    def __init__(self) -> None:
        self.activity = Activity(
            type="message",
            reply_to_id="card-1",
            channel_id="msteams",
            service_url="https://smba.trafficmanager.net/emea/",
            conversation=ConversationAccount(id="a:chat-1", tenant_id=TENANT),
            from_property=ChannelAccount(id="29:user"),
            recipient=ChannelAccount(id="28:bot"),
        )
        self.sent: list = []
        self.updated: list = []

    async def send_activity(self, activity) -> None:
        self.sent.append(activity)

    async def update_activity(self, activity) -> None:
        self.updated.append(activity)


def _closed_text(activity) -> str:
    return activity.attachments[0].content["body"][0]["text"]


def _tap(action: str = "approve", **edits) -> dict:
    return {**teams_bot._approval_value(action, "terbox", "ap-1", "Rechnung RE-1 buchen?"), **edits}


@pytest.fixture
def decided(monkeypatch: pytest.MonkeyPatch) -> list:
    calls = []

    async def fake_decide(url, approval_id, tenant_id, *, approve, edits=None):
        calls.append({"approval_id": approval_id, "approve": approve, "edits": edits})
        return {"status": "approved" if approve else "rejected"}

    monkeypatch.setattr(teams_bot, "decide_approval", fake_decide)
    return calls


async def test_approving_closes_the_card_and_finishes_in_the_background(monkeypatch, decided) -> None:
    bot = _bot(monkeypatch)
    bot.adapter = _FakeAdapter()

    async def fake_handle_message(*args, **kwargs):
        return OrchestratorResult(text="Rechnung RE-1 gebucht (BILL/1)."), []

    monkeypatch.setattr(teams_bot, "handle_message", fake_handle_message)
    turn = _CardTurn()

    await bot._handle_approval_callback(turn, TENANT, _tap(kostenart="Material"))

    assert decided == [{"approval_id": "ap-1", "approve": True, "edits": {"kostenart": "Material"}}]
    [closed] = turn.updated
    assert closed.id == "card-1"
    assert _closed_text(closed) == "✅ Freigegeben: Rechnung RE-1 buchen? – wird ausgeführt …"
    assert turn.sent == []  # answered at once, nothing else in this turn
    await asyncio.gather(*bot._background)
    [result] = bot.adapter.context.sent
    assert result.endswith("Rechnung RE-1 gebucht (BILL/1).")


async def test_a_second_tap_is_no_error(monkeypatch) -> None:
    bot = _bot(monkeypatch)

    async def already(*args, **kwargs):
        raise ApprovalDecisionError(409, "approval ap-1 is approved, not pending")

    monkeypatch.setattr(teams_bot, "decide_approval", already)
    turn = _CardTurn()

    await bot._handle_approval_callback(turn, TENANT, _tap())

    assert turn.sent == ["ℹ️ Diese Freigabe ist schon erledigt."]
    assert _closed_text(turn.updated[0]) == "Erledigt: Rechnung RE-1 buchen?"
    assert not bot._background


async def test_rejecting_closes_the_card_without_running_anything(monkeypatch, decided) -> None:
    bot = _bot(monkeypatch)
    turn = _CardTurn()

    await bot._handle_approval_callback(turn, TENANT, _tap("reject"))

    assert decided[0]["approve"] is False and decided[0]["edits"] is None
    assert _closed_text(turn.updated[0]) == "❌ Abgelehnt: Rechnung RE-1 buchen?"
    assert not bot._background


async def test_other_failures_still_show_as_errors(monkeypatch) -> None:
    bot = _bot(monkeypatch)

    async def gone(*args, **kwargs):
        raise ApprovalDecisionError(404, "no such pending approval")

    monkeypatch.setattr(teams_bot, "decide_approval", gone)
    turn = _CardTurn()

    await bot._handle_approval_callback(turn, TENANT, _tap())

    assert "no such pending approval" in turn.sent[0]
    assert turn.updated == []


def test_card_title_is_never_sent_as_an_edit() -> None:
    card = teams_bot.approval_card(PendingApproval(server="terbox", approval_id="ap-1", message="Buchen?", payload={"a": "1"}))
    data = card.content["actions"][0]["data"]
    assert data["_titel"] == "Buchen?"
    assert set(data) - set(teams_bot._CARD_KEYS) == set()


def test_report_card_column_widths_are_numbers() -> None:
    """Teams rejects the whole card ("card unsupported") for a width like "40"."""
    content = morning_report.card(morning_report.Bericht.model_validate(REPORT), NOW, None).content

    def widths(element):
        if isinstance(element, dict):
            if element.get("type") == "Column":
                yield element["width"]
            for value in element.values():
                yield from widths(value)
        elif isinstance(element, list):
            for item in element:
                yield from widths(item)

    found = list(widths(content))
    assert found and all(isinstance(w, (int, float)) or w in ("auto", "stretch") for w in found)
