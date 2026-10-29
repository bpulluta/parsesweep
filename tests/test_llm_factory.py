"""Unit tests for the per-stage model resolver (``llm_factory``)."""

from __future__ import annotations

from types import SimpleNamespace

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
    """Alias lookup wins when stage value is an alias."""
    models = {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    assert (
        llm_factory.resolve_model_name(
            "accurate", models=models, llm_config=OPENAI_CFG
        )
        == "gpt-5"
    )


def test_literal_model_string_passes_through_when_not_an_alias():
    """Literal model string passes through when not an alias."""
    models = {"fast": "gpt-4o-mini"}
    assert (
        llm_factory.resolve_model_name(
            "claude-3.5-sonnet", models=models, llm_config=OPENAI_CFG
        )
        == "claude-3.5-sonnet"
    )


def test_default_model_alias_used_when_no_stage_value():
    """Default model alias used when no stage value."""
    models = {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    # default_model may itself be an alias; it resolves through the map.
    assert (
        llm_factory.resolve_model_name(
            None, models=models, llm_config=OPENAI_CFG, default_model="fast"
        )
        == "gpt-4o-mini"
    )


def test_default_model_literal_used_when_no_stage_value():
    """Default model literal used when no stage value."""
    assert (
        llm_factory.resolve_model_name(
            None, models={}, llm_config=OPENAI_CFG, default_model="gpt-4o"
        )
        == "gpt-4o"
    )


def test_falls_back_to_llm_config_model_when_nothing_specified():
    """Falls back to llm config model when nothing specified."""
    assert (
        llm_factory.resolve_model_name(None, models={}, llm_config=OPENAI_CFG)
        == "gpt-4o-mini"
    )


def test_final_floor_is_default_model():
    """Final floor is default model."""
    assert (
        llm_factory.resolve_model_name(None, models={}, llm_config={})
        == llm_factory.DEFAULT_MODEL
    )


# ── resolve_llm_kwargs: credential / provider threading ─────────────────────


def test_same_provider_alias_threads_configured_azure_creds():
    """Same provider alias threads configured azure creds."""
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
    """Openai same provider threads openai key."""
    kw = llm_factory.resolve_llm_kwargs(
        "gpt-4o", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "openai"
    assert kw["api_key"] == "openai-key"
    assert kw["azure_endpoint"] is None


def test_cross_provider_falls_back_to_ambient_env_key(monkeypatch):
    """Cross provider falls back to ambient env key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-ambient")
    kw = llm_factory.resolve_llm_kwargs(
        "claude-3.5-sonnet", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "anthropic"
    assert kw["api_key"] == "anthropic-ambient"


def test_cross_provider_without_key_yields_none(monkeypatch):
    """Cross provider without key yields none."""
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
    """Processing model honored under azure config."""
    kw = llm_factory.resolve_llm_kwargs(
        "gpt-5-strong-deploy",
        models={},
        llm_config=AZURE_CFG,
    )
    assert kw["model"] == "gpt-5-strong-deploy"
    assert kw["provider"] == "azure"
    assert kw["api_key"] == "azure-key"


def test_empty_config_defaults(monkeypatch):
    """Empty config defaults."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    kw = llm_factory.resolve_llm_kwargs(None, models={}, llm_config={})
    assert kw["model"] == llm_factory.DEFAULT_MODEL
    assert kw["provider"] == "openai"


def test_cross_provider_mistral_uses_mistral_api_key(monkeypatch):
    # mistral is now a recognized cross-provider fallback (MISTRAL_API_KEY).
    """Cross provider mistral uses mistral api key."""
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-ambient")
    kw = llm_factory.resolve_llm_kwargs(
        "mistral-large-2", models={}, llm_config=OPENAI_CFG
    )
    assert kw["provider"] == "mistral"
    assert kw["api_key"] == "mistral-ambient"


# ── provider-set consistency (single source of truth) ───────────────────────


def test_provider_env_keys_are_a_subset_of_known_providers():
    """Provider env keys are a subset of known providers."""
    assert set(llm_factory._PROVIDER_ENV_KEY).issubset(
        llm_factory.KNOWN_PROVIDERS
    )


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("gpt-4o-mini", "openai"),
        ("azure/my-deploy", "azure"),
        ("claude-3.5-sonnet", "anthropic"),
        ("gemini-1.5-pro", "gemini"),
        ("gemma-2", "gemini"),
        ("llama-3.3-70b", "meta"),
        ("mistral-large-2", "mistral"),
        ("codestral-latest", "mistral"),
        ("o1", "openai"),
        ("o3", "openai"),
        ("some-unknown-model", "openai"),
        ("AZURE/CLAUDE-gpt-gemini", "azure"),
        ("claude-gemini-gpt", "anthropic"),
        ("gemini-gpt", "gemini"),
        ("gpt-llama-mistral", "openai"),
        ("llama-mistral", "meta"),
    ],
)
def test_detect_provider_preserves_known_results_and_marker_priority(
    model, provider
):
    """Preserve every heuristic provider result and ordered marker priority."""
    assert llm_factory.detect_provider(model) == provider
    assert provider in llm_factory.KNOWN_PROVIDERS


@pytest.mark.parametrize("provider", [None, "auto", "AUTO"])
def test_automatic_provider_preserves_azure_deployment_resolution(provider):
    """Automatic provider preserves azure deployment resolution."""
    kwargs = llm_factory.resolve_llm_kwargs(
        "fast",
        models={"fast": "custom-deployment"},
        llm_config=AZURE_CFG,
        provider=provider,
    )
    assert kwargs["model"] == "custom-deployment"
    assert kwargs["provider"] == "azure"
    assert kwargs["api_key"] == "azure-key"


@pytest.mark.parametrize("provider", sorted(llm_factory.KNOWN_PROVIDERS))
def test_explicit_provider_wins_over_model_heuristic(provider):
    """Explicit provider wins over model heuristic."""
    cfg = {"provider": provider, "api_key": "configured-key"}
    kwargs = llm_factory.resolve_llm_kwargs(
        "gpt-named-deployment",
        llm_config=cfg,
        provider=provider,
    )
    assert kwargs["provider"] == provider
    assert kwargs["api_key"] == "configured-key"


def test_explicit_openai_overrides_configured_azure(monkeypatch):
    """Explicit openai overrides configured azure."""
    monkeypatch.setenv("OPENAI_API_KEY", "openai-ambient")
    kwargs = llm_factory.resolve_llm_kwargs(
        "gpt-4o",
        llm_config=AZURE_CFG,
        provider="openai",
    )
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "openai-ambient"
    assert kwargs["azure_endpoint"] is None
    assert kwargs["azure_api_version"] is None


def test_explicit_azure_uses_ambient_azure_endpoint_and_version(monkeypatch):
    """Explicit azure uses ambient azure endpoint and version."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "ambient-azure")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://ambient.azure.example"
    )
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "ambient-version")
    cfg = {
        **OPENAI_CFG,
        "azure_endpoint": "foreign-endpoint",
        "azure_api_version": "foreign-version",
    }
    kwargs = llm_factory.resolve_llm_kwargs(
        "claude-named-deployment",
        llm_config=cfg,
        provider="azure",
    )
    assert kwargs["provider"] == "azure"
    assert kwargs["api_key"] == "ambient-azure"
    assert kwargs["azure_endpoint"] == "https://ambient.azure.example"
    assert kwargs["azure_api_version"] == "ambient-version"


@pytest.mark.parametrize("provider", sorted(llm_factory.KNOWN_PROVIDERS))
def test_explicit_cross_provider_never_borrows_foreign_key(
    monkeypatch, provider
):
    """Explicit cross provider never borrows foreign key."""
    for env_var in (*llm_factory._PROVIDER_ENV_KEY.values(), "GOOGLE_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)
    cfg_provider = "azure" if provider == "openai" else "openai"
    kwargs = llm_factory.resolve_llm_kwargs(
        "custom-deployment",
        llm_config={"provider": cfg_provider, "api_key": "foreign"},
        provider=provider,
    )
    assert kwargs["provider"] == provider
    assert kwargs["api_key"] is None


def test_explicit_gemini_uses_google_key_when_gemini_key_missing(monkeypatch):
    """Explicit gemini uses google key when gemini key missing."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-ambient")
    kwargs = llm_factory.resolve_llm_kwargs(
        "custom-deployment",
        llm_config=OPENAI_CFG,
        provider="gemini",
    )
    assert kwargs["api_key"] == "google-ambient"


