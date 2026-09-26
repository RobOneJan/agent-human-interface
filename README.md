# agent-hub

A channel-agnostic Claude agent core, with a Telegram adapter as the first
channel. Connects to one or more MCP servers (starting with
[`email-mcp-server`](../email-mcp-server)) and exposes their tools to Claude
via a manual tool-use loop.

## Why this exists

The goal is a Telegram bot today that becomes an email/Outlook/Gmail add-on
later, without a rewrite. So the design keeps two things strictly separate:

- **`orchestrator.py`** - channel-agnostic. Knows nothing about Telegram. Takes
  a plain user message, runs the Claude + MCP tool-use loop, returns text plus
  any file attachments a tool produced.
- **`telegram_bot.py`** - a thin adapter. Translates Telegram updates into
  calls to `orchestrator.handle_message()`, and translates the result back
  into Telegram replies (including sending real files via `reply_document`,
  not just describing them as text).

A future add-on channel is a sibling adapter file, not a rewrite of the core.

## Architecture

```text
Telegram   --adapter--> orchestrator.handle_message() --> McpToolHub --> email-mcp-server (MCP)
(future)                                                              --> erp-mcp-server (MCP, later)
Add-on     --adapter-->
```

- **`config.py`** - env-driven settings. `MCP_SERVERS` is `name=url,name=url,...`;
  adding a server is a config change, not a code change.
- **`mcp_client.py`** (`McpToolHub`) - connects to every configured MCP server,
  merges their tools into one list for Claude with names qualified as
  `<server>__<tool>` (so two servers can never collide on a tool name), and
  routes `call_tool` back to the right server by that prefix.
