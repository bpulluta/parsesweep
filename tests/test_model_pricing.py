"""Tests for model pricing resolution.

Pricing values for models in the LiteLLM catalog are validated against
``litellm.model_cost`` (the installed catalog) so the test cannot simply
reproduce the local map and pass.  Models that are NOT in the LiteLLM
catalog (e.g. dot-notation aliases, internal/HALO models) are checked for
self-consistency with their dash-notation counterparts.
"""

import pytest
import litellm

from psweep.utils.model_pricing import get_model_pricing


def _catalog_price(litellm_key: str):
    """Return (input_per_1m, output_per_1m) from the installed LiteLLM catalog."""
    entry = litellm.model_cost.get(litellm_key)
    if entry is None:
        return None
    inp = round(entry.get("input_cost_per_token", 0) * 1_000_000, 6)
    out = round(entry.get("output_cost_per_token", 0) * 1_000_000, 6)
    return (inp, out)


# ── Catalog-verified prices ────────────────────────────────────────────────────
# Each tuple: (our model name, LiteLLM catalog key).
# The test fetches the expected value from the live catalog rather than
# hard-coding it, so a future pricing change in LiteLLM surfaces immediately.

CATALOG_MODELS = [
    # OpenAI
    ("gpt-5.6-luna",  "gpt-5.6-luna"),
    ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt-5.6-sol",   "gpt-5.6-sol"),
    ("gpt-5.4",       "gpt-5.4"),
    ("gpt-5.5",       "gpt-5.5"),
    # Anthropic — all newly-added entries must stay catalog-aligned
    ("claude-opus-4-7",  "claude-opus-4-7"),
    ("claude-opus-4-8",  "claude-opus-4-8"),
    ("claude-opus-5",    "claude-opus-5"),
    ("claude-haiku-4-5", "claude-haiku-4-5"),
    ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-sonnet-5",   "claude-sonnet-5"),
    # Gemini
    ("gemini-3.5-flash", "gemini-3.5-flash"),
]


@pytest.mark.parametrize("our_key,catalog_key", CATALOG_MODELS)
def test_price_matches_litellm_catalog(our_key, catalog_key):
    """Prices in MODEL_PRICING must match the installed LiteLLM catalog."""
    expected = _catalog_price(catalog_key)
    if expected is None:
        pytest.skip(f"Model '{catalog_key}' not found in installed LiteLLM catalog")
    assert get_model_pricing(our_key) == expected, (
        f"Price mismatch for '{our_key}': local={get_model_pricing(our_key)}, "
        f"LiteLLM catalog={expected}"
    )


# ── Alias consistency (dot-notation <-> dash-notation) ────────────────────────
# These models have no LiteLLM catalog entry; test that their dot alias
# resolves to the same price as the dash variant.

ALIAS_PAIRS = [
    ("claude-opus-4.7", "claude-opus-4-7"),
    ("claude-opus-4.8", "claude-opus-4-8"),
    ("claude-haiku-4.5", "claude-haiku-4-5"),
    ("claude-sonnet-4.5", "claude-sonnet-4-5"),
    ("claude-sonnet-4.6", "claude-sonnet-4-6"),
]


@pytest.mark.parametrize("dot_key,dash_key", ALIAS_PAIRS)
def test_dot_alias_matches_dash_variant(dot_key, dash_key):
    assert get_model_pricing(dot_key) == get_model_pricing(dash_key)


# ── Resolution logic ──────────────────────────────────────────────────────────

def test_azure_prefix_stripped():
    assert get_model_pricing("azure/gpt-4.1") == get_model_pricing("gpt-4.1")


def test_anthropic_prefix_stripped():
    assert get_model_pricing("anthropic/claude-3-haiku") == get_model_pricing("claude-3-haiku")


def test_gemini_prefix_stripped():
    assert get_model_pricing("gemini/gemini-1.5-pro") == get_model_pricing("gemini-1.5-pro")


def test_compassop_azure_deployment_resolved():
    # "compassop-gpt-4.1-mini" → strips "compassop-" prefix → "gpt-4.1-mini"
    assert get_model_pricing("compassop-gpt-4.1-mini") == get_model_pricing("gpt-4.1-mini")


def test_halo_models_are_free():
    for key in ["halo-devstral-123b", "halo-gemma-4", "halo-gpt-oss-120b",
                "halo-llama-4-scout", "halo-neotron-3-nano", "halo-neotron-3-super"]:
        assert get_model_pricing(key) == (0.00, 0.00), f"{key} should be free"


def test_unknown_model_returns_fallback_not_zero():
    result = get_model_pricing("totally-unknown-model-xyz-9999")
    inp, out = result
    # Fallback should resolve to a known model's price, not zero
    assert inp > 0 or out > 0
