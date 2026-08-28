"""Single-source-of-truth checks for LLM routing constants.

``DEFAULT_MODEL`` (the "gpt-4o-mini" floor) and ``KNOWN_PROVIDERS`` (the
canonical provider set) are defined once in ``llm_factory`` and consumed
everywhere else. These tests lock that in so the value cannot silently drift
back into three separate definitions.
"""

from __future__ import annotations

from psweep.config import model_registry
from psweep.extraction import llm_factory
from psweep.utils.config import Config

# Env keys that select a provider ahead of the OPENAI_API_KEY branch, plus the
# OpenAI model override — cleared so ``_load_llm_config`` deterministically hits
# the OpenAI default-model path.
_PROVIDER_ENV_KEYS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_MODEL",
    "AZURE_OPENAI_ENDPOINT",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_MODEL",
)


def test_registry_default_model_is_factory_default_model():
    # model_registry imports the value; the defensive fallback is bound to it.
    assert model_registry.DEFAULT_MODEL is llm_factory.DEFAULT_MODEL
    assert model_registry._DEFAULT_MODEL_FALLBACK is llm_factory.DEFAULT_MODEL


def test_config_default_model_is_factory_default_model(monkeypatch, tmp_path):
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    cfg = Config(project_root=tmp_path)  # tmp_path has no .env -> pure env read

    assert cfg.llm_config["provider"] == "openai"
    assert cfg.llm_config["model"] == llm_factory.DEFAULT_MODEL


def test_known_providers_is_shared_across_modules():
    # Re-exported, not redefined: same object in both namespaces.
    assert model_registry.KNOWN_PROVIDERS is llm_factory.KNOWN_PROVIDERS
