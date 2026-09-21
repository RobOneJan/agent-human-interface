import pytest

from agent_hub.config import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "telegram_bot_token": "test-token",
        "mcp_servers": "email=https://email.example.com/mcp",
        "telegram_allowed_chat_ids": "111,222",
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_parses_single_mcp_server() -> None:
    settings = _settings()
    assert settings.parsed_mcp_servers() == {"email": "https://email.example.com/mcp"}


def test_parses_multiple_mcp_servers() -> None:
    settings = _settings(mcp_servers="email=https://a/mcp, erp=https://b/mcp")
    assert settings.parsed_mcp_servers() == {"email": "https://a/mcp", "erp": "https://b/mcp"}


def test_rejects_malformed_mcp_server_entry() -> None:
    settings = _settings(mcp_servers="not-a-valid-entry")
    with pytest.raises(ValueError, match="invalid MCP_SERVERS entry"):
        settings.parsed_mcp_servers()


def test_rejects_empty_mcp_servers() -> None:
    settings = _settings(mcp_servers="")
    with pytest.raises(ValueError, match="at least one"):
        settings.parsed_mcp_servers()


def test_parses_allowed_chat_ids() -> None:
    settings = _settings(telegram_allowed_chat_ids=" 111, 222,333 ")
    assert settings.parsed_allowed_chat_ids() == {111, 222, 333}


def test_empty_allowed_chat_ids_is_empty_set() -> None:
    settings = _settings(telegram_allowed_chat_ids="")
    assert settings.parsed_allowed_chat_ids() == set()


def test_bare_chat_id_maps_to_default_tenant() -> None:
    settings = _settings(telegram_allowed_chat_ids="111,222")
    assert settings.parsed_chat_tenants() == {111: "default", 222: "default"}


def test_chat_id_with_explicit_tenant_is_routed_to_it() -> None:
    settings = _settings(telegram_allowed_chat_ids="111,222=friend-terbox")
    assert settings.parsed_chat_tenants() == {111: "default", 222: "friend-terbox"}


def test_parsed_allowed_chat_ids_is_derived_from_chat_tenants_keys() -> None:
    settings = _settings(telegram_allowed_chat_ids=" 111, 222=friend-terbox ")
    assert settings.parsed_allowed_chat_ids() == {111, 222}
