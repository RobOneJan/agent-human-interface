"""An editable approval in Telegram: a Mini App form behind the tile's
"Bearbeiten" button, plus the web server that carries both it and the
Telegram webhook.

A Telegram message cannot hold input fields; a Mini App can. The tile's
button opens `/kachel?id=<approval>` inside Telegram. The page reads and
decides the approval through `/kachel/api`, which only acts for a request
whose `initData` Telegram signed with this bot's token for an allowed user -
the same human-only boundary as a button tap (see approvals.py), never
reachable from the agent's tool loop. The server behind it is the approval's
own (bauakte-mcp's `formular` route): it validates the edits and turns a
rejection comment into a rule.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from agent_hub import state
from agent_hub.approvals import ApprovalDecisionError, get_approval_route, post_approval_route

logger = logging.getLogger(__name__)

INIT_DATA_MAX_AGE = 24 * 3600


def verify_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """The Telegram user of a Mini App request, if Telegram signed `initData`
    with this bot's token (https://core.telegram.org/bots/webapps, "Validating
    data received via the Mini App") and it is less than a day old."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def signed(fields: dict[str, str]) -> bool:
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received)

    # Newer clients add `signature` (for third-party checks); depending on the
    # client it is or is not part of what `hash` covers.
    without_signature = {k: v for k, v in pairs.items() if k != "signature"}
    if not (signed(pairs) or signed(without_signature)):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


