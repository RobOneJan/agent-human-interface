"""Standard processes ("playbooks") the agent follows, written as Markdown.

Each `.md` file in `playbooks/` (or in `PLAYBOOK_DIR`, if set) describes one
process, e.g. how to file a supplier invoice from an email. They are appended
to the system prompt once at startup, in file-name order, so editing a process
needs no code change - only a new file or an edit, then a deploy.

Read once, not per message: the system prompt is part of the cached prompt
prefix, and changing it per message would throw that cache away.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent / "playbooks"

INTRO = (
    "\n\nStandard processes of this company follow. When a request matches one, "
    "follow its steps; they take precedence over your own approach. They are "
    "written in German.\n\n"
)


def load(directory: Path | None = None) -> str:
    directory = directory or Path(os.environ.get("PLAYBOOK_DIR") or DEFAULT_DIR)
    if not directory.is_dir():
        return ""
    texts = [path.read_text(encoding="utf-8").strip() for path in sorted(directory.glob("*.md"))]
    texts = [text for text in texts if text]
    return INTRO + "\n\n---\n\n".join(texts) if texts else ""