@pytest.mark.parametrize("provider", ["not-a-provider", "", 123])
def test_explicit_unknown_provider_is_rejected(provider):
    """Explicit unknown provider is rejected."""
    with pytest.raises(ValueError, match=r"provider.*must be"):
        llm_factory.resolve_llm_kwargs(
            "gpt-4o", llm_config=OPENAI_CFG, provider=provider
        )


def test_explicit_provider_is_case_insensitive():
    """Explicit provider is case insensitive."""
    kwargs = llm_factory.resolve_llm_kwargs(
        "custom-deployment",
        llm_config=AZURE_CFG,
        provider="AZURE",
    )
    assert kwargs["provider"] == "azure"
    assert kwargs["api_key"] == "azure-key"


@pytest.mark.parametrize("provider", [None, "auto", "azure", "anthropic"])
def test_proxy_endpoint_keeps_proxy_credentials_with_provider_override(
    monkeypatch, provider
):
    """Proxy endpoint keeps proxy credentials with provider override."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-proxy-key")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-proxy-key")
    cfg = {
        **OPENAI_CFG,
        "api_key": "proxy-key",
        "base_url": "https://proxy.example/v1",
    }
    kwargs = llm_factory.resolve_llm_kwargs(
        "anthropic/claude-deployment",
        llm_config=cfg,
        provider=provider,
    )
    assert kwargs["model"] == "anthropic/claude-deployment"
    assert kwargs["api_key"] == "proxy-key"
    assert kwargs["base_url"] == "https://proxy.example/v1"
    assert kwargs["provider"] == (
        provider if provider not in {None, "auto"} else "openai"
    )
    assert not kwargs.get("azure_endpoint")
    assert not kwargs.get("azure_api_version")


@pytest.mark.parametrize("provider", [None, "auto", "azure"])
def test_omitted_model_uses_environment_config(monkeypatch, provider):
    """Omitted model uses environment config."""
    monkeypatch.setattr(
        "psweep.utils.config.get_config",
        lambda: SimpleNamespace(llm_config=AZURE_CFG),
    )
    kwargs = llm_factory.resolve_llm_kwargs(None, provider=provider)
    assert kwargs["model"] == "azure-default-deploy"
    assert kwargs["provider"] == "azure"
    assert kwargs["api_key"] == "azure-key"


def test_ambient_azure_without_endpoint_does_not_borrow_configured_values(
    monkeypatch,
):
    """Ambient azure without endpoint does not borrow configured values."""
    for env_var in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
    ):
        monkeypatch.delenv(env_var, raising=False)
    cfg = {
        **OPENAI_CFG,
        "azure_endpoint": "foreign",
        "azure_api_version": "foreign",
    }
    kwargs = llm_factory.resolve_llm_kwargs(
        "deployment", llm_config=cfg, provider="azure"
    )
    assert kwargs["api_key"] is None
    assert kwargs["azure_endpoint"] is None
    assert kwargs["azure_api_version"] is None
