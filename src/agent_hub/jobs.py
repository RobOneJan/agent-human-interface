"""Scheduled work the agent does without being asked, e.g. the morning office
round at 6:45 (Cloud Scheduler -> POST /internal/jobs/buero, see teams_bot.py).

The route is public like every other route of this service (Teams must reach
/api/messages), so it checks a Google-signed OIDC token itself: only the
configured service account can start a job. The job's only output is a message
to the user's own Teams chat; anything it wants to change still goes through
the MCP servers' approval gates.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from agent_hub import state

TIMEZONE = ZoneInfo("Europe/Berlin")
# A job reads many emails and may file several invoices; a chat turn is capped
# much lower (orchestrator.MAX_TOOL_ITERATIONS).
JOB_MAX_TOOL_ITERATIONS = 60
# Never look further back than this, even after a long pause.
MAX_LOOKBACK = timedelta(days=4)
DEFAULT_LOOKBACK = timedelta(days=1)


class JobAuthError(Exception):
    pass


def _verify(token: str, audience: str) -> dict:
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)


async def verify_invoker(authorization: str | None, audience: str, invoker: str | None) -> None:
    """Raise JobAuthError unless `authorization` carries a valid Google ID token
    for `audience`, issued to the `invoker` service account."""
    if not invoker:
        raise JobAuthError("no JOB_INVOKER configured, jobs are disabled")
    if not authorization or not authorization.startswith("Bearer "):
        raise JobAuthError("missing bearer token")
    try:
        claims = await asyncio.to_thread(_verify, authorization.removeprefix("Bearer "), audience)
    except ValueError as exc:
        raise JobAuthError(f"invalid token: {exc}") from exc
    if claims.get("email") != invoker or not claims.get("email_verified"):
        raise JobAuthError("token was not issued to the configured invoker")


def since(tenant_id: str, now: datetime) -> datetime:
    """Start of the window: the last successful run, so Monday covers the
    weekend and a missed morning is caught up - bounded by MAX_LOOKBACK."""
    last = state.load("last_office_job", tenant_id)
    start = datetime.fromisoformat(last) if last else now - DEFAULT_LOOKBACK
    return max(start, now - MAX_LOOKBACK)


def office_prompt(start: datetime, now: datetime) -> str:
    return (
        "[Automatischer Morgenlauf] Arbeite den Ablauf \"Büroarbeit am Morgen\" ab. "
        f"Jetzt ist {now:%A, %d.%m.%Y %H:%M} Uhr (Europe/Berlin). Berücksichtige alle E-Mails, "
        f"die seit {start:%d.%m.%Y %H:%M} Uhr eingegangen sind."
    )


def mark_done(tenant_id: str, now: datetime) -> None:
    state.save("last_office_job", tenant_id, now.isoformat())


def now() -> datetime:
    return datetime.now(TIMEZONE)