def build_web_app(application: Application, bot_token: str, webhook_secret: str) -> web.Application:
    """`/telegram` (webhook), `/kachel` (the form), `/kachel/api`, `/healthz`."""
    data = application.bot_data
    chat_tenants: dict[int, str] = data["chat_tenants"]
    mcp_servers: dict[str, str] = data["mcp_servers"]

    async def telegram_webhook(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret):
            return web.Response(status=403)
        update = Update.de_json(await request.json(), application.bot)
        await application.update_queue.put(update)
        return web.Response(text="ok")

    async def form_page(_request: web.Request) -> web.Response:
        return web.Response(text=FORM_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def form_api(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        user = verify_init_data(str(body.get("initData", "")), bot_token)
        tenant_id = chat_tenants.get(int(user["id"])) if user else None
        if tenant_id is None:
            return web.json_response({"error": "Nicht angemeldet - bitte aus Telegram öffnen."}, status=403)
        approval_id = str(body.get("id", ""))
        kachel = state.load("kachel", approval_id) or {}
        server_url = mcp_servers.get(kachel.get("server", "bauakte"))
        if server_url is None:
            return web.json_response({"error": "Unbekannter Server."}, status=404)

        aktion = body.get("aktion")
        try:
            if aktion == "laden":
                return web.json_response(await get_approval_route(server_url, approval_id, tenant_id, "formular"))
            if aktion not in ("freigeben", "ablehnen"):
                return web.json_response({"error": "unbekannte Aktion"}, status=400)
            felder = {k: str(body.get(k) or "") for k in ("projekt_id", "ordner", "unterordner", "kommentar")}
            antwort = await post_approval_route(server_url, approval_id, tenant_id, "formular",
                                                {"aktion": aktion, **felder})
        except ApprovalDecisionError as exc:
            return web.json_response({"error": exc.detail}, status=exc.status_code)

        # Close the tile in the chat and, once approved, let the agent file it.
        chat_id = kachel.get("chat_id") or int(user["id"])
        text = antwort["message"] + ("\n\n🟢 Freigegeben." if aktion == "freigeben" else "\n\n🔴 Abgelehnt.")
        if antwort.get("regel"):
            text += f"\n📝 Gemerkt: „{antwort['regel']}“"
        if kachel.get("message_id"):
            try:
                await application.bot.edit_message_text(chat_id=chat_id, message_id=kachel["message_id"], text=text)
            except Exception:  # an already edited or deleted tile must not fail the decision
                logger.warning("could not update tile for approval %s", approval_id, exc_info=True)
        if aktion == "freigeben":
            asyncio.create_task(data["resume"](chat_id, tenant_id, approval_id))
        return web.json_response({"status": antwort["status"], "regel": antwort.get("regel", "")})

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/telegram", telegram_webhook)
    app.router.add_get("/kachel", form_page)
    app.router.add_post("/kachel/api", form_api)
    app.router.add_get("/healthz", health)
    return app


async def serve_webhook(application: Application, *, port: int, public_url: str, bot_token: str,
                        webhook_secret: str) -> None:
    """Runs until the process is stopped (Cloud Run sends SIGTERM)."""
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=f"{public_url.rstrip('/')}/telegram", secret_token=webhook_secret,
                                      allowed_updates=Update.ALL_TYPES)
    runner = web.AppRunner(build_web_app(application, bot_token, webhook_secret))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("agent-hub (Telegram webhook + Bauakte form) listening on %s", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await application.stop()
        await application.shutdown()


FORM_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ablagevorschlag</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #ffffff);
    --text: var(--tg-theme-text-color, #1b1b1b);
    --hint: var(--tg-theme-hint-color, #8a8a8a);
    --field: var(--tg-theme-secondary-bg-color, #f2f2f5);
    --accent: var(--tg-theme-button-color, #2a7fd4);
    --accent-text: var(--tg-theme-button-text-color, #ffffff);
    --danger: var(--tg-theme-destructive-text-color, #d23c3c);
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--text);
         font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  h1 { font-size: 17px; margin: 0 0 4px; word-break: break-word; }
  .betreff { color: var(--hint); font-size: 14px; margin-bottom: 16px; word-break: break-word; }
  label { display: block; font-size: 13px; color: var(--hint); margin: 14px 0 6px; }
  input, select, textarea { width: 100%; padding: 11px 12px; border: 0; border-radius: 10px;
         background: var(--field); color: var(--text); font: inherit; }
  textarea { min-height: 72px; resize: vertical; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chips button { border: 1px solid var(--accent); background: transparent; color: var(--accent);
         border-radius: 14px; padding: 4px 10px; font-size: 13px; cursor: pointer; }
  .hinweis { font-size: 13px; color: var(--hint); margin-top: 6px; }
  .knoepfe { display: flex; gap: 10px; margin-top: 22px; }
  .knoepfe button { flex: 1; padding: 13px; border: 0; border-radius: 10px; font: inherit; font-weight: 600; cursor: pointer; }
  #freigeben { background: var(--accent); color: var(--accent-text); }
  #ablehnen { background: var(--field); color: var(--danger); }
  button:disabled { opacity: .5; }
  #meldung { margin-top: 14px; min-height: 1.4em; }
  .fehler { color: var(--danger); }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<div id="form" hidden>
  <h1 id="datei"></h1>
  <div class="betreff" id="betreff"></div>

  <label for="projekt">Projekt</label>
  <select id="projekt"></select>

  <label for="ordner">Ordner</label>
  <input id="ordner" list="ordnerliste" autocomplete="off">
  <datalist id="ordnerliste"></datalist>
  <div class="chips" id="vorschlaege"></div>

  <label for="unterordner">Unterordner (optional)</label>
  <input id="unterordner" placeholder="z. B. Firmen oder AR5" autocomplete="off">
  <div class="hinweis" id="ersetzt" hidden></div>

  <label for="kommentar">Kommentar beim Ablehnen</label>
  <textarea id="kommentar" placeholder="Warum ist der Vorschlag falsch? Wird als Regel gemerkt."></textarea>

  <div class="knoepfe">
    <button id="ablehnen" type="button">Ablehnen</button>
    <button id="freigeben" type="button">Freigeben</button>
  </div>
</div>
<div id="meldung">Lädt …</div>
<script>
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  const id = new URLSearchParams(location.search).get("id") || "";
  const $ = (x) => document.getElementById(x);
  const meldung = (text, fehler) => { $("meldung").textContent = text; $("meldung").className = fehler ? "fehler" : ""; };

  async function api(body) {
    const r = await fetch("/kachel/api", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData: tg ? tg.initData : "", id, ...body }) });
    const data = await r.json().catch(() => ({ error: "Keine Antwort vom Server." }));
    if (!r.ok) throw new Error(data.error || "Fehler");
    return data;
  }

  async function laden() {
    try {
      const f = await api({ aktion: "laden" });
      if (f.status !== "pending") { meldung("Dieser Vorschlag ist schon erledigt (" + f.status + ")."); return; }
      $("datei").textContent = f.dateiname;
      $("betreff").textContent = f.betreff || "";
      for (const p of f.projekte) {
        const o = new Option(p.name, p.id, false, p.id === f.projekt_id);
        $("projekt").add(o);
      }
      $("ordner").value = f.ordner;
      for (const name of f.ordnerliste) { const o = document.createElement("option"); o.value = name; $("ordnerliste").append(o); }
      for (const v of f.vorschlaege) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = v;
        b.onclick = () => { $("ordner").value = v; $("unterordner").value = ""; };
        $("vorschlaege").append(b);
      }
      $("unterordner").value = f.unterordner || "";
      if (f.ersetzt && f.ersetzt.length) {
        $("ersetzt").hidden = false;
        $("ersetzt").textContent = "Ersetzt: " + f.ersetzt.join(", ") + " (kommt nach 'alt')";
      }
      $("form").hidden = false;
      meldung("");
    } catch (e) { meldung(e.message, true); }
  }

  async function entscheiden(aktion) {
    $("freigeben").disabled = $("ablehnen").disabled = true;
    meldung(aktion === "freigeben" ? "Wird freigegeben …" : "Wird abgelehnt …");
    try {
      const r = await api({ aktion, projekt_id: $("projekt").value, ordner: $("ordner").value,
                            unterordner: $("unterordner").value, kommentar: $("kommentar").value });
      meldung(aktion === "freigeben" ? "✅ Freigegeben – wird abgelegt." :
              "❌ Abgelehnt." + (r.regel ? " Als Regel gemerkt." : ""));
      setTimeout(() => tg && tg.close(), 900);
    } catch (e) {
      meldung(e.message, true);
      $("freigeben").disabled = $("ablehnen").disabled = false;
    }
  }
  $("freigeben").onclick = () => entscheiden("freigeben");
  $("ablehnen").onclick = () => entscheiden("ablehnen");
  laden();
</script>
</body>
</html>
"""
