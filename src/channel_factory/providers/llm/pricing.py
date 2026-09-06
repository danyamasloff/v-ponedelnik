"""Model routing and price table.

Prices are per million tokens, taken from the Anthropic pricing table. They are
kept in code rather than fetched, because a wrong price must fail a test rather
than silently change what the cost limits mean. Update deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from channel_factory.core.enums import AiTaskType

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"

# Google's documented free tier. The zero here is a *stated* price, not an
# unknown model silently costing nothing — the difference matters, because an
# unpriced model must still raise. On this tier the limiting resource is the
# daily request quota, not money, so it is capped separately.
GEMINI_FLASH = "gemini-3.8-flash"

# Cache reads are ~0.1x the input price, cache writes ~1.25x.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token prices for one model."""

    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal


MODEL_PRICES: dict[str, ModelPrice] = {
    HAIKU: ModelPrice(HAIKU, Decimal("1.00"), Decimal("5.00")),
    SONNET: ModelPrice(SONNET, Decimal("2.00"), Decimal("10.00")),
    GEMINI_FLASH: ModelPrice(GEMINI_FLASH, Decimal("0"), Decimal("0")),
}

# Routing is a policy decision, not an optimization detail: cheap models do
# structural work, the balanced model does work that needs reasoning, and Opus
# never runs in the pipeline.
TASK_MODELS: dict[AiTaskType, str] = {
    AiTaskType.EXTRACTION: HAIKU,
    AiTaskType.CLASSIFICATION: HAIKU,
    AiTaskType.SCORING: HAIKU,
    AiTaskType.DEDUP_ADJUDICATION: HAIKU,
    AiTaskType.SYNTHESIS: SONNET,
    # Search needs the current web_search tool variant, which the Haiku
    # generation does not support.
    AiTaskType.SEARCH: SONNET,
    # Post text is what readers judge the channel by, so it does not go to the
    # cheapest model. This route is the paid fallback only: content generation
    # normally runs on a local model or the Gemini free tier.
    AiTaskType.CONTENT_DRAFT: SONNET,
}


def model_for(task: AiTaskType) -> str:
    """Model id for a task type."""
    try:
        return TASK_MODELS[task]
    except KeyError as exc:  # pragma: no cover - guarded by the enum
        raise ValueError(f"no model routed for task {task}") from exc


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Cost of one call in USD."""
    price = MODEL_PRICES.get(model)
    if price is None:
        # An unknown model must not silently cost zero — that would make the
        # spend limits meaningless the moment routing changes.
        raise ValueError(f"no price known for model {model!r}; add it to MODEL_PRICES")

    total = (
        Decimal(input_tokens) * price.input_per_mtok
        + Decimal(output_tokens) * price.output_per_mtok
        + Decimal(cache_read_tokens) * price.input_per_mtok * CACHE_READ_MULTIPLIER
        + Decimal(cache_write_tokens) * price.input_per_mtok * CACHE_WRITE_MULTIPLIER
    ) / MILLION
    return total.quantize(Decimal("0.000001"))
