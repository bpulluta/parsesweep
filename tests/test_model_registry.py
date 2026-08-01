"""Unit tests for the unified model registry (``config.model_registry``)."""

from __future__ import annotations

import pytest

from psweep.config.model_registry import (
    KNOWN_PROVIDERS,
    MIN_MULTI_MODELS,
    ModelDefinition,
    ModelRegistry,
    ModelRegistryError,
)


OPENAI_CFG = {
    "provider": "openai",
    "model": "env-default-model",
    "api_key": "openai-key",
}


def _fake_resolve_kwargs(stage_value, *, models=None, llm_config=None, **_):
    """Record the model passed through and echo a deterministic kwargs dict."""
    return {
        "model": stage_value,
        "api_key": (llm_config or {}).get("api_key"),
        "provider": (llm_config or {}).get("provider"),
        "azure_endpoint": (llm_config or {}).get("azure_endpoint"),
        "azure_api_version": (llm_config or {}).get("azure_api_version"),
    }


# ── from_config: building from the various shapes ──────────────────────────


def test_from_config_string_form_builds_tier_definitions():
    cfg = {"models": {"primary": "gpt-4.1", "secondary": "gpt-4.1-mini"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)

    primary = registry.get_model("primary")
    assert isinstance(primary, ModelDefinition)
    assert primary.tier == "primary"
    assert primary.model == "gpt-4.1"
    assert primary.provider is None
    assert primary.context_window is None
    assert dict(primary.params) == {}


def test_from_config_long_form_dict_carries_provider_window_and_params():
    cfg = {
        "models": {
            "primary": {
                "model": "azure-deploy",
                "provider": "azure",
                "context_window": 200000,
                "params": {"temperature": 0.0},
            }
        }
    }
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)

    definition = registry.get_model("primary")
    assert definition.model == "azure-deploy"
    assert definition.provider == "azure"
    assert definition.context_window == 200000
    assert dict(definition.params) == {"temperature": 0.0}


def test_from_config_folds_model_context_windows_into_tier():
    cfg = {
        "models": {"primary": "gpt-4.1"},
        "model_context_windows": {"gpt-4.1": 128000},
    }
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)

    assert registry.get_model("primary").context_window == 128000


def test_from_config_long_form_context_window_wins_over_side_map():
    cfg = {
        "models": {
            "primary": {"model": "gpt-4.1", "context_window": 999}
        },
        "model_context_windows": {"gpt-4.1": 128000},
    }
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)

    assert registry.get_model("primary").context_window == 999


def test_from_config_empty_models_block_is_backward_compatible():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)

    # No tiers, but any reference still resolves as a literal.
    definition = registry.get_model("some-literal-model")
    assert definition.model == "some-literal-model"


def test_from_config_none_config_yields_usable_registry():
    registry = ModelRegistry.from_config(None, OPENAI_CFG)
    assert registry.get_model("literal").model == "literal"


def test_from_config_rejects_non_mapping_models_block():
    with pytest.raises(ModelRegistryError, match="'models' must be a mapping"):
        ModelRegistry.from_config({"models": ["gpt-4.1"]}, OPENAI_CFG)


def test_from_config_rejects_long_form_without_model():
    cfg = {"models": {"primary": {"provider": "azure"}}}
    with pytest.raises(ModelRegistryError, match="non-empty 'model' string"):
        ModelRegistry.from_config(cfg, OPENAI_CFG)


def test_from_config_rejects_invalid_spec_type():
    cfg = {"models": {"primary": 123}}
    with pytest.raises(ModelRegistryError, match="model string or a mapping"):
        ModelRegistry.from_config(cfg, OPENAI_CFG)


# ── get_model: alias hit vs literal fallback vs floor ──────────────────────


def test_get_model_alias_hit_returns_mapped_definition():
    registry = ModelRegistry.from_config(
        {"models": {"accurate": "gpt-5"}}, OPENAI_CFG
    )
    assert registry.get_model("accurate").model == "gpt-5"


