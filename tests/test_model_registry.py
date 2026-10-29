"""Test string aliases, model resolution, and strict config contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import yaml

from psweep.config.model_registry import (
    MIN_MULTI_MODELS,
    ModelDefinition,
    ModelRegistry,
    ModelRegistryError,
)
from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.extraction import llm_factory
from psweep.extraction.llm_client import LLMClient

OPENAI_CFG = {
    "provider": "openai",
    "model": "env-default-model",
    "api_key": "openai-key",
}


def test_from_config_string_form_builds_tier_definitions():
    """From config string form builds tier definitions."""
    registry = ModelRegistry.from_config(
        {"models": {"primary": "gpt-4.1", "secondary": "gpt-4.1-mini"}},
        OPENAI_CFG,
    )
    assert registry.get_model("primary") == ModelDefinition(
        tier="primary", model="gpt-4.1"
    )
    assert registry.get_model("secondary").model == "gpt-4.1-mini"


@pytest.mark.parametrize(
    "spec",
    [
        {"model": "deployment", "provider": "azure"},
        {"model": "gpt-4.1", "context_window": 200000},
        {"model": "gpt-4.1", "params": {"temperature": 0.0}},
        {"model": "gpt-4.1", "context_window": 999},
        {"provider": "azure"},
        {},
    ],
)
def test_from_config_rejects_long_form_models_with_actionable_message(spec):
    """From config rejects long form models with actionable message."""
    with pytest.raises(ModelRegistryError, match="non-empty model string"):
        ModelRegistry.from_config({"models": {"primary": spec}}, OPENAI_CFG)


@pytest.mark.parametrize("defaults", [{"extract": "primary"}, {}, None, []])
def test_from_config_rejects_retired_phase_defaults(defaults):
    """From config rejects retired phase defaults."""
    with pytest.raises(
        ModelRegistryError, match=r"model_defaults.*not supported"
    ):
        ModelRegistry.from_config({"model_defaults": defaults}, OPENAI_CFG)


@pytest.mark.parametrize("config", [{}, {"models": {}}, None])
def test_empty_config_resolves_literal_models(config):
    """Empty config resolves literal models."""
    registry = ModelRegistry.from_config(config, OPENAI_CFG)
    assert registry.get_model("literal").model == "literal"


@pytest.mark.parametrize("block", [["gpt-4.1"], [], "", False, 123])
def test_from_config_rejects_non_mapping_models_block(block):
    """From config rejects non mapping models block."""
    with pytest.raises(ModelRegistryError, match="'models' must be a mapping"):
        ModelRegistry.from_config({"models": block}, OPENAI_CFG)


@pytest.mark.parametrize("spec", [123, True, None, "", "   "])
def test_from_config_rejects_invalid_model_strings(spec):
    """From config rejects invalid model strings."""
    with pytest.raises(ModelRegistryError, match="non-empty model string"):
        ModelRegistry.from_config({"models": {"primary": spec}}, OPENAI_CFG)


@pytest.mark.parametrize("name", [123, "", "   "])
def test_from_config_rejects_invalid_alias_names(name):
    """From config rejects invalid alias names."""
    with pytest.raises(ModelRegistryError, match="non-empty strings"):
        ModelRegistry.from_config({"models": {name: "gpt-4.1"}}, OPENAI_CFG)


def test_get_model_literal_fallback_for_unknown_reference():
    """Get model literal fallback for unknown reference."""
    registry = ModelRegistry.from_config(
        {"models": {"accurate": "gpt-5"}}, OPENAI_CFG
    )
    assert registry.get_model("accurate").model == "gpt-5"
    assert registry.get_model("claude-3.5-sonnet") == ModelDefinition(
        tier="claude-3.5-sonnet", model="claude-3.5-sonnet"
    )


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_get_model_omitted_reference_returns_configured_model(reference):
    """Get model omitted reference returns configured model."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    assert registry.get_model(reference).model == "env-default-model"


def test_get_model_floor_uses_factory_default_when_explicit_config_is_empty():
    """Get model floor uses factory default when explicit config is empty."""
    registry = ModelRegistry.from_config({}, {})
    assert registry.get_model(None).model == llm_factory.DEFAULT_MODEL


def test_get_model_omitted_config_uses_global_environment_model(monkeypatch):
    """Get model omitted config uses global environment model."""
    monkeypatch.setattr(
        "psweep.utils.config.get_config",
        lambda: SimpleNamespace(llm_config=OPENAI_CFG),
    )
    registry = ModelRegistry.from_config({})
    assert registry.get_model(None).model == "env-default-model"
    assert registry.to_llm_kwargs(None)["api_key"] == "openai-key"


