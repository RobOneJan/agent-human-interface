from __future__ import annotations

# Teams is the primary channel now - see teams_bot.py's module docstring.
# telegram_bot.py stays importable (not deleted) in case both channels need
# to run from one process later; swap this import back to re-enable it.
from agent_hub.teams_bot import run

# from agent_hub.telegram_bot import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
