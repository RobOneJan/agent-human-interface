from agent_hub.orchestrator import OrchestratorResult, PendingApproval, Status
from agent_hub.telegram_bot import (
    approval_callback_data,
    approval_keyboard,
    format_cost_line,
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


def test_format_cost_line_reports_when_tools_were_used() -> None:
    result = OrchestratorResult(text="ok", tool_call_count=2, cost_usd=0.0034)
    assert format_cost_line(result) == "Robert zahlt: 0.34ct"


def test_format_cost_line_reports_for_a_single_call_too() -> None:
    result = OrchestratorResult(text="ok", tool_call_count=1, cost_usd=0.001)
    assert format_cost_line(result) == "Robert zahlt: 0.10ct"


def test_format_cost_line_reports_even_without_any_tool_calls() -> None:
    # For now cost is reported on every reply, including a plain
    # conversational one - see MIN_TOOL_CALLS_FOR_COST_REPORT.
    result = OrchestratorResult(text="hi", tool_call_count=0, cost_usd=0.001)
    assert format_cost_line(result) == "Robert zahlt: 0.10ct"


def test_format_cost_line_is_none_when_cost_is_unknown() -> None:
    # cost_usd=None means "unpriced model", not "free" - must stay silent,
    # never show $0.00 (see pricing.py / OrchestratorResult docstring).
    result = OrchestratorResult(text="ok", tool_call_count=3, cost_usd=None)
    assert format_cost_line(result) is None


def test_pending_lists_its_fields_read_only_under_the_message() -> None:
    from agent_hub.telegram_bot import format_pending

    pending = PendingApproval(server="bauakte", approval_id="a1", message="📁 Ablage: B2/2.5/KW36.xlsx",
                              payload={"eiu_nr": "B 2.5", "dokumentart": "", "dateiname": "KW36.xlsx"})
    assert format_pending(pending).splitlines() == [
        "\U0001f7e1 📁 Ablage: B2/2.5/KW36.xlsx", "eiu_nr: B 2.5", "dateiname: KW36.xlsx",
    ]


def test_channel_defaults_to_teams(monkeypatch) -> None:
    import agent_hub.main as main_module
    import agent_hub.teams_bot as teams_bot
    import agent_hub.telegram_bot as telegram_bot

    gestartet = []
    monkeypatch.setattr(teams_bot, "run", lambda: gestartet.append("teams"))
    monkeypatch.setattr(telegram_bot, "run", lambda: gestartet.append("telegram"))
    monkeypatch.delenv("CHANNEL", raising=False)
    main_module.main()
    monkeypatch.setenv("CHANNEL", "telegram")
    main_module.main()
    assert gestartet == ["teams", "telegram"]


async def test_approving_in_telegram_resumes_the_agent(monkeypatch) -> None:
    """Like Teams: after the tap, the agent gets the resume prompt and its new
    reply is posted - otherwise an approved filing or send never happens."""
    from types import SimpleNamespace

    import agent_hub.telegram_bot as telegram_bot
    from agent_hub.config import Settings

    prompts: list[str] = []

    async def fake_decide(url, approval_id, tenant_id, *, approve, edits=None):
        return {"status": "approved"}

    async def fake_handle(text, hub, claude, model, history=None, effort="low"):
        prompts.append(text)
        return OrchestratorResult(text="abgelegt"), []

    class FakeHub:
        def __init__(self, servers, tenant_id):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(telegram_bot, "decide_approval", fake_decide)
    monkeypatch.setattr(telegram_bot, "handle_message", fake_handle)
    monkeypatch.setattr(telegram_bot, "McpToolHub", FakeHub)
    monkeypatch.setattr(telegram_bot, "AsyncAnthropic", lambda: None)

    settings = Settings(mcp_servers="bauakte=http://localhost:1/mcp", telegram_bot_token="1:x",
                        telegram_allowed_chat_ids="42")
    app = telegram_bot.build_application(settings)
    callback = next(h for h in app.handlers[0] if isinstance(h, telegram_bot.CallbackQueryHandler)).callback

    replies: list[str] = []

    async def reply_text(text, **_):
        replies.append(text)

    async def noop(*_, **__):
        return None

    message = SimpleNamespace(text="🟡 Ablage …", reply_text=reply_text)
    query = SimpleNamespace(data="approve:bauakte:abc-1", message=message, answer=noop, edit_message_text=noop)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42))
    await callback(update, None)

    assert prompts == [telegram_bot.RESUME_PROMPT.format(approval_id="abc-1")]
    assert replies[0].endswith("abgelegt")


# --- compact tiles (bauakte-mcp): change the folder in place, say why on reject