def test_get_model_non_string_reference_raises():
    """Get model non string reference raises."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="must be a string or None"):
        registry.get_model(123)


def test_get_models_preserves_order_and_dedups_by_concrete_model():
    """Get models preserves order and dedups by concrete model."""
    cfg = {"models": {"a": "gpt-5", "b": "gpt-4.1", "c": "gpt-5"}}
    registry = ModelRegistry.from_config(cfg, OPENAI_CFG)
    resolved = registry.get_models(["a", "b", "c", "gpt-4.1"])
    assert [(d.tier, d.model) for d in resolved] == [
        ("a", "gpt-5"),
        ("b", "gpt-4.1"),
    ]
    assert registry.get_models(None) == []
    assert registry.get_models([]) == []


def test_to_llm_kwargs_delegates_once_with_concrete_model(monkeypatch):
    """To llm kwargs delegates once with concrete model."""
    captured = {}

    def spy(stage_value, **kwargs):
        captured.update(stage_value=stage_value, **kwargs)
        return {"model": stage_value, "provider": "openai", "api_key": "key"}

    monkeypatch.setattr(llm_factory, "resolve_llm_kwargs", spy)
    # The concrete value is also another alias. It must not be resolved twice.
    registry = ModelRegistry.from_config(
        {"models": {"primary": "secondary", "secondary": "gpt-5"}}, OPENAI_CFG
    )
    assert registry.to_llm_kwargs("primary")["model"] == "secondary"
    assert captured == {
        "stage_value": "secondary",
        "models": {},
        "llm_config": OPENAI_CFG,
    }


@pytest.mark.parametrize("reference", ["gpt-4o", None])
def test_to_llm_kwargs_same_provider_uses_configured_credentials(reference):
    """To llm kwargs same provider uses configured credentials."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    kwargs = registry.to_llm_kwargs(reference)
    assert kwargs["model"] == (reference or "env-default-model")
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "openai-key"


def test_to_llm_kwargs_cross_provider_uses_only_target_credentials(
    monkeypatch,
):
    """To llm kwargs cross provider uses only target credentials."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    registry = ModelRegistry.from_config(
        {"models": {"review": "claude-3.5-sonnet"}}, OPENAI_CFG
    )
    assert registry.to_llm_kwargs("review")["api_key"] == "anthropic-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert registry.to_llm_kwargs("review")["api_key"] is None


def test_to_llm_kwargs_threads_azure_deployments():
    """To llm kwargs threads azure deployments."""
    cfg = {
        "provider": "azure",
        "api_key": "azure-key",
        "azure_endpoint": "https://azure.example",
        "azure_api_version": "version",
    }
    registry = ModelRegistry.from_config(
        {"models": {"primary": "deployment"}}, cfg
    )
    kwargs = registry.to_llm_kwargs("primary")
    assert kwargs["model"] == "deployment"
    assert kwargs["provider"] == "azure"
    assert kwargs["api_key"] == "azure-key"
    assert kwargs["azure_endpoint"] == "https://azure.example"
    assert kwargs["azure_api_version"] == "version"


def test_to_llm_kwargs_threads_proxy_without_provider_sniffing():
    """To llm kwargs threads proxy without provider sniffing."""
    cfg = {**OPENAI_CFG, "base_url": "https://proxy.example/v1"}
    registry = ModelRegistry.from_config(
        {"models": {"primary": "claude-custom"}}, cfg
    )
    kwargs = registry.to_llm_kwargs("primary")
    assert kwargs["model"] == "claude-custom"
    assert kwargs["base_url"] == "https://proxy.example/v1"
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "openai-key"


def test_explicit_empty_llm_config_does_not_load_global_credentials(
    monkeypatch,
):
    """Explicit empty llm config does not load global credentials."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "psweep.utils.config.get_config",
        lambda: SimpleNamespace(llm_config=OPENAI_CFG),
    )
    registry = ModelRegistry.from_config({}, {})
    kwargs = registry.to_llm_kwargs(None)
    assert kwargs["model"] == llm_factory.DEFAULT_MODEL
    assert kwargs["api_key"] is None


