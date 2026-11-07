"""Tell the user why a message failed when it is something they can fix.

A bare "something went wrong" sent people to the logs for problems like an
empty Anthropic balance or an expired key, which they can solve themselves.
Only known causes get a specific message; everything else keeps the generic
one, so no internal detail ends up in the chat.
"""

from __future__ import annotations

import anthropic
import httpx2

GENERIC = "Sorry, something went wrong answering that. Try again shortly."


def _causes(exc: BaseException):
    """The exception and, for a group (MCP sessions run in task groups), its members."""
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _causes(inner)
    if exc.__cause__ is not None:
        yield from _causes(exc.__cause__)


def user_message(exc: BaseException) -> str:
    for cause in _causes(exc):
        if isinstance(cause, anthropic.BadRequestError) and "credit balance" in str(cause).lower():
            return (
                "⚠️ Das Anthropic-Guthaben ist aufgebraucht. Bitte in der Anthropic-Console "
                "unter Plans & Billing aufladen, dann geht es sofort weiter."
            )
        if isinstance(cause, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return (
                "⚠️ Der Anthropic-API-Key wird nicht akzeptiert (abgelaufen oder ungültig). "
                "Bitte einen neuen Key im Secret agent-hub-anthropic-api-key hinterlegen."
            )
        if isinstance(cause, anthropic.RateLimitError):
            return "⚠️ Gerade zu viele Anfragen an Claude. Bitte in einer Minute noch einmal versuchen."
        if isinstance(cause, (anthropic.APIConnectionError, anthropic.InternalServerError)) or (
            isinstance(cause, anthropic.APIStatusError) and cause.status_code == 529
        ):
            return "⚠️ Claude ist gerade nicht erreichbar. Bitte gleich noch einmal versuchen."
        if isinstance(cause, (httpx2.ConnectError, httpx2.TimeoutException)):
            return "⚠️ Ein angebundener Dienst (E-Mail oder ERP) antwortet gerade nicht. Bitte gleich noch einmal versuchen."
    return GENERIC
