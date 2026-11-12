"""The morning office report as one Adaptive Card instead of free text.

The "Büroarbeit am Morgen" playbook has the agent answer with a small JSON
object; `parse` reads it and `card` lays it out the same way every day: header,
a table of the invoices filed, what was already in Odoo, warnings, the closing
jab and the cost. Anything that isn't that JSON gives None from `parse`, and the
caller sends the agent's text as before - a formatting slip never loses the
report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from botbuilder.core import CardFactory
from botbuilder.schema import Attachment
from pydantic import BaseModel, Field, ValidationError

MAX_ROWS = 30
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


class Rechnung(BaseModel):
    lieferant: str
    nummer: str = ""
    betrag: str = ""
    faellig: str = ""
    hinweis: str = ""


class Vorhanden(BaseModel):
    lieferant: str
    nummer: str = ""


class Bericht(BaseModel):
    spitzname: str = ""
    seit: str = ""
    rechnungen: list[Rechnung] = Field(default_factory=list)
    schon_vorhanden: list[Vorhanden] = Field(default_factory=list)
    warnungen: list[str] = Field(default_factory=list)
    stichelei: str = ""


def parse(text: str) -> Bericht | None:
    """The report in the agent's answer, fenced or bare, or None."""
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL) or re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        return Bericht.model_validate(data) if isinstance(data, dict) else None
    except (ValueError, ValidationError):
        return None


def _text(text: str, **style) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **style}


def _row(cells: list[tuple[str, dict]], sub: str = "", **container) -> dict:
    """One table row; `sub` is a second, smaller line under the first cell (the
    invoice number) - a separate block, since Teams doesn't reliably render a
    line break inside one."""
    widths = ("40", "20", "18", "22")
    columns = []
    for index, (width, (text, style)) in enumerate(zip(widths, cells)):
        items = [_text(text, size="small", **style)]
        if index == 0 and sub:
            items.append(_text(sub, size="small", isSubtle=True, spacing="none"))
        columns.append({"type": "Column", "width": width, "items": items})
    return {"type": "ColumnSet", **container, "columns": columns}


def _hint_style(hinweis: str) -> dict:
    if "neu" in hinweis.lower():
        return {"color": "accent", "weight": "bolder"}
    if "lastschrift" in hinweis.lower():
        return {"color": "warning"}
    return {"isSubtle": True}


def card(bericht: Bericht, now: datetime, cost_line: str | None) -> Attachment:
    greeting = f"Guten Morgen, {bericht.spitzname}!" if bericht.spitzname else "Guten Morgen!"
    body: list[dict] = [
        {
            "type": "Container",
            "style": "emphasis",
            "bleed": True,
            "items": [
                _text(f"📋 Büroarbeit · {_WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y}", weight="bolder", size="medium"),
                _text(greeting, spacing="small"),
            ],
        }
    ]

    since = f" seit {bericht.seit}" if bericht.seit else ""
    rows = bericht.rechnungen[:MAX_ROWS]
    if rows:
        body.append(_text(f"Neue Rechnungen ({len(bericht.rechnungen)}){since}", weight="bolder", spacing="medium"))
        body.append(
            _row(
                [(title, {"weight": "bolder", "isSubtle": True}) for title in ("Lieferant", "Betrag", "Fällig", "Hinweis")],
                spacing="small",
            )
        )
        for rechnung in rows:
            body.append(
                _row(
                    [
                        (rechnung.lieferant, {}),
                        (rechnung.betrag, {}),
                        (rechnung.faellig, {}),
                        (rechnung.hinweis, _hint_style(rechnung.hinweis)),
                    ],
                    sub=rechnung.nummer,
                    separator=True,
                    spacing="small",
                )
            )
        if len(bericht.rechnungen) > MAX_ROWS:
            body.append(_text(f"… und {len(bericht.rechnungen) - MAX_ROWS} weitere", isSubtle=True, size="small"))
    else:
        body.append(_text(f"Keine neuen Rechnungen{since}.", spacing="medium"))

    if bericht.schon_vorhanden:
        already = ", ".join(f"{v.lieferant} {v.nummer}".strip() for v in bericht.schon_vorhanden)
        body.append(_text(f"Schon in Odoo: {already}", isSubtle=True, size="small", spacing="medium"))

    for warnung in bericht.warnungen:
        body.append(_text(f"⚠️ {warnung}", color="attention", spacing="small"))

    if bericht.stichelei:
        body.append(_text(f"_{bericht.stichelei}_", spacing="large", separator=True))

    if cost_line:
        body.append(_text(cost_line, isSubtle=True, size="small", horizontalAlignment="right", spacing="small"))

    return CardFactory.adaptive_card(
        {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "msteams": {"width": "Full"},
            "body": body,
        }
    )
