from __future__ import annotations

from datetime import datetime, timedelta

import anthropic
import httpx2
import pytest
from botbuilder.schema import ChannelAccount, ConversationAccount, ConversationReference

from agent_hub import jobs, state, teams_bot
from agent_hub.config import Settings
from agent_hub.orchestrator import OrchestratorResult, PendingApproval

TENANT = "96b89eb1-e3be-4726-a38d-b154b2e7f873"
NOW = datetime(2026, 11, 9, 6, 45, tzinfo=jobs.TIMEZONE)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    state._memory.clear()
    yield
    state._memory.clear()


# --- who may start a job -------------------------------------------------------


async def test_job_needs_a_configured_invoker() -> None:
    with pytest.raises(jobs.JobAuthError, match="disabled"):
        await jobs.verify_invoker("Bearer x", "aud", None)


async def test_job_needs_a_bearer_token() -> None:
    with pytest.raises(jobs.JobAuthError, match="missing"):
        await jobs.verify_invoker(None, "aud", "sched@p.iam.gserviceaccount.com")


async def test_token_of_another_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "_verify", lambda token, audience: {"email": "other@x.com", "email_verified": True})
    with pytest.raises(jobs.JobAuthError, match="not issued"):
        await jobs.verify_invoker("Bearer t", "aud", "sched@p.iam.gserviceaccount.com")


async def test_invalid_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad(token, audience):
        raise ValueError("Token has wrong audience")

    monkeypatch.setattr(jobs, "_verify", bad)
    with pytest.raises(jobs.JobAuthError, match="invalid token"):
        await jobs.verify_invoker("Bearer t", "aud", "sched@p.iam.gserviceaccount.com")


async def test_scheduler_token_is_accepted_for_the_right_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def ok(token, audience):
        seen.update(token=token, audience=audience)
        return {"email": "sched@p.iam.gserviceaccount.com", "email_verified": True}

    monkeypatch.setattr(jobs, "_verify", ok)
    await jobs.verify_invoker("Bearer abc", "https://hub/internal/jobs/buero", "sched@p.iam.gserviceaccount.com")
    assert seen == {"token": "abc", "audience": "https://hub/internal/jobs/buero"}


# --- which emails a run covers ---------------------------------------------------


def test_first_run_covers_the_last_day() -> None:
    assert jobs.since(TENANT, NOW) == NOW - timedelta(days=1)


def test_next_run_starts_where_the_last_successful_one_ended() -> None:
    friday = NOW - timedelta(days=3)
    jobs.mark_done(TENANT, friday)
    state._memory.clear()  # survives a restart
    assert jobs.since(TENANT, NOW) == friday  # Monday covers the weekend


def test_lookback_is_bounded_after_a_long_pause() -> None:
    jobs.mark_done(TENANT, NOW - timedelta(days=30))
    assert jobs.since(TENANT, NOW) == NOW - jobs.MAX_LOOKBACK


def test_state_files_cannot_escape_the_state_dir(tmp_path) -> None:
    state.save("conversation", "../../etc/passwd", {"x": 1})
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*.json"))
    assert list((tmp_path / "conversation").glob("*.json"))


# --- the run itself ------------------------------------------------------------------


class _FakeTurnContext:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_activity(self, activity) -> None:
        self.sent.append(activity)


class _FakeAdapter:
    def __init__(self) -> None:
        self.context = _FakeTurnContext()
        self.references: list = []

    async def continue_conversation(self, reference, callback, bot_app_id=None):
        self.references.append((reference, bot_app_id))
        await callback(self.context)


class _NoHub:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def _bot(monkeypatch: pytest.MonkeyPatch) -> teams_bot.TeamsBot:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    settings = Settings(mcp_servers="email=http://email/mcp,terbox=http://erp/mcp", teams_app_id="app-1")
    monkeypatch.setattr(teams_bot, "McpToolHub", _NoHub)
    monkeypatch.setattr(teams_bot.jobs, "now", lambda: NOW)
    return teams_bot.TeamsBot(settings, settings.parsed_mcp_servers(), teams_bot._AttachmentStore())


def _remember_chat() -> None:
    reference = ConversationReference(
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/emea/",
        conversation=ConversationAccount(id="a:chat-1", tenant_id=TENANT),
        user=ChannelAccount(id="29:user"),
        bot=ChannelAccount(id="28:bot"),
    )
    state.save("conversation", TENANT, reference.serialize())


async def test_without_a_known_chat_the_run_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch)
    status = await bot.run_office_job(_FakeAdapter(), TENANT)
    assert status.startswith("skipped")


async def test_run_posts_one_summary_and_its_approval_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch)
    _remember_chat()
    prompts = []

    async def fake_handle_message(text, tool_hub, claude, model, history, effort, max_tool_iterations):
        prompts.append((text, history, max_tool_iterations))
        result = OrchestratorResult(
            text="Guten Morgen! 2 Rechnungen abgelegt.",
            pending_approvals=[PendingApproval(server="terbox", approval_id="ap-1", message="Neuer Lieferant?")],
            tool_call_count=7,
        )
        return result, [{"role": "user", "content": text}]

    monkeypatch.setattr(teams_bot, "handle_message", fake_handle_message)
    adapter = _FakeAdapter()

    status = await bot.run_office_job(adapter, TENANT)

    [(prompt, history, cap)] = prompts
    assert "Büroarbeit am Morgen" in prompt
    assert "seit 08.11.2026 06:45" in prompt
    assert history is None and cap == jobs.JOB_MAX_TOOL_ITERATIONS
    reference, app_id = adapter.references[0]
    assert reference.conversation.id == "a:chat-1" and app_id == "app-1"
    texts = [a if isinstance(a, str) else a.text for a in adapter.context.sent]
    assert texts[0].endswith("Guten Morgen! 2 Rechnungen abgelegt.")
    assert any("Neuer Lieferant?" in t for t in texts)
    assert bot._chat_history[TENANT]  # approving a card continues this run
    assert status.startswith("done")
    assert state.load("last_office_job", TENANT) == NOW.isoformat()


async def test_failed_run_says_why_and_is_retried_next_time(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch)
    _remember_chat()

    async def broke(*args, **kwargs):
        response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        raise anthropic.BadRequestError("Your credit balance is too low", response=response, body=None)

    monkeypatch.setattr(teams_bot, "handle_message", broke)
    adapter = _FakeAdapter()

    status = await bot.run_office_job(adapter, TENANT)

    assert status.startswith("failed")
    assert "Guthaben" in adapter.context.sent[0]
    assert state.load("last_office_job", TENANT) is None  # the window stays open


def test_stored_chat_reference_round_trips() -> None:
    _remember_chat()
    state._memory.clear()  # as after a restart
    restored = ConversationReference().deserialize(state.load("conversation", TENANT))
    assert restored.service_url == "https://smba.trafficmanager.net/emea/"
    assert restored.conversation.tenant_id == TENANT
