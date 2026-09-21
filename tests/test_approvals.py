from __future__ import annotations

import httpx2
import pytest

from agent_hub.approvals import ApprovalDecisionError, _base_url, decide_approval


def test_base_url_strips_mcp_suffix() -> None:
    assert _base_url("https://email-mcp-server.example.com/mcp") == "https://email-mcp-server.example.com"


@pytest.fixture
def patch_transport(monkeypatch):
    """Redirect httpx2.AsyncClient to a MockTransport, and skip real Google
    auth (this is a localhost URL in every test, so fetch_auth_header already
    returns {} without needing credentials - see auth.is_local)."""

    def _install(handler):
        original_init = httpx2.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = httpx2.MockTransport(handler)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx2.AsyncClient, "__init__", patched_init)

    return _install


async def test_decide_approval_success_returns_json(patch_transport) -> None:
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        return httpx2.Response(200, json={"id": "abc", "status": "approved", "resource_id": "draft-1"})

    patch_transport(handler)

    result = await decide_approval("http://localhost:8080/mcp", "abc", approve=True)

    assert result == {"id": "abc", "status": "approved", "resource_id": "draft-1"}
    assert captured["url"] == "http://localhost:8080/internal/approvals/abc/approve"


async def test_decide_approval_reject_hits_reject_path(patch_transport) -> None:
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        return httpx2.Response(200, json={"id": "abc", "status": "rejected", "resource_id": "draft-1"})

    patch_transport(handler)

    await decide_approval("http://localhost:8080/mcp", "abc", approve=False)

    assert captured["url"] == "http://localhost:8080/internal/approvals/abc/reject"


async def test_decide_approval_404_raises_with_detail(patch_transport) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"error": "no such pending approval"})

    patch_transport(handler)

    with pytest.raises(ApprovalDecisionError) as exc_info:
        await decide_approval("http://localhost:8080/mcp", "missing", approve=True)

    assert exc_info.value.status_code == 404
    assert "no such pending approval" in exc_info.value.detail


async def test_decide_approval_409_raises_with_detail(patch_transport) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(409, json={"error": "approval abc is approved, not pending"})

    patch_transport(handler)

    with pytest.raises(ApprovalDecisionError) as exc_info:
        await decide_approval("http://localhost:8080/mcp", "abc", approve=True)

    assert exc_info.value.status_code == 409
