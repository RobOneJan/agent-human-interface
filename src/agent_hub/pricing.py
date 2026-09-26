"""Approximate USD cost of one Claude API call, from its response.usage.

Prices are per-token (not per-MTok) so they multiply directly against token
counts - see https://platform.claude.com/docs/en/about-claude/pricing
(checked 2026-09-26). Only covers models this project has actually used or
is a plausible CLAUDE_MODEL switch-target for - an unlisted model returns
None (see `estimate_cost_usd`) rather than a silently wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelPricing:
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


def _per_mtok(input: float, cache_write_5m: float, cache_write_1h: float, cache_read: float, output: float) -> ModelPricing:
    million = 1_000_000
    return ModelPricing(
        input=input / million,
        cache_write_5m=cache_write_5m / million,
        cache_write_1h=cache_write_1h / million,
        cache_read=cache_read / million,
        output=output / million,
    )


_PRICING: dict[str, ModelPricing] = {
    "claude-sonnet-5": _per_mtok(input=2.00, cache_write_5m=2.50, cache_write_1h=4.00, cache_read=0.20, output=10.00),
    "claude-opus-5": _per_mtok(input=5.00, cache_write_5m=6.25, cache_write_1h=10.00, cache_read=0.50, output=25.00),
    "claude-haiku-4-5-20251001": _per_mtok(
        input=1.00, cache_write_5m=1.25, cache_write_1h=2.00, cache_read=0.10, output=5.00
    ),
}


class UsageLike(Protocol):
    """Structural subset of anthropic.types.Usage this module needs - lets
    tests build a plain stand-in instead of the real SDK type."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation: object | None  # anthropic.types.CacheCreation | None


def estimate_cost_usd(model: str, usage: UsageLike | None) -> float | None:
    """None for a model not in `_PRICING`, or a missing usage payload -
    callers must treat that as "unknown", never as free/zero."""
    if usage is None:
        return None
    pricing = _PRICING.get(model)
    if pricing is None:
        return None

    cache_creation = usage.cache_creation
    if cache_creation is not None:
        write_1h = cache_creation.ephemeral_1h_input_tokens  # type: ignore[attr-defined]
        write_5m = cache_creation.ephemeral_5m_input_tokens  # type: ignore[attr-defined]
    else:
        # Older/partial usage payloads only give the combined total - this
        # project always requests ttl="1h" caching (see orchestrator.py), so
        # attributing the whole total to the 1h rate is the closest estimate.
        write_1h = usage.cache_creation_input_tokens or 0
        write_5m = 0
    cache_read = usage.cache_read_input_tokens or 0

    return (
        usage.input_tokens * pricing.input
        + write_1h * pricing.cache_write_1h
        + write_5m * pricing.cache_write_5m
        + cache_read * pricing.cache_read
        + usage.output_tokens * pricing.output
    )
