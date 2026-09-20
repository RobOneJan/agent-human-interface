import pytest

from agent_hub.mcp_client import qualify_tool_name, split_tool_name


def test_qualify_then_split_roundtrips() -> None:
    qualified = qualify_tool_name("email", "search_emails")
    assert qualified == "email__search_emails"
    assert split_tool_name(qualified) == ("email", "search_emails")


def test_split_rejects_unqualified_name() -> None:
    with pytest.raises(ValueError, match="not a qualified tool name"):
        split_tool_name("search_emails")


def test_tool_name_itself_may_contain_underscores() -> None:
    qualified = qualify_tool_name("erp", "get_order_sum")
    assert split_tool_name(qualified) == ("erp", "get_order_sum")