def test_context_windows_remain_separate_from_alias_resolution(
    tmp_path, monkeypatch
):
    """Context windows remain separate from alias resolution."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "models:\n  primary: deployment\n"
        "model_context_windows:\n  deployment: 20000\n  other: 30000\n"
        "extraction:\n  schema: s.json\n  input_dir: documents\n",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)
    resolved = resolve_command_config(
        command="extract", cli_values={}, config_data=config_data, strict=True
    )
    windows = resolved["model_context_windows"]
    assert windows == {"deployment": 20000, "other": 30000}
    registry = ModelRegistry.from_config(config_data, OPENAI_CFG)
    kwargs = registry.to_llm_kwargs("primary")
    assert "context_windows" not in kwargs
    assert "context_window" not in kwargs

    # Client setup mutates ambient keys; keep this contract test isolated.
    monkeypatch.setenv("OPENAI_API_KEY", "test-original-key")
    client = LLMClient(**kwargs, context_windows=windows)
    assert client.context_windows == windows
    assert client._get_context_window_tokens() == 20000
    assert windows == config_data["model_context_windows"]


@pytest.mark.parametrize("window", [0, -1, True, 1.5, "20000"])
def test_strict_loader_rejects_invalid_context_windows(tmp_path, window):
    """Strict loader rejects invalid context windows."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump({"model_context_windows": {"deployment": window}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match="positive integer"):
        load_runtime_config_file(config_path)


def test_validate_accepts_well_formed_aliases_and_references():
    """Validate accepts well formed aliases and references."""
    registry = ModelRegistry.from_config(
        {"models": {"primary": "gpt-5"}}, OPENAI_CFG
    )
    registry.validate(references=["primary", "claude-3.5-sonnet", None])


@pytest.mark.parametrize("name", ["", "   ", 123])
def test_validate_rejects_invalid_direct_alias_name(name):
    """Validate rejects invalid direct alias name."""
    registry = ModelRegistry(
        tiers={name: ModelDefinition(tier="primary", model="gpt-5")}
    )
    with pytest.raises(ModelRegistryError, match="non-empty strings"):
        registry.validate()


@pytest.mark.parametrize("model", ["", "   ", None])
def test_validate_rejects_invalid_direct_model(model):
    """Validate rejects invalid direct model."""
    registry = ModelRegistry(
        tiers={"primary": ModelDefinition(tier="primary", model=model)}
    )
    with pytest.raises(ModelRegistryError, match="non-empty model"):
        registry.validate()


@pytest.mark.parametrize("reference", ["", "  ", 42])
def test_validate_rejects_invalid_single_reference(reference):
    """Validate rejects invalid single reference."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="non-empty string"):
        registry.validate(references=[reference])


def test_validate_multi_reference_requires_two_distinct_models():
    """Validate multi reference requires two distinct models."""
    registry = ModelRegistry.from_config(
        {"models": {"a": "gpt-5", "b": "gpt-4.1"}}, OPENAI_CFG
    )
    registry.validate(multi_references=[("validation.models", ["a", "b"])])


@pytest.mark.parametrize("references", [["a"], ["a", "alias"], []])
def test_validate_multi_reference_rejects_too_few_distinct_models(references):
    """Validate multi reference rejects too few distinct models."""
    registry = ModelRegistry.from_config(
        {"models": {"a": "gpt-5", "alias": "gpt-5"}}, OPENAI_CFG
    )
    with pytest.raises(
        ModelRegistryError, match=f"at least {MIN_MULTI_MODELS} distinct"
    ):
        registry.validate(multi_references=[("validation.models", references)])


def test_validate_multi_reference_rejects_none_list():
    """Validate multi reference rejects none list."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(
        ModelRegistryError, match=r"validation\.models must list at least"
    ):
        registry.validate(multi_references=[("validation.models", None)])


def test_validate_multi_reference_rejects_non_list():
    """Validate multi reference rejects non list."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(ModelRegistryError, match="must be a list"):
        registry.validate(multi_references=[("validation.models", "gpt-5")])


@pytest.mark.parametrize("reference", [" ", 42, None])
def test_validate_multi_reference_rejects_invalid_entry(reference):
    """Validate multi reference rejects invalid entry."""
    registry = ModelRegistry.from_config({}, OPENAI_CFG)
    with pytest.raises(
        ModelRegistryError, match="entries must be non-empty strings"
    ):
        registry.validate(
            multi_references=[("validation.models", ["gpt-5", reference])]
        )


def test_model_definition_is_frozen():
    """Model definition is frozen."""
    definition = ModelDefinition(tier="primary", model="gpt-5")
    with pytest.raises(FrozenInstanceError):
        definition.model = "other"