def _tile_app(monkeypatch, calls):
    from types import SimpleNamespace

    import agent_hub.telegram_bot as telegram_bot
    from agent_hub.config import Settings

    async def fake_decide(url, approval_id, tenant_id, *, approve, edits=None):
        calls.append(("decide", approval_id, approve))
        return {"status": "approved" if approve else "rejected"}

    async def fake_route(url, approval_id, tenant_id, route, body):
        calls.append((route, approval_id, body))
        if route == "ordner":
            return {"message": f"📁 x.pdf\nOrdner: {body['ordner']}"}
        return {"regel": body["text"]}

    monkeypatch.setattr(telegram_bot, "decide_approval", fake_decide)
    monkeypatch.setattr(telegram_bot, "post_approval_route", fake_route)
    monkeypatch.setattr(telegram_bot, "AsyncAnthropic", lambda: None)
    monkeypatch.delenv("STATE_DIR", raising=False)
    settings = Settings(mcp_servers="bauakte=http://localhost:1/mcp", telegram_bot_token="1:x",
                        telegram_allowed_chat_ids="42")
    app = telegram_bot.build_application(settings)
    handlers = app.handlers[0]
    callback = next(h for h in handlers if isinstance(h, telegram_bot.CallbackQueryHandler)).callback
    on_message = next(h for h in handlers if isinstance(h, telegram_bot.MessageHandler)).callback
    return SimpleNamespace(callback=callback, on_message=on_message, telegram_bot=telegram_bot)


def _message(text, replies, edits=None):
    from types import SimpleNamespace

    async def reply_text(t, **_):
        replies.append(t)

    async def edit_text(t, **_):
        (edits if edits is not None else []).append(t)

    async def noop(*_, **__):
        return None

    msg = SimpleNamespace(text=text, message_id=7, reply_text=reply_text)
    return msg, SimpleNamespace(message=msg, answer=noop, edit_message_text=edit_text,
                                edit_message_reply_markup=noop)


def test_compact_tile_shows_only_its_message_and_has_a_folder_button() -> None:
    from agent_hub.telegram_bot import approval_keyboard, format_pending

    pending = PendingApproval(server="bauakte", approval_id="a" * 36, message="📁 x.pdf\nProjekt: M\nOrdner: B 2.4.3",
                              payload={"eiu_nr": "B 2.4.3"}, compact=True, options=["B 5.1 Kontrollmessungen"])
    assert format_pending(pending) == "🟡 📁 x.pdf\nProjekt: M\nOrdner: B 2.4.3"
    labels = [b.text for row in approval_keyboard(pending).inline_keyboard for b in row]
    assert labels == ["✅ Freigeben", "❌ Ablehnen", "📁 Ordner ändern"]


def test_folder_choices_fit_telegrams_64_byte_callback_data() -> None:
    from agent_hub.telegram_bot import folder_keyboard

    uuid = "76fbf894-60d8-498b-a0f9-c407e669f128"
    keyboard = folder_keyboard("bauakte", uuid, ["B 6.2.3 Sachverständigerabnahmen / Fachtechnische Prüfungen"])
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data[0] == f"o:bauakte:{uuid}|B 6.2.3"
    assert all(len(d.encode()) <= 64 for d in data)


async def test_picking_a_folder_option_updates_the_tile(monkeypatch) -> None:
    from types import SimpleNamespace

    calls: list = []
    app = _tile_app(monkeypatch, calls)
    edits: list[str] = []
    _, query = _message("🟡 📁 x.pdf", [], edits)
    query.data = "o:bauakte:abc-1|B 5.1"
    await app.callback(SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42)), None)
    assert calls == [("ordner", "abc-1", {"ordner": "B 5.1"})]
    assert edits == ["🟡 📁 x.pdf\nOrdner: B 5.1"]


async def test_typed_folder_goes_to_the_server_not_the_agent(monkeypatch) -> None:
    from types import SimpleNamespace

    calls: list = []
    app = _tile_app(monkeypatch, calls)
    replies: list[str] = []
    _, query = _message("🟡 📁 x.pdf", replies)
    query.data = "frei:bauakte:abc-1"
    chat = SimpleNamespace(id=42)
    await app.callback(SimpleNamespace(callback_query=query, effective_chat=chat), None)
    assert "Welcher Ordner?" in replies[-1]

    edited: list = []

    async def edit_message_text(**kw):
        edited.append(kw)

    context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit_message_text))
    antwort, _ = _message("B 5.2/Firmen", replies)
    await app.on_message(SimpleNamespace(effective_chat=chat, message=antwort), context)
    assert calls == [("ordner", "abc-1", {"ordner": "B 5.2/Firmen"})]
    assert edited[0]["message_id"] == 7 and "B 5.2/Firmen" in edited[0]["text"]


async def test_reject_asks_why_and_keeps_the_answer_as_a_rule(monkeypatch) -> None:
    from types import SimpleNamespace

    calls: list = []
    app = _tile_app(monkeypatch, calls)
    replies: list[str] = []
    edits: list[str] = []
    _, query = _message("🟡 📁 x.pdf", replies, edits)
    query.data = "nein:bauakte:abc-1"
    chat = SimpleNamespace(id=42)
    await app.callback(SimpleNamespace(callback_query=query, effective_chat=chat), None)
    assert calls == [("decide", "abc-1", False)]
    assert edits == ["📁 x.pdf\n\n🔴 Abgelehnt."]
    assert "Warum" in replies[-1]

    antwort, _ = _message("Leistungsanfragen gehören nicht in die Bauakte", replies)
    await app.on_message(SimpleNamespace(effective_chat=chat, message=antwort), None)
    assert calls[-1] == ("begruendung", "abc-1", {"text": "Leistungsanfragen gehören nicht in die Bauakte"})
    assert replies[-1] == "📝 Gemerkt: „Leistungsanfragen gehören nicht in die Bauakte“"
