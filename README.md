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
- No per-channel approval-gate UX yet (e.g. a Telegram inline
  approve/reject button for `send_email`) - the underlying MCP server still
  requires out-of-band human approval via `scripts/approve_request.py`
  regardless, so nothing unsafe is possible, but there is no convenient way
  to trigger that approval from Telegram yet.
- No conversation memory across messages - each message is a fresh,
  independent request to Claude. Add history if/when a real conversation
  (not one-shot Q&A) is needed.
