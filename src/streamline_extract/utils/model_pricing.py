"""AI Model Pricing Database.

Pricing data as of January 2026.
All prices are per 1M tokens in USD.
"""

from typing import Dict, Tuple
import logging

logger = logging.getLogger(__name__)


# Pricing: (input_per_1m, output_per_1m)
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # OpenAI Models
    "gpt-5": (1.25, 10.00),
    "gpt-5-pro": (15.00, 120.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (3.00, 6.00),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    # Google Gemini Models
    "gemini-3-pro": (2.00, 12.00),  # ≤200K tokens
    "gemini-3-flash": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 5.00),  # ≤200K tokens
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.0375, 0.15),
    "gemini-2.0-flash": (0.105, 0.42),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Anthropic Claude Models
    "claude-opus-4.5": (5.00, 25.00),
    "claude-opus-4.1": (15.00, 75.00),
    "claude-sonnet-4.5": (3.00, 15.00),  # ≤200K tokens
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-3.7": (3.00, 15.00),
    "claude-3.5-sonnet": (3.00, 15.00),  # Alias
    "claude-3-opus": (15.00, 75.00),  # Legacy
    "claude-haiku-4.5": (1.00, 5.00),
    "claude-haiku-3.5": (0.25, 1.25),
    "claude-3-haiku": (0.25, 1.25),
    # Meta Llama Models
    "llama-4-maverick": (0.28, 0.89),
    "llama-4-scout": (0.19, 0.62),
    "llama-3.3-70b": (0.04, 0.04),
    "llama-3.2-11b-vision": (0.049, 0.049),
    "llama-3.1-405b": (0.75, 2.25),  # Average
    "llama-3.1-70b": (0.20, 0.60),  # Average
    # Mistral AI Models
    "mistral-large-2": (3.00, 9.00),
    "mistral-medium-3": (0.40, 2.00),
    "mistral-small-3.2": (0.06, 0.18),
    "mistral-small-3.1": (0.03, 0.11),
    "mistral-nemo": (0.02, 0.30),
    # xAI Grok Models
    "grok-4.1": (3.00, 20.00),
    "grok-4.1-fast": (5.00, 25.00),
    "grok-4.1-mini": (0.30, 4.00),
    # DeepSeek Models
    "deepseek-v3.2": (0.28, 0.42),
    "deepseek-r1": (0.55, 2.19),
}


def get_model_pricing(model_name: str) -> Tuple[float, float]:
    """
    Get pricing for a model.

    Handles:
    - Exact matches: "gpt-4o-mini" -> (0.15, 0.60)
    - Partial matches: "gpt-4o" in "azure/gpt-4o" -> (5.00, 15.00)
    - Azure deployments: "compassop-gpt-4.1-mini" -> (0.40, 1.60)

    Args:
        model_name: Model name (e.g., "gpt-4o-mini", "claude-3.5-sonnet", "compassop-gpt-4.1-mini")

    Returns:
        (input_cost_per_1m, output_cost_per_1m) tuple
    """
    model_lower = model_name.lower()

    # Remove common prefixes
    model_lower = model_lower.replace("azure/", "")
    model_lower = model_lower.replace("gemini/", "")
    model_lower = model_lower.replace("anthropic/", "")

    # Handle Azure custom deployment names (e.g., "compassop-gpt-4.1-mini")
    # Extract the actual model name after the last hyphen group
    if "-gpt-" in model_lower:
        # "compassop-gpt-4.1-mini" -> "gpt-4.1-mini"
        parts = model_lower.split("-")
        gpt_index = next(i for i, p in enumerate(parts) if p == "gpt")
        model_lower = "-".join(parts[gpt_index:])
    elif "-claude-" in model_lower:
        parts = model_lower.split("-")
        claude_index = next(i for i, p in enumerate(parts) if p == "claude")
        model_lower = "-".join(parts[claude_index:])
    elif "-gemini-" in model_lower:
        parts = model_lower.split("-")
        gemini_index = next(i for i, p in enumerate(parts) if p == "gemini")
        model_lower = "-".join(parts[gemini_index:])

    # Try exact match first
    if model_lower in MODEL_PRICING:
        return MODEL_PRICING[model_lower]

    # Try partial match (longest match wins)
    matches = []
    for key in MODEL_PRICING:
        if key in model_lower or model_lower in key:
            matches.append((len(key), key))

    if matches:
        # Get longest match
        matches.sort(reverse=True)
        best_match = matches[0][1]
        logger.debug(
            f"Matched model '{model_name}' to pricing key '{best_match}'"
        )
        return MODEL_PRICING[best_match]

    # Default to gpt-4o-mini pricing if unknown
    logger.warning(
        f"Unknown model '{model_name}', using gpt-4o-mini pricing as fallback. "
        "Please add model to MODEL_PRICING in model_pricing.py"
    )
    return MODEL_PRICING["gpt-4o-mini"]


def calculate_cost(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """
    Calculate API cost for a model.

    Args:
        model_name: Model name
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens

    Returns:
        Cost in USD
    """
    input_cost_per_1m, output_cost_per_1m = get_model_pricing(model_name)

    input_cost = (prompt_tokens / 1_000_000) * input_cost_per_1m
    output_cost = (completion_tokens / 1_000_000) * output_cost_per_1m

    return input_cost + output_cost


class ModelPricing:
    """Simple wrapper for pricing lookup (for LLMClient compatibility)."""

    def get_cost(
        self, model_name: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Calculate cost for a model."""
        return calculate_cost(model_name, input_tokens, output_tokens)


# Global pricing instance
def get_pricing() -> ModelPricing:
    """Get the global pricing instance."""
    return ModelPricing()