- **`auth.py`** - fetches a Google-signed identity token per MCP server URL,
  same pattern as any other caller of a Cloud-Run-IAM-protected MCP server
  (see `email-mcp-server`'s README, "Access for additional callers"). For a
  `localhost` URL (e.g. `gcloud run services proxy` during dev), skips auth
  entirely and relies on the proxy.
- **`orchestrator.py`** - the actual Claude tool-use loop. Deliberately a
  manual loop, not the SDK's Tool Runner: a `get_attachment` tool result
  needs to become a real file sent to the user, not just text Claude
  describes, which requires inspecting every raw tool result here.
- **`approvals.py`** - calls a server's `/internal/approvals/{id}/approve`
  or `.../reject` HTTP route directly (see below). Deliberately separate
  from `mcp_client.py`/`orchestrator.py`: it is never reachable from the
  tool-use loop, only from a human-triggered Telegram button.
- **`pricing.py`** - per-token USD rates for each supported `CLAUDE_MODEL`,
  used to turn a Claude API response's `usage` into an approximate cost -
  see "Cost" below.

## Approving from Telegram

When a `request_send_approval` MCP call comes back PENDING, `on_message`
sends a follow-up Telegram message with two inline buttons (`callback_data`
encodes `action:server:approval_id`, e.g. `approve:email:<uuid>`). Tapping
one fires `telegram_bot.py`'s `CallbackQueryHandler`, which calls
`approvals.decide_approval()` - a plain HTTP POST to the target MCP
server's `/internal/approvals/{id}/approve` (or `.../reject`) route.

This preserves the guarantee `email-mcp-server`'s approval flow already
has: **the LLM can never approve its own send.** The button-tap handler is
a completely different code path from `orchestrator.handle_message()` - the
model driving the tool-use loop has no tool that reaches
`approvals.decide_approval()`, and no way to synthesize a Telegram
`callback_query` update (that only exists because a human actually tapped a
button in their Telegram client). See `email-mcp-server`'s README,
"Approving from a chat channel", for the server side of this.

Auth for the approval call reuses `auth.py` exactly like any other call to
that MCP server - the calling identity (your `gcloud` login locally, or
`agent-hub-run`'s service account once deployed) needs `roles/run.invoker`
on the target Cloud Run service, same as for `/mcp` itself.

## Cost

**Per-message cost reporting.** After any reply that made at least one tool
call (`telegram_bot.MIN_TOOL_CALLS_FOR_COST_REPORT`, currently 1 - a plain
conversational reply without tools stays quiet), the bot sends a short
follow-up message like `Robert zahlt: 0.34ct` - in cents, not dollars (a
single reply's cost is normally well under a cent). "Robert" is this bot's
owner, not the tenant - every tenant's usage is billed to the same
Anthropic account regardless of which chat triggered it, so the message is
accurate for any chat, not just the owner's own. The estimate is computed
from `response.usage` on every `claude.messages.create()` call that turn
(see `pricing.py`'s per-token rates for the configured `CLAUDE_MODEL`,
checked against [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
on 2026-09-26) - it is an approximation, not a substitute for the Claude
Console's own usage/billing numbers. **Switching `CLAUDE_MODEL` to a model
not in `pricing.py`'s table silently stops cost reporting** (no tool-call
turn will show a cost line) rather than showing a wrong number - add the
new model's rates there when you switch. This exists ahead of the
scheduled/ERP-integration work below, where autonomous runs make cost
visibility matter more than it does for a manually-triggered chat reply.

No eval exists in this project, so there is no automated way to tell a real
saving from a quality regression - these levers were applied on an explicit
decision to prioritize cost over the (unmeasured) quality cost, not because
an eval cleared them. If replies start feeling worse, that trade is the
first thing to revisit - bump `CLAUDE_EFFORT` up before reaching for
`CLAUDE_MODEL`, per the stepping-down order below.

Free wins (no quality tradeoff, so these stay on regardless):

- **Prompt caching** (`orchestrator.py`'s `cache_control={"type": "ephemeral", "ttl": "1h"}`
  on every `messages.create()` call) - system prompt, tool schemas, and the
  growing conversation history were being resent and re-billed in full on
  every single message before this. The 1h TTL (not the 5m default) matches
  this loop's shape: it waits on a human typing in Telegram between turns.
- **`get_attachment` result trimming** - the raw tool result carries the
  full base64 file content (a large PDF is easily >100K tokens); Claude
  never needs those bytes since the file goes straight to the user, so the
  tool result sent back to Claude is replaced with a short confirmation
  once the attachment is captured.

Tradeoffs (capability traded for cost - applied on request, unvalidated):

- **`CLAUDE_EFFORT=low`** (`config.py`, passed as `output_config={"effort": ...}`)
  - this workload's shape (short email Q&A, usually 1-3 tool calls)
  resembles the "research and knowledge work" curve in Anthropic's own
  cost-optimization guide, where `low` gave up little accuracy for real
  savings on their benchmarks - but that curve is theirs, not measured on
  this traffic. Fixed for a whole conversation (`handle_message`'s
  docstring): changing it mid-chat would invalidate the prompt cache above.
- **`CLAUDE_MODEL=claude-sonnet-5`** (stepped down from `claude-opus-5`) -
  applied together with the effort change rather than swept one at a time
  against an eval, so if quality drops there's no data saying which lever
  caused it. Revert either independently via env var if needed.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in the values below
```

### Credentials

- **`ANTHROPIC_API_KEY`** - not a field in `config.py`; the Anthropic SDK
  resolves it from the environment (or an `ant auth login` profile) itself.
  Set it as a plain env var for a headless deployment.
- **`TELEGRAM_BOT_TOKEN`** - from [@BotFather](https://t.me/BotFather) on
  Telegram (`/newbot`).
- **`MCP_SERVERS`** - e.g. `email=http://localhost:8080/mcp` while testing
  locally through `gcloud run services proxy` (see email-mcp-server's
  README), or `email=https://<cloud-run-url>/mcp` once this itself is
  deployed with a service account that has `roles/run.invoker` on that
  service.
- **`TELEGRAM_ALLOWED_CHAT_IDS`** - comma-separated chat ids allowed to use
  the bot. There is no self-serve signup - this is the access-control list.
  Find your own chat id by messaging the bot once and checking the logs, or
  via [@userinfobot](https://t.me/userinfobot).

  Each entry is `<chat_id>` or `<chat_id>=<tenant_id>` - a bare chat_id maps
  to tenant `"default"` (this bot's original single mailbox), so an existing
  plain comma-separated list needs no changes. Use `<chat_id>=<tenant_id>`
  to let a *different* chat reach a *different* mailbox through this same
  bot - e.g. a friend's own mailbox, onboarded on the email-mcp-server side
  via `scripts/gmail_auth.py --tenant <id>` or `scripts/imap_auth.py --tenant <id>`
  (see that repo's README). Example:
  `TELEGRAM_ALLOWED_CHAT_IDS=8811142957,123456789=friend-terbox`.

  This works by sending an `X-Tenant-Id: <tenant_id>` header on every MCP
  call for that chat (see `mcp_client.py`) and a matching `?tenant=<tenant_id>`
  query param on every approval decision (see `approvals.py`) - each chat
  only ever sees and can approve sends for its own tenant's mailbox, never
  another chat's. This is not itself an authentication mechanism (a header
  is client-supplied input) - it only works because the whole connection to
  the MCP server is already gated by Cloud Run IAM (see `auth.py`), so the
  target server only trusts this bot's own already-authorized chat/tenant
  mapping because it already trusts this bot as a whole. Do not point
  `MCP_SERVERS` at a server you would not already trust with every listed
  tenant's mailbox.

### Run

```bash
agent-hub
```

Polls Telegram (no public webhook/URL needed) and answers messages from
allowed chat ids only.

## Testing

```bash
pytest
```

All tests are offline (fake Claude client, fake MCP tool hub) - no
`ANTHROPIC_API_KEY` or `TELEGRAM_BOT_TOKEN` required to run them.

## Known limitations (by design, for now)

- One MCP session is opened fresh per message (not pooled/reused across
  messages) - simplest correct thing; revisit only if latency becomes a
  real problem.
- Conversation history is in-memory only, keyed by `chat_id`
  (`telegram_bot.py`'s `chat_history` dict), capped at
  `orchestrator.MAX_HISTORY_MESSAGES`. Fine because this process is a
  singleton (`--max-instances=1`) - lost on restart/redeploy, same as
  `drop_pending_updates=True` already accepts for the Telegram side. Move to
  a real store if history needs to survive a restart.