def test_get_model_literal_fallback_for_unknown_reference():
    registry = ModelRegistry.from_config(
        {"models": {"accurate": "gpt-5"}}, OPENAI_CFG
    )
    definition = registry.get_model("claude-3.5-sonnet")
    assert definition.tier == "claude-3.5-sonnet"
    assert definition.model == "claude-3.5-sonnet"


def test_get_model_none_returns_env_floor_model():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    assert registry.get_model(None).model == "env-default-model"


def test_get_model_empty_string_returns_env_floor_model():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    assert registry.get_model("   ").model == "env-default-model"


def test_get_model_floor_uses_default_when_no_env_model():
    registry = ModelRegistry.from_config({}, {"provider": "openai"})
    # Falls back to llm_factory.DEFAULT_MODEL.
    assert registry.get_model(None).model == "gpt-4o-mini"


def test_get_model_non_string_reference_raises():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="must be a string or None"):
        registry.get_model(123)  # type: ignore[arg-type]


def test_get_model_literal_folds_context_window_from_side_map():
    cfg = {"model_context_windows": {"gpt-4.1": 128000}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    assert registry.get_model("gpt-4.1").context_window == 128000


# ── get_models: dedup + order preservation ─────────────────────────────────


def test_get_models_preserves_order_and_dedups_by_model():
    cfg = {"models": {"a": "gpt-5", "b": "gpt-4.1", "c": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)

    resolved = registry.get_models(["a", "b", "c", "gpt-4.1"])
    models = [d.model for d in resolved]
    assert models == ["gpt-5", "gpt-4.1"]


def test_get_models_empty_input_returns_empty_list():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    assert registry.get_models(None) == []
    assert registry.get_models([]) == []


# ── resolve_for_phase: explicit -> defaults -> floor ───────────────────────


def test_resolve_for_phase_explicit_reference_wins():
    cfg = {"models": {"primary": "gpt-5"}, "model_defaults": {"extract": "primary"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    # Explicit literal overrides the phase default.
    assert registry.resolve_for_phase("extract", "gpt-4.1").model == "gpt-4.1"


def test_resolve_for_phase_uses_default_when_no_explicit():
    cfg = {"models": {"primary": "gpt-5"}, "model_defaults": {"extract": "primary"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    assert registry.resolve_for_phase("extract").model == "gpt-5"


def test_resolve_for_phase_falls_to_floor_without_default():
    cfg = {"models": {"primary": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    assert registry.resolve_for_phase("extract").model == "env-default-model"


def test_resolve_for_phase_blank_explicit_ignored():
    cfg = {"model_defaults": {"extract": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    assert registry.resolve_for_phase("extract", "  ").model == "gpt-5"


# ── to_llm_kwargs: single credential path via resolve_llm_kwargs ────────────


def test_to_llm_kwargs_delegates_to_resolve_llm_kwargs():
    cfg = {"models": {"primary": "gpt-5"}}
    registry = ModelRegistry.from_config(
        cfg, OPENAI_CFG, resolve_kwargs=_fake_resolve_kwargs
    )
    kwargs = registry.to_llm_kwargs("primary")
    assert kwargs["model"] == "gpt-5"
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "openai-key"


def test_to_llm_kwargs_literal_reference_passes_through():
    registry = ModelRegistry.from_config(
        {}, OPENAI_CFG, resolve_kwargs=_fake_resolve_kwargs
    )
    assert registry.to_llm_kwargs("claude-3.5-sonnet")["model"] == "claude-3.5-sonnet"


def test_to_llm_kwargs_none_reference_uses_floor_model():
    registry = ModelRegistry.from_config(
        {}, OPENAI_CFG, resolve_kwargs=_fake_resolve_kwargs
    )
    assert registry.to_llm_kwargs(None)["model"] == "env-default-model"


def test_to_llm_kwargs_default_bridge_calls_real_factory(monkeypatch):
    # Without an injected callable, it lazily imports the real factory.
    import psweep.extraction.llm_factory as factory

    captured = {}

    def _spy(stage_value, *, models=None, llm_config=None, **_):
        captured["stage_value"] = stage_value
        captured["models"] = models
        return {"model": stage_value, "provider": (llm_config or {}).get("provider")}

    monkeypatch.setattr(factory, "resolve_llm_kwargs", _spy)
    registry = ModelRegistry.from_config({"models": {"primary": "gpt-5"}}, OPENAI_CFG)
    result = registry.to_llm_kwargs("primary")
    assert result["model"] == "gpt-5"
    assert captured["stage_value"] == "gpt-5"
    # Concrete model must pass as a literal (empty alias map).
    assert captured["models"] == {}


# ── validate: structure ─────────────────────────────────────────────────────


def test_validate_accepts_well_formed_registry():
    cfg = {
        "models": {
            "primary": {"model": "azure-deploy", "provider": "azure"},
            "secondary": "gpt-4.1-mini",
        },
        "model_context_windows": {"azure-deploy": 200000},
    }
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    registry.validate()  # should not raise


def test_validate_rejects_unknown_declared_provider():
    cfg = {"models": {"primary": {"model": "x", "provider": "notaprovider"}}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="unknown provider"):
        registry.validate()


def test_validate_rejects_non_positive_context_window():
    cfg = {"models": {"primary": {"model": "x", "context_window": 0}}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="context_window must be a positive"):
        registry.validate()


def test_validate_rejects_boolean_context_window():
    registry = ModelRegistry(
        tiers={
            "primary": ModelDefinition(
                tier="primary", model="x", context_window=True  # type: ignore[arg-type]
            )
        }
    )
    with pytest.raises(ModelRegistryError, match="context_window must be a positive"):
        registry.validate()


def test_validate_known_providers_are_accepted():
    for provider in sorted(KNOWN_PROVIDERS):
        registry = ModelRegistry(
            tiers={
                "t": ModelDefinition(tier="t", model="m", provider=provider)
            }
        )
        registry.validate()


# ── validate: defaults ──────────────────────────────────────────────────────


def test_validate_rejects_blank_default_tier():
    registry = ModelRegistry(
        tiers={"primary": ModelDefinition(tier="primary", model="x")},
        defaults={"extract": ""},
    )
    with pytest.raises(ModelRegistryError, match="must be a non-empty tier"):
        registry.validate()


# ── validate: single references ─────────────────────────────────────────────


def test_validate_single_reference_accepts_alias_and_literal():
    cfg = {"models": {"primary": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    # Alias hit, literal, and None (unset) are all fine.
    registry.validate(references=["primary", "claude-3.5-sonnet", None])


def test_validate_rejects_empty_string_reference():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="non-empty string"):
        registry.validate(references=[""])


def test_validate_rejects_non_string_reference():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="non-empty string"):
        registry.validate(references=[42])  # type: ignore[list-item]


# ── validate: multi-model arity ─────────────────────────────────────────────


def test_validate_multi_reference_requires_two_distinct_models():
    cfg = {"models": {"a": "gpt-5", "b": "gpt-4.1"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    registry.validate(multi_references=[("qaqc.models", ["a", "b"])])


def test_validate_multi_reference_rejects_single_model():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(
        ModelRegistryError,
        match=f"at least {MIN_MULTI_MODELS} distinct",
    ):
        registry.validate(multi_references=[("qaqc.models", ["gpt-5"])])


def test_validate_multi_reference_rejects_duplicate_resolved_models():
    cfg = {"models": {"a": "gpt-5", "b": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="distinct models"):
        registry.validate(multi_references=[("qaqc.models", ["a", "b"])])


def test_validate_multi_reference_rejects_none_list():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="at least"):
        registry.validate(multi_references=[("qaqc.models", None)])


def test_validate_multi_reference_rejects_non_list():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="must be a list"):
        registry.validate(multi_references=[("qaqc.models", "gpt-5")])


def test_validate_multi_reference_rejects_blank_entry():
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="non-empty strings"):
        registry.validate(multi_references=[("qaqc.models", ["gpt-5", " "])])


# ── immutability ────────────────────────────────────────────────────────────


def test_model_definition_is_frozen():
    definition = ModelDefinition(tier="t", model="m")
    with pytest.raises(Exception):
        definition.model = "other"  # type: ignore[misc]
