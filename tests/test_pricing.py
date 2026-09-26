from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_hub.pricing import estimate_cost_usd


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation: object | None = None,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation=cache_creation,
    )


def test_returns_none_for_an_unlisted_model() -> None:
    assert estimate_cost_usd("some-future-model", _usage(input_tokens=1000)) is None


def test_returns_none_when_usage_is_missing() -> None:
    assert estimate_cost_usd("claude-sonnet-5", None) is None


def test_computes_plain_input_and_output_cost() -> None:
    usage = _usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd("claude-sonnet-5", usage) == pytest.approx(2.00 + 10.00)


def test_uses_the_cache_creation_breakdown_when_present() -> None:
    usage = _usage(
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=1_000_000, ephemeral_5m_input_tokens=1_000_000)
    )
    assert estimate_cost_usd("claude-sonnet-5", usage) == pytest.approx(4.00 + 2.50)


def test_falls_back_to_the_combined_total_as_a_1h_write_when_breakdown_is_missing() -> None:
    # This project always requests ttl="1h" caching (orchestrator.py), so
    # attributing the whole total to the 1h rate is the closest estimate
    # available when a usage payload has no per-TTL breakdown.
    usage = _usage(cache_creation_input_tokens=1_000_000, cache_creation=None)
    assert estimate_cost_usd("claude-sonnet-5", usage) == pytest.approx(4.00)


def test_cache_read_uses_the_read_rate() -> None:
    usage = _usage(cache_read_input_tokens=1_000_000)
    assert estimate_cost_usd("claude-sonnet-5", usage) == pytest.approx(0.20)


def test_opus_5_pricing() -> None:
    usage = _usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(5.00 + 25.00)


def test_haiku_4_5_pricing() -> None:
    usage = _usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost_usd("claude-haiku-4-5-20251001", usage) == pytest.approx(1.00 + 5.00)
