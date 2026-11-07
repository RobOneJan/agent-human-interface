from __future__ import annotations

import anthropic
import httpx2

from agent_hub import errors, orchestrator, playbooks


def _response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_bundled_playbooks_are_part_of_the_system_prompt() -> None:
    assert "Rechnung aus E-Mail ablegen" in orchestrator.SYSTEM
    assert orchestrator.SYSTEM.startswith(orchestrator.SYSTEM_PROMPT)


def test_playbooks_load_in_name_order_and_skip_empty_files(tmp_path) -> None:
    (tmp_path / "b.md").write_text("# Zweiter Ablauf")
    (tmp_path / "a.md").write_text("# Erster Ablauf")
    (tmp_path / "leer.md").write_text("  \n")
    (tmp_path / "notizen.txt").write_text("ignoriert")

    text = playbooks.load(tmp_path)

    assert text.index("Erster") < text.index("Zweiter")
    assert "ignoriert" not in text


def test_no_playbooks_add_nothing(tmp_path) -> None:
    assert playbooks.load(tmp_path) == ""
    assert playbooks.load(tmp_path / "missing") == ""


def test_empty_credit_balance_is_named() -> None:
    exc = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.", response=_response(400), body=None
    )
    assert "Guthaben ist aufgebraucht" in errors.user_message(exc)


def test_invalid_key_is_named_even_inside_an_exception_group() -> None:
    exc = anthropic.AuthenticationError("invalid x-api-key", response=_response(401), body=None)
    assert "API-Key" in errors.user_message(ExceptionGroup("mcp", [ValueError("x"), exc]))


def test_other_bad_requests_and_unknown_errors_stay_generic() -> None:
    exc = anthropic.BadRequestError("messages: invalid", response=_response(400), body=None)
    assert errors.user_message(exc) == errors.GENERIC
    assert errors.user_message(RuntimeError("internal detail")) == errors.GENERIC


def test_overloaded_and_unreachable_services() -> None:
    assert "nicht erreichbar" in errors.user_message(
        anthropic.APIStatusError("overloaded", response=_response(529), body=None)
    )
    assert "Dienst" in errors.user_message(httpx2.ConnectError("refused"))
