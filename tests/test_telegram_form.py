"""The editable approval form (Telegram Mini App) and its signed-request check."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

import agent_hub.telegram_bot as telegram_bot
import agent_hub.telegram_form as telegram_form
from agent_hub import state
from agent_hub.config import Settings
from agent_hub.telegram_form import build_web_app, verify_init_data

TOKEN = "123:geheim"


def signed_init_data(user_id: int, token: str = TOKEN, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "q1",
              "user": json.dumps({"id": user_id, "first_name": "Robert"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_signed_init_data_names_the_user():
    assert verify_init_data(signed_init_data(42), TOKEN)["id"] == 42


@pytest.mark.parametrize("init_data", [
    signed_init_data(42, token="999:andere"),          # another bot's signature
    signed_init_data(42, auth_date=int(time.time()) - 3 * 24 * 3600),  # too old
    signed_init_data(42).replace("Robert", "Mallory"),  # tampered
    "",
])
def test_unsigned_or_stale_init_data_is_refused(init_data):
    assert verify_init_data(init_data, TOKEN) is None


def test_tile_opens_the_form_in_webhook_mode():
    keyboard = telegram_bot.tile_keyboard("bauakte", "abc-1", "https://hub.example")
    first = keyboard.inline_keyboard[0][0]
    assert first.web_app.url == "https://hub.example/kachel?id=abc-1"
    assert [b.text for b in keyboard.inline_keyboard[1]] == ["✅ Freigeben", "❌ Ablehnen"]


@pytest.fixture
def form_app(monkeypatch):
    calls: list = []
    resumed: list = []

    async def fake_get(url, approval_id, tenant_id, route):
        calls.append(("get", approval_id, tenant_id))
        return {"status": "pending", "dateiname": "x.pdf"}

    async def fake_post(url, approval_id, tenant_id, route, body):
        calls.append(("post", approval_id, body))
        if body["ordner"] == "B 5":
            raise telegram_form.ApprovalDecisionError(400, "B 5 nimmt keine Dokumente auf")
        status = "approved" if body["aktion"] == "freigeben" else "rejected"
        return {"status": status, "message": "📁 x.pdf\nOrdner: B 5.1", "regel": body["kommentar"]}

    monkeypatch.setattr(telegram_form, "get_approval_route", fake_get)
    monkeypatch.setattr(telegram_form, "post_approval_route", fake_post)
    monkeypatch.setattr(telegram_bot, "AsyncAnthropic", lambda: None)
    monkeypatch.delenv("STATE_DIR", raising=False)
    settings = Settings(mcp_servers="bauakte=http://localhost:1/mcp", telegram_bot_token=TOKEN,
                        telegram_allowed_chat_ids="42")
    application = telegram_bot.build_application(settings)

    edited: list = []

    async def edit_message_text(**kw):
        edited.append(kw)

    async def resume(chat_id, tenant_id, approval_id):
        resumed.append(approval_id)

    application.bot_data["resume"] = resume
    state.save("kachel", "abc-1", {"server": "bauakte", "chat_id": 42, "message_id": 7, "options": []})
    app = build_web_app(application, TOKEN, "s3cret")
    return app, application, calls, resumed, edited


async def _client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_form_api_needs_a_telegram_signature(form_app):
    app, *_ = form_app
    client = await _client(app)
    try:
        r = await client.post("/kachel/api", json={"initData": signed_init_data(7), "id": "abc-1", "aktion": "laden"})
        assert r.status == 403  # signed, but not an allowed chat
        r = await client.post("/kachel/api", json={"initData": "user=%7B%22id%22%3A42%7D", "id": "abc-1",
                                                   "aktion": "laden"})
        assert r.status == 403
    finally:
        await client.close()


async def test_form_loads_and_approves_with_edits(form_app, monkeypatch):
    app, application, calls, resumed, edited = form_app

    async def edit_message_text(**kw):
        edited.append(kw)

    monkeypatch.setattr(type(application.bot), "edit_message_text", lambda self, **kw: edit_message_text(**kw))
    client = await _client(app)
    try:
        init = signed_init_data(42)
        r = await client.post("/kachel/api", json={"initData": init, "id": "abc-1", "aktion": "laden"})
        assert (await r.json())["dateiname"] == "x.pdf"

        r = await client.post("/kachel/api", json={"initData": init, "id": "abc-1", "aktion": "freigeben",
                                                   "projekt_id": "m", "ordner": "B 5.1", "unterordner": ""})
        assert r.status == 200 and (await r.json())["status"] == "approved"
        assert calls[-1] == ("post", "abc-1", {"aktion": "freigeben", "projekt_id": "m", "ordner": "B 5.1",
                                               "unterordner": "", "kommentar": ""})
        assert edited[0]["message_id"] == 7 and edited[0]["text"].endswith("🟢 Freigegeben.")
        import asyncio
        await asyncio.sleep(0)
        assert resumed == ["abc-1"]
    finally:
        await client.close()


async def test_form_shows_server_errors_and_rejects_with_a_comment(form_app, monkeypatch):
    app, application, calls, resumed, edited = form_app

    async def edit_message_text(**kw):
        edited.append(kw)

    monkeypatch.setattr(type(application.bot), "edit_message_text", lambda self, **kw: edit_message_text(**kw))
    client = await _client(app)
    try:
        init = signed_init_data(42)
        r = await client.post("/kachel/api", json={"initData": init, "id": "abc-1", "aktion": "freigeben",
                                                   "ordner": "B 5"})
        assert r.status == 400 and "keine Dokumente" in (await r.json())["error"]

        r = await client.post("/kachel/api", json={"initData": init, "id": "abc-1", "aktion": "ablehnen",
                                                   "ordner": "B 2.4.3", "kommentar": "Gehört nicht in die Akte"})
        assert (await r.json())["regel"] == "Gehört nicht in die Akte"
        assert "📝 Gemerkt" in edited[-1]["text"]
        assert resumed == []
    finally:
        await client.close()


async def test_webhook_needs_the_secret_header(form_app):
    app, *_ = form_app
    client = await _client(app)
    try:
        assert (await client.post("/telegram", json={"update_id": 1})).status == 403
        r = await client.get("/kachel")
        assert r.status == 200 and "telegram-web-app.js" in await r.text()
    finally:
        await client.close()
