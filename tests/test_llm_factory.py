"""Unit tests for the per-stage model resolver (``llm_factory``)."""

from __future__ import annotations

import pytest

from psweep.extraction import llm_factory


AZURE_CFG = {
    "provider": "azure",
    "model": "azure-default-deploy",
    "api_key": "azure-key",
    "azure_endpoint": "https://example.openai.azure.com",
    "azure_api_version": "2024-02-15-preview",
}

OPENAI_CFG = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key": "openai-key",
}


# ── resolve_model_name: precedence chain ────────────────────────────────────


def test_alias_lookup_wins_when_stage_value_is_an_alias():
    models = {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    assert (
        llm_factory.resolve_model_name(
            "accurate", models=models, llm_config=OPENAI_CFG
        )
        == "gpt-5"
    )


def test_literal_model_string_passes_through_when_not_an_alias():
    models = {"fast": "gpt-4o-mini"}
    assert (
        llm_factory.resolve_model_name(
            "claude-3.5-sonnet", models=models, llm_config=OPENAI_CFG
        )
        == "claude-3.5-sonnet"
    )


def test_default_model_alias_used_when_no_stage_value():
    models = {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    # default_model may itself be an alias; it resolves through the map.
    assert (
        llm_factory.resolve_model_name(
            None, models=models, llm_config=OPENAI_CFG, default_model="fast"
        )
        == "gpt-4o-mini"
    )


def test_default_model_literal_used_when_no_stage_value():
    assert (
        llm_factory.resolve_model_name(
            None, models={}, llm_config=OPENAI_CFG, default_model="gpt-4o"
        )
        == "gpt-4o"
    )


def test_falls_back_to_llm_config_model_when_nothing_specified():
    assert (
        llm_factory.resolve_model_name(None, models={}, llm_config=OPENAI_CFG)
        == "gpt-4o-mini"
    )


def test_final_floor_is_default_model():
    assert (
        llm_factory.resolve_model_name(None, models={}, llm_config={})
        == llm_factory.DEFAULT_MODEL
    )


# ── resolve_llm_kwargs: credential / provider threading ─────────────────────


def test_same_provider_alias_threads_configured_azure_creds():
    models = {"fast": "fast-deploy", "accurate": "accurate-deploy"}
    kw = llm_factory.resolve_llm_kwargs(
        "fast", models=models, llm_config=AZURE_CFG
    )
    assert kw["model"] == "fast-deploy"
    assert kw["provider"] == "azure"  # deployment name corrected to azure
    assert kw["api_key"] == "azure-key"
    assert kw["azure_endpoint"] == AZURE_CFG["azure_endpoint"]
    assert kw["azure_api_version"] == AZURE_CFG["azure_api_version"]


def test_openai_same_provider_threads_openai_key():
    kw = llm_factory.resolve_llm_kwargs(
        "gpt-4o", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "openai"
    assert kw["api_key"] == "openai-key"
    assert kw["azure_endpoint"] is None


def test_cross_provider_falls_back_to_ambient_env_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-ambient")
    kw = llm_factory.resolve_llm_kwargs(
        "claude-3.5-sonnet", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "anthropic"
    assert kw["api_key"] == "anthropic-ambient"


def test_cross_provider_without_key_yields_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    kw = llm_factory.resolve_llm_kwargs(
        "claude-3.5-sonnet", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "anthropic"
    assert kw["api_key"] is None


def test_processing_model_honored_under_azure_config():
    # Regression lock: previously commands.py discarded processing.model for
    # non-OpenAI providers. A literal model under an Azure config must now be
    # honored (routed through the factory).
    kw = llm_factory.resolve_llm_kwargs(
        "gpt-5-strong-deploy",
        models={},
        llm_config=AZURE_CFG,
    )
    assert kw["model"] == "gpt-5-strong-deploy"
    assert kw["provider"] == "azure"
    assert kw["api_key"] == "azure-key"


def test_empty_config_defaults(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    kw = llm_factory.resolve_llm_kwargs(None, models={}, llm_config={})
    assert kw["model"] == llm_factory.DEFAULT_MODEL
    assert kw["provider"] == "openai"


def test_cross_provider_mistral_uses_mistral_api_key(monkeypatch):
    # mistral is now a recognized cross-provider fallback (MISTRAL_API_KEY).
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-ambient")
    kw = llm_factory.resolve_llm_kwargs(
        "mistral-large-2", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "mistral"
    assert kw["api_key"] == "mistral-ambient"


# ── provider-set consistency (single source of truth) ───────────────────────


def test_provider_env_keys_are_a_subset_of_known_providers():
    assert set(llm_factory._PROVIDER_ENV_KEY).issubset(
        llm_factory.KNOWN_PROVIDERS
    )


def test_detect_provider_outputs_are_all_known_providers():
    # Every provider detect_provider can emit must be a recognized provider.
    sample_models = [
        "gpt-4o-mini",
        "azure/my-deploy",
        "claude-3.5-sonnet",
        "gemini-1.5-pro",
        "gemma-2",
        "llama-3.3-70b",
        "mistral-large-2",
        "codestral-latest",
        "o1",
        "o3",
        "some-unknown-model",  # falls through to the "openai" default
    ]
    for model in sample_models:
        assert llm_factory.detect_provider(model) in llm_factory.KNOWN_PROVIDERS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
