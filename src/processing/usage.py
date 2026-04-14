"""Token usage and cost tracking for API calls.

Estimates cost from token counts using per-model pricing. Caller
passes a SyncState instance; logs are written to the api_usage table.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Pricing in USD per 1M tokens (input, output)
# Updated as of 2026-01; users should not expect these to be current forever.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (0.8, 4.0),
    # OpenAI embeddings
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    # OpenAI vision
    "gpt-4o-mini": (0.15, 0.6),
    # Voyage embeddings
    "voyage-3.5": (0.06, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate API cost in USD for a call.

    Returns 0.0 if the model is unknown (rather than raising).
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return 0.0

    input_price, output_price = pricing
    cost = (input_tokens / 1_000_000) * input_price
    cost += (output_tokens / 1_000_000) * output_price
    return cost


def log_anthropic_response(
    state,
    response,
    model: str,
    operation: str,
    doc_id: str | None = None,
) -> None:
    """Extract usage from an Anthropic response and log it to state.

    Accepts either a direct response object with .usage or a dict shape.
    Silently returns if state is None or usage info is missing.
    """
    if state is None:
        return

    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    if usage is None:
        return

    input_tokens = _get(usage, "input_tokens", 0)
    output_tokens = _get(usage, "output_tokens", 0)
    cost = estimate_cost(model, input_tokens, output_tokens)

    try:
        state.log_api_usage(
            provider="anthropic",
            model=model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            doc_id=doc_id,
        )
    except Exception as e:
        logger.debug("Failed to log API usage: %s", e)


def log_embedding_usage(
    state,
    provider: str,
    model: str,
    input_tokens: int,
    operation: str = "embed",
) -> None:
    """Log embedding API usage. Embeddings have no output tokens."""
    if state is None:
        return

    cost = estimate_cost(model, input_tokens, 0)
    try:
        state.log_api_usage(
            provider=provider,
            model=model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=0,
            cost_usd=cost,
        )
    except Exception as e:
        logger.debug("Failed to log embedding usage: %s", e)


def _get(obj, key: str, default: int = 0) -> int:
    """Get a numeric attribute or dict key."""
    if isinstance(obj, dict):
        return int(obj.get(key, default))
    return int(getattr(obj, key, default))
