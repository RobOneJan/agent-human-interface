from __future__ import annotations

import json

import pytest

from agent_hub import morning_report, teams_bot
from agent_hub.orchestrator import OrchestratorResult, PendingApproval
from tests.test_jobs import (  # noqa: F401 - fixture
    NOW,
    TENANT,
    _bot,
    _clean_state,
    _FakeAdapter,
    _remember_chat,
)

REPORT = {
    "spitzname": "Graf Rechnungsstau",
    "seit": "12.11.2026 06:45",
    "rechnungen": [
        {"lieferant": "Adolf Würth GmbH & Co. KG", "nummer": "6089443652", "betrag": "145,56 €", "faellig": "21.10.2026", "hinweis": "Lastschrift"},
        {"lieferant": "Neu GmbH", "nummer": "N-1", "betrag": "320,00 €", "faellig": "25.11.2026", "hinweis": "neuer Lieferant"},
    ],
    "schon_vorhanden": [{"lieferant": "Katemann", "nummer": "20260335"}],
    "warnungen": ["Würth: andere IBAN als in Odoo"],
    "stichelei": "Sven, Würth bucht pünktlich ab.",
}


def _texts(element) -> list[str]:
    """Every TextBlock text in a card, in order."""
    found = []
    if isinstance(element, dict):
        if element.get("type") == "TextBlock":
            found.append(element["text"])
        for value in element.values():
            found += _texts(value)
    elif isinstance(element, list):
        for item in element:
            found += _texts(item)
    return found


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps(REPORT),
        "```json\n" + json.dumps(REPORT, indent=2) + "\n```",
        "Hier der Bericht:\n```\n" + json.dumps(REPORT) + "\n```\nSchönen Tag!",
    ],
)
def test_report_is_read_fenced_or_bare(answer: str) -> None:
    bericht = morning_report.parse(answer)
    assert bericht.spitzname == "Graf Rechnungsstau"
    assert [r.lieferant for r in bericht.rechnungen] == ["Adolf Würth GmbH & Co. KG", "Neu GmbH"]


@pytest.mark.parametrize("answer", ["Guten Morgen! Keine neuen Rechnungen.", "{kaputt", "[1, 2]", ""])
def test_anything_else_is_no_report(answer: str) -> None:
    assert morning_report.parse(answer) is None


def test_missing_lists_are_simply_empty() -> None:
    bericht = morning_report.parse('{"spitzname": "X", "stichelei": "Y"}')
    assert bericht.rechnungen == [] and bericht.warnungen == []


def test_card_shows_header_table_warnings_jab_and_cost() -> None:
    content = morning_report.card(morning_report.Bericht.model_validate(REPORT), NOW, "Kosten: 4.20ct").content
    texts = _texts(content)

    assert texts[0] == "📋 Büroarbeit · Montag, 09.11.2026"
    assert texts[1] == "Guten Morgen, Graf Rechnungsstau!"
    assert "Neue Rechnungen (2) seit 12.11.2026 06:45" in texts
    assert texts[texts.index("Lieferant") : texts.index("Lieferant") + 4] == ["Lieferant", "Betrag", "Fällig", "Hinweis"]
    for value in ("Adolf Würth GmbH & Co. KG", "6089443652", "145,56 €", "21.10.2026", "Lastschrift", "neuer Lieferant"):
        assert value in texts
    assert "Schon in Odoo: Katemann 20260335" in texts
    assert "⚠️ Würth: andere IBAN als in Odoo" in texts
    assert "_Sven, Würth bucht pünktlich ab._" in texts
    assert texts[-1] == "Kosten: 4.20ct"
    assert content["version"] == "1.4"


def test_card_without_new_invoices_says_so() -> None:
    texts = _texts(morning_report.card(morning_report.Bericht(seit="12.11.2026 06:45"), NOW, None).content)
    assert "Keine neuen Rechnungen seit 12.11.2026 06:45." in texts
    assert "Guten Morgen!" in texts


def test_long_lists_are_cut_with_a_note() -> None:
    many = morning_report.Bericht(rechnungen=[{"lieferant": f"L{i}"} for i in range(35)])
    texts = _texts(morning_report.card(many, NOW, None).content)
    assert "… und 5 weitere" in texts
    assert "L34" not in texts


async def test_office_job_sends_the_card_then_its_approval_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch)
    _remember_chat()

    async def fake_handle_message(*args, **kwargs):
        return (
            OrchestratorResult(
                text="```json\n" + json.dumps(REPORT) + "\n```",
                pending_approvals=[PendingApproval(server="terbox", approval_id="ap-1", message="Rechnung buchen?")],
                tool_call_count=5,
                cost_usd=0.042,
            ),
            [],
        )

    monkeypatch.setattr(teams_bot, "handle_message", fake_handle_message)
    adapter = _FakeAdapter()

    await bot.run_office_job(adapter, TENANT)

    report, approval = adapter.context.sent
    report_card = report.attachments[0].content
    assert "Guten Morgen, Graf Rechnungsstau!" in _texts(report_card)
    assert _texts(report_card)[-1] == "Kosten: 4.20ct"  # inside the card, no extra message
    assert "Rechnung buchen?" in approval.text
    assert len(adapter.context.sent) == 2


async def test_office_job_falls_back_to_text_without_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch)
    _remember_chat()

    async def fake_handle_message(*args, **kwargs):
        return OrchestratorResult(text="Guten Morgen! Keine neuen Rechnungen."), []

    monkeypatch.setattr(teams_bot, "handle_message", fake_handle_message)
    adapter = _FakeAdapter()

    await bot.run_office_job(adapter, TENANT)

    assert adapter.context.sent[0].endswith("Guten Morgen! Keine neuen Rechnungen.")
