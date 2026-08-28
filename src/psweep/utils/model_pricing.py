"""AI Model Pricing Database.

Pricing data as of August 2026.
All prices are per 1M tokens in USD.
"""

from typing import Dict, Tuple
import logging

from psweep.extraction.llm_factory import DEFAULT_MODEL

logger = logging.getLogger(__name__)


# Pricing: (input_per_1m, output_per_1m)
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # OpenAI Models
    "gpt-5": (1.25, 10.00),
    "gpt-5-pro": (15.00, 120.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5.1-batch": (0.69, 5.50),
    "gpt-5.1-codex-mini": (0.25, 2.00),
    "gpt-5.3-codex": (1.75, 14.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6-terra": (2.00, 12.00),
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
    "gemini-3-pro-image-preview": (2.00, 12.00),
    "gemini-3-flash": (0.10, 0.40),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-2.5-pro": (1.25, 5.00),  # ≤200K tokens
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.0375, 0.15),
    "gemini-2.0-flash": (0.105, 0.42),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Anthropic Claude Models
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4.5": (1.00, 5.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4.6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4.7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4.8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4.5": (5.00, 25.00),
    "claude-opus-4.1": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4.5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4.6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-5.0": (2.00, 10.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-3.7": (3.00, 15.00),
    "claude-3.5-sonnet": (3.00, 15.00),  # Alias
    "claude-3-opus": (15.00, 75.00),  # Legacy
    "claude-haiku-3.5": (0.25, 1.25),
    "claude-3-haiku": (0.25, 1.25),
    # Custom HALO models (free internal routing)
    "halo devstral 123b": (0.00, 0.00),
    "halo-devstral-123b": (0.00, 0.00),
    "halo gemma 4": (0.00, 0.00),
    "halo-gemma-4": (0.00, 0.00),
    "halo gpt oss 120b": (0.00, 0.00),
    "halo-gpt-oss-120b": (0.00, 0.00),
    "halo llama 4 scout": (0.00, 0.00),
    "halo-llama-4-scout": (0.00, 0.00),
    "halo neotron 3 nano": (0.00, 0.00),
    "halo-neotron-3-nano": (0.00, 0.00),
    "halo neotron 3 super": (0.00, 0.00),
    "halo-neotron-3-super": (0.00, 0.00),
    # Embeddings
    "text-embedding-3-large": (0.13, 0.00),
    "text-embedding-3-small": (0.02, 0.00),
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

_UNKNOWN_MODEL_WARNED: set[str] = set()

# Provider-family markers used to peel a custom deployment prefix off a model
# name (e.g. "compassop-gpt-4.1-mini" -> "gpt-4.1-mini").
_DEPLOYMENT_FAMILY_MARKERS = ("gpt", "claude", "gemini")


def _strip_deployment_prefix(model_lower: str) -> str:
    """Drop a custom deployment prefix before a known provider-family marker."""
    for marker in _DEPLOYMENT_FAMILY_MARKERS:
        if f"-{marker}-" in model_lower:
            parts = model_lower.split("-")
            idx = next(i for i, p in enumerate(parts) if p == marker)
            return "-".join(parts[idx:])
    return model_lower


def _boundary_partial_match(model_lower: str) -> str | None:
    """Longest known pricing key that appears in ``model_lower`` as a token.

    Only the "known key appears in the model name" direction is used: it
    resolves deployment/version suffixes (e.g. "gpt-4o-2024-05-13" -> "gpt-4o").
    The reverse direction (query is a substring of a key) is intentionally not
    allowed — it would map a bare family name like "gpt-4" to an arbitrary
    longer, differently-priced variant (e.g. "gpt-4o-mini").
    """
    matches = []
    for key in MODEL_PRICING:
        idx = model_lower.find(key)
        if idx == -1:
            continue
        before_ok = idx == 0 or not model_lower[idx - 1].isalnum()
        end = idx + len(key)
        after_ok = end == len(model_lower) or not model_lower[end].isalnum()
        if before_ok and after_ok:
            matches.append((len(key), key))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def get_model_pricing(model_name: str) -> Tuple[float, float]:
    """
    Get pricing for a model.

    Handles:

    - Exact matches: "gpt-4o-mini" -> (0.15, 0.60)
    - Partial matches: "gpt-4o" in "azure/gpt-4o" -> (5.00, 15.00)
    - Azure deployments: "compassop-gpt-4.1-mini" -> (0.40, 1.60)

    Parameters
    ----------
    model_name : str
        Model name (e.g., "gpt-4o-mini", "claude-3.5-sonnet", "compassop-gpt-4.1-mini")

    Returns
    -------
    Tuple[float, float]
        (input_cost_per_1m, output_cost_per_1m) tuple
    """
    model_lower = model_name.lower()

    # Remove common prefixes
    model_lower = model_lower.replace("azure/", "")
    model_lower = model_lower.replace("gemini/", "")
    model_lower = model_lower.replace("anthropic/", "")

    # Exact match first — before any deployment-prefix stripping — so a whole
    # model name that itself contains a family marker (e.g. the internal
    # "halo-gpt-oss-120b") is matched intact rather than mangled.
    if model_lower in MODEL_PRICING:
        return MODEL_PRICING[model_lower]

    # Peel a custom deployment prefix (e.g. "compassop-gpt-4.1-mini") and retry.
    model_lower = _strip_deployment_prefix(model_lower)
    if model_lower in MODEL_PRICING:
        return MODEL_PRICING[model_lower]

    best_match = _boundary_partial_match(model_lower)
    if best_match is not None:
        logger.debug(
            f"Matched model '{model_name}' to pricing key '{best_match}'"
        )
        return MODEL_PRICING[best_match]

    # Default to DEFAULT_MODEL pricing if unknown
    if model_lower not in _UNKNOWN_MODEL_WARNED:
        _UNKNOWN_MODEL_WARNED.add(model_lower)
        logger.warning(
            f"Unknown model '{model_name}'; estimating cost using {DEFAULT_MODEL} pricing fallback."
        )
    return MODEL_PRICING[DEFAULT_MODEL]


def calculate_cost(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """
    Calculate API cost for a model.

    Parameters
    ----------
    model_name : str
        Model name
    prompt_tokens : int
        Number of input tokens
    completion_tokens : int
        Number of output tokens

    Returns
    -------
    float
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
