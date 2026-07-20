"""Unified per-stage LLM model resolver / factory.

One place that turns a stage's ``model`` config value into a configured
``LLMClient``. Every LLM-using stage (extraction, document review, page
targeting, synthesis) routes through here, so each stage can be pointed at a
different model for cost/quality control — across any provider.

The simplest, recommended usage is to name the model directly on each stage::

    processing:
      model: gpt-5                 # extraction model
    acquisition:
      document_review:
        model: gpt-4o-mini         # a cheaper model for bulk curation

Optionally, define reusable named aliases once in a top-level ``models`` block
and reference an alias wherever a model is expected. The alias names are entirely
user-chosen (there are no reserved names)::

    models:
      fast: gpt-4o-mini
      accurate: gpt-5
    processing:
      model: accurate              # alias, OR an explicit model string

Model-name resolution precedence (``resolve_model_name``):

1. ``stage_value`` — an alias lookup (``stage_value in models``) when it matches
   a defined alias, otherwise treated as a literal model name.
2. ``default_model`` — an explicit fallback the caller supplies (rarely needed).
3. ``llm_config["model"]`` — the env/``.env``-derived default.
4. ``DEFAULT_MODEL`` (``"gpt-4o-mini"``) as the final floor.

Credential threading & the one honest limitation
-------------------------------------------------
``get_config().llm_config`` holds credentials for exactly ONE provider (the one
configured in the environment). Choosing different models of the SAME provider
(e.g. two Azure deployments, or two OpenAI models) is fully supported and is the
intended use. If a stage's model names a *different* provider than the configured
one (e.g. a ``claude-*`` model while the run is configured for Azure), its API
key can only come from the ambient environment; when that key is absent,
``api_key`` is ``None`` and LiteLLM will error at call time. Cross-provider
per-stage selection is therefore best-effort, not guaranteed.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "gpt-4o-mini"

# Provider -> ambient env var holding that provider's API key. Used only on the
# cross-provider fallback path (resolved model's provider != configured one).
_PROVIDER_ENV_KEY: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def _get_llm_config(llm_config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the given llm_config or lazily load the global one."""
    if llm_config is not None:
        return llm_config
    # Lazy import avoids an import cycle (config.py never imports this module).
    from ..utils.config import get_config

    return get_config().llm_config or {}


def resolve_model_name(
    stage_value: str | None,
    *,
    models: dict[str, str] | None = None,
    llm_config: dict[str, Any] | None = None,
    default_model: str | None = None,
) -> str:
    """Resolve a stage's ``model`` config value to a concrete model name.

    ``stage_value`` may be a named alias (a key in ``models``) or an explicit
    model string. See the module docstring for the full precedence chain.
    """
    models = models or {}
    cfg = _get_llm_config(llm_config)

    if stage_value:
        if stage_value in models:
            return models[stage_value]
        return stage_value
    if default_model:
        return models.get(default_model, default_model)
    if cfg.get("model"):
        return str(cfg["model"])
    return DEFAULT_MODEL


def detect_provider(model: str) -> str:
    """Detect an LLM provider from a model name (domain-neutral, best-effort).

    This is the single source of truth for name-based provider detection; both
    the factory and ``LLMClient`` use it, so there is only one place that maps
    model names to providers.

    Detection is heuristic and only a *fallback*: the pipeline normally sets the
    provider explicitly (``get_config().llm_config['provider']`` is always
    populated from the environment). Azure custom deployment names are arbitrary
    and carry no reliable marker, so the correct signal for Azure is an explicit
    ``provider: azure`` — not a name sniff. Only the canonical ``azure/`` prefix
    is treated as an Azure marker here.
    """
    m = model.lower()
    if "azure/" in m:
        return "azure"
    if "claude" in m:
        return "anthropic"
    if "gemini" in m or "gemma" in m:
        return "gemini"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "openai"
    if "llama" in m:
        return "meta"
    if "mistral" in m or "codestral" in m:
        return "mistral"
    return "openai"


# Backwards-compatible private alias (kept for any internal callers/tests).
_detect_provider = detect_provider


def resolve_llm_kwargs(
    stage_value: str | None,
    *,
    models: dict[str, str] | None = None,
    llm_config: dict[str, Any] | None = None,
    default_model: str | None = None,
) -> dict[str, Any]:
    """Resolve the kwargs needed to construct an ``LLMClient`` for a stage.

    Returns ``{model, api_key, provider, azure_endpoint, azure_api_version}``.
    """
    cfg = _get_llm_config(llm_config)
    resolved_model = resolve_model_name(
        stage_value,
        models=models,
        llm_config=cfg,
        default_model=default_model,
    )

    cfg_provider = cfg.get("provider")
    target = detect_provider(resolved_model)

    # Azure deployment names usually carry no azure/ marker, so provider
    # detection misreads them as "openai". When the environment is configured for
    # Azure and the model shows no *foreign* marker, treat it as an Azure
    # deployment. This — not a name sniff — is how Azure deployments are honored.
    if cfg_provider == "azure" and target in ("openai", "azure"):
        target = "azure"

    if cfg_provider and target == cfg_provider:
        # Same provider as configured — the common case. Thread the configured
        # credentials (identical to today's behavior).
        return {
            "model": resolved_model,
            "api_key": cfg.get("api_key"),
            "provider": cfg_provider,
            "azure_endpoint": cfg.get("azure_endpoint"),
            "azure_api_version": cfg.get("azure_api_version"),
        }

    # Cross-provider: the model names a provider whose creds are not in
    # llm_config. Best-effort fallback to that provider's ambient env key.
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
    """Build a configured ``LLMClient`` for a stage's ``model`` value."""
    from .llm_client import LLMClient

    return LLMClient(
        **resolve_llm_kwargs(
            stage_value,
            models=models,
            llm_config=llm_config,
            default_model=default_model,
        )
    )
