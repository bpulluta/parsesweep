"""Resolve model names, providers, and credentials for every LLM stage.

Extraction, discovery review, page targeting, QA/QC, and synthesis share
this boundary. Stage values can be literal model names or string aliases
from the top-level ``models`` map. Alias names are user-chosen.

Model precedence is stage value (alias or literal), explicit caller
fallback, environment-configured model, then ``DEFAULT_MODEL``.

An explicit supported provider wins over name-based detection. ``None``
and ``"auto"`` retain configured/heuristic resolution, including custom
Azure deployment names. Credentials come from the configured provider
when it matches, otherwise only from the target's ambient environment.
A missing target key remains ``None``, never another provider's key.

A configured ``base_url`` routes all models through that proxy with its
configured key, regardless of provider override or model name.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"

# Single source for the character limit shared by CLI/API extraction,
# multi-model validation, and DocumentExtractor. See the defaults tests.
DEFAULT_MAX_CONTEXT = 600000

# Canonical runtime providers, covering every detect_provider result.
# "auto" requests resolution rather than naming a runtime provider.
KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"openai", "azure", "anthropic", "gemini", "meta", "mistral"}
)

# Target provider's ambient key, used only across providers. Meta/Llama
# has no canonical host/key; use configured credentials or a proxy.
_PROVIDER_ENV_KEY: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


def _get_llm_config(llm_config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the given llm_config or lazily load the global one."""
    if llm_config is not None:
        return llm_config
    # Config imports this factory when selecting its OpenAI default.
    from psweep.utils.config import get_config  # noqa: PLC0415

    return get_config().llm_config or {}


def resolve_model_name(
    stage_value: str | None,
    *,
    models: dict[str, str] | None = None,
    llm_config: dict[str, Any] | None = None,
    default_model: str | None = None,
) -> str:
    """Resolve a model alias or literal using shared precedence."""
    models = models or {}
    cfg = _get_llm_config(llm_config)

    if stage_value:
        return models.get(stage_value, stage_value)
    if default_model:
        return models.get(default_model, default_model)
    if cfg.get("model"):
        return str(cfg["model"])
    return DEFAULT_MODEL


def detect_provider(model: str) -> str:
    """Detect a provider from ordered model markers, as a fallback only.

    Both the factory and LLMClient use this heuristic. Azure deployments
    have arbitrary names, so explicit provider/configuration wins over
    detection; only ``azure/`` identifies Azure by name.
    """
    name = model.lower()
    for provider, markers in (
        ("azure", ("azure/",)),
        ("anthropic", ("claude",)),
        ("gemini", ("gemini", "gemma")),
        ("openai", ("gpt", "o1", "o3")),
        ("meta", ("llama",)),
        ("mistral", ("mistral", "codestral")),
    ):
        if any(marker in name for marker in markers):
            return provider
    return "openai"


def resolve_llm_kwargs(
    stage_value: str | None,
    *,
    models: dict[str, str] | None = None,
    llm_config: dict[str, Any] | None = None,
    default_model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Resolve LLM client kwargs using one provider/credential boundary.

    A supported explicit ``provider`` overrides name detection. ``None``
    or ``"auto"`` retains configured/heuristic resolution. Credentials
    always belong to the target provider, never another configured one.

    A configured ``base_url`` retains proxy routing and its configured
    key regardless of provider/model name. Azure endpoint/version apply
    only to direct Azure routing.
    """
    if provider is not None:
        if not isinstance(provider, str) or provider.lower() not in (
            KNOWN_PROVIDERS | {"auto"}
        ):
            message = "provider must be 'auto' or one of: " + ", ".join(
                sorted(KNOWN_PROVIDERS)
            )
            raise ValueError(message)
        provider = provider.lower()
        if provider == "auto":
            provider = None
    cfg = _get_llm_config(llm_config)
    resolved_model = resolve_model_name(
        stage_value,
        models=models,
        llm_config=cfg,
        default_model=default_model,
    )

    # Proxy credentials/routing apply regardless of provider or model.
    if cfg.get("base_url"):
        return {
            "model": resolved_model,
            "api_key": cfg.get("api_key"),
            "provider": provider or cfg.get("provider") or "openai",
            "base_url": cfg["base_url"],
        }

    cfg_provider = cfg.get("provider")
    target = provider or detect_provider(resolved_model)

    # Arbitrary Azure deployment names usually look like OpenAI models.
    # Use configured Azure only without an explicit provider override.
    if (
        provider is None
        and cfg_provider == "azure"
        and target in {"openai", "azure"}
    ):
        target = "azure"

    if cfg_provider and target == cfg_provider:
        return {
            "model": resolved_model,
            "api_key": cfg.get("api_key"),
            "provider": target,
            "azure_endpoint": cfg.get("azure_endpoint")
            if target == "azure"
            else None,
            "azure_api_version": cfg.get("azure_api_version")
            if target == "azure"
            else None,
        }

    # Cross-provider: use only the target's ambient key and settings.
    env_key = _PROVIDER_ENV_KEY.get(target)
    api_key = os.getenv(env_key) if env_key else None
    if target == "gemini" and not api_key:
        api_key = os.getenv("GOOGLE_API_KEY")
    return {
        "model": resolved_model,
        "api_key": api_key,
        "provider": target,
        "azure_endpoint": (
            os.getenv("AZURE_OPENAI_ENDPOINT") if target == "azure" else None
        ),
        "azure_api_version": (
            os.getenv("AZURE_OPENAI_API_VERSION")
            if target == "azure"
            else None
        ),
    }


def build_llm_client(
    stage_value: str | None,
    *,
    models: dict[str, str] | None = None,
    llm_config: dict[str, Any] | None = None,
    default_model: str | None = None,
) -> Any:
    """Build a configured LLMClient for a stage's model value."""
    # LLMClient imports model defaults and detection from this module.
    from .llm_client import LLMClient  # noqa: PLC0415

    kwargs = resolve_llm_kwargs(
        stage_value,
        models=models,
        llm_config=llm_config,
        default_model=default_model,
    )
    if (
        kwargs.get("provider") == "azure"
        and kwargs.get("model") == DEFAULT_MODEL
    ):
        logger.warning(
            "Provider resolved to azure but model is still '%s'. "
            "Pin your model in run.yaml models: block or pass --model.",
            DEFAULT_MODEL,
        )
    return LLMClient(**kwargs)
