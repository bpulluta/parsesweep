"""Model pricing from LiteLLM's bundled cost map, plus pre-flight estimates."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Use the bundled cost map; never fetch pricing over the network at import.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import litellm  # noqa: E402

_PROVIDER_PREFIXES = ("openai", "azure", "anthropic", "gemini", "vertex_ai")

CHARS_PER_TOKEN = 4
OUTPUT_TOKEN_RATIO = 0.1
TOKENS_PER_SECOND = 100


def _candidates(model: str):
    bare = model.split("/", 1)[-1]
    yield bare
    for prefix in _PROVIDER_PREFIXES:
        yield f"{prefix}/{bare}"


def get_model_pricing(model_name: str) -> tuple[float, float] | None:
    """Return ``(input, output)`` USD per 1M tokens, or None if unknown."""
    model = model_name.strip().lower()
    for key in (model, *_candidates(model)):
        entry = litellm.model_cost.get(key)
        if entry and entry.get("input_cost_per_token") is not None:
            return (
                round(entry["input_cost_per_token"] * 1_000_000, 6),
                round(entry.get("output_cost_per_token", 0.0) * 1_000_000, 6),
            )
    return None


@dataclass(frozen=True)
class ExtractionEstimate:
    """Pre-flight cost/time estimate; cost fields are None when the price is unknown."""

    model: str
    input_tokens: int
    output_tokens: float
    input_rate: float | None
    output_rate: float | None
    total_cost: float | None
    estimated_seconds: float

    @property
    def estimated_minutes(self) -> float:
        return self.estimated_seconds / 60

    def format_cost(self) -> str:
        if self.total_cost is None:
            return f"price unknown for {self.model}"
        return f"${self.total_cost:.2f}"


def estimate_extraction_cost(
    total_chars: float, model: str, *, workers: int = 1
) -> ExtractionEstimate:
    """Order-of-magnitude cost/time estimate for extracting ``total_chars`` of text."""
    input_tokens = int(total_chars // CHARS_PER_TOKEN)
    output_tokens = input_tokens * OUTPUT_TOKEN_RATIO
    rates = get_model_pricing(model)
    input_rate, output_rate = rates if rates else (None, None)
    total_cost = (
        (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        if rates
        else None
    )
    return ExtractionEstimate(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_rate=input_rate,
        output_rate=output_rate,
        total_cost=total_cost,
        estimated_seconds=input_tokens / (TOKENS_PER_SECOND * workers),
    )
