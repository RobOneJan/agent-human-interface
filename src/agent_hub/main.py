from __future__ import annotations

import os


def main() -> None:
    # CHANNEL picks the adapter (see config.Settings.channel); Teams stays the
    # default so existing deployments need no change.
    if os.environ.get("CHANNEL", "teams").strip().lower() == "telegram":
        from agent_hub.telegram_bot import run
    else:
        from agent_hub.teams_bot import run
    run()


if __name__ == "__main__":
    main()
