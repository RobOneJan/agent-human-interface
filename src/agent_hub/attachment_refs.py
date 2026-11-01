"""Lets one MCP server's file reach another server without passing through Claude.

A file arrives from one tool (e.g. email's `get_attachment`) and has to go to
another (e.g. an ERP's "store this invoice"). Claude cannot carry it: copying the
base64 into a tool call would cost hundreds of thousands of tokens, and a single
wrong character would corrupt an invoice. So Claude gets a short reference, and
`resolve_refs` swaps it for the real content on the copy of the arguments sent to
the server, leaving the history with only the reference.

In memory, like `chat_history`: a restart (this service scales to zero) loses the
references, and the error tells Claude to fetch the file again. This only has to
bridge the moment between fetching a file and handing it over; the receiving
server keeps its own copy while a human decides.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

REF_PREFIX = "attachment-ref:"
TTL_SECONDS = 3600.0

# (tenant_id, ref) -> (content, stored_at). Scoped per tenant so one chat can never
# resolve a reference to another chat's file, even if it learned the reference.
_refs: dict[tuple[str, str], tuple[bytes, float]] = {}


class UnknownAttachmentRef(Exception):
    """The reference expired, was lost in a restart, or belongs to another chat."""


def remember(tenant_id: str, content: bytes) -> str:
    _gc()
    ref = f"{REF_PREFIX}{uuid.uuid4().hex}"
    _refs[(tenant_id, ref)] = (content, time.monotonic())
    return ref


def resolve_refs(value: Any, tenant_id: str) -> Any:
    """Return `value` with each exact reference string replaced by base64 content.

    Only a whole string counts, so text that merely mentions a reference (an email
    body quoting one, say) is passed through untouched. Builds new containers and
    never mutates `value`, because the original is what stays in history.
    """
    if isinstance(value, dict):
        return {key: resolve_refs(item, tenant_id) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, tenant_id) for item in value]
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        _gc()
        entry = _refs.get((tenant_id, value))
        if entry is None:
            raise UnknownAttachmentRef(
                f"{value} is no longer available - fetch the file again to get a new reference"
            )
        return base64.b64encode(entry[0]).decode("ascii")
    return value


def _gc() -> None:
    cutoff = time.monotonic() - TTL_SECONDS
    for key in [key for key, (_, stored_at) in _refs.items() if stored_at < cutoff]:
        del _refs[key]
