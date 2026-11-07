"""Small per-tenant state that must outlive this process: where to reach a
user in Teams (their conversation reference) and when the morning job last ran.

The service scales to zero, so in-memory state is gone by the next morning.
With `STATE_DIR` set (a mounted bucket in production, same stopgap as
email-mcp-server's ./var) each value is one JSON file; without it, state lives
in memory only, which is enough for tests and local runs. Single instance only
(--max-instances=1), so no locking beyond the process.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_memory: dict[str, Any] = {}


def _path(kind: str, tenant_id: str) -> Path | None:
    directory = os.environ.get("STATE_DIR")
    if not directory:
        return None
    # tenant ids are Azure AD GUIDs; anything else must not shape a path.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tenant_id)
    return Path(directory) / kind / f"{safe}.json"


def save(kind: str, tenant_id: str, value: Any) -> None:
    _memory[f"{kind}/{tenant_id}"] = value
    path = _path(kind, tenant_id)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))


def load(kind: str, tenant_id: str) -> Any:
    key = f"{kind}/{tenant_id}"
    if key in _memory:
        return _memory[key]
    path = _path(kind, tenant_id)
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text())
    _memory[key] = value
    return value


def tenants(kind: str) -> list[str]:
    """Tenants with a stored value of this kind."""
    found = {key.split("/", 1)[1] for key in _memory if key.startswith(f"{kind}/")}
    directory = os.environ.get("STATE_DIR")
    if directory and (Path(directory) / kind).is_dir():
        found |= {path.stem for path in (Path(directory) / kind).glob("*.json")}
    return sorted(found)
