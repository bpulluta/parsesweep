"""Resolve supported string aliases through the shared LLM factory.

The strict loader accepts ``models: {alias: model-name}`` only. Context
windows remain a separate model-name map passed directly to LLM clients,
not folded into aliases. The factory owns provider and credentials for
all callers, including QA/QC.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psweep.extraction import llm_factory

MIN_MULTI_MODELS = 2


class ModelRegistryError(ValueError):
    """Report invalid aliases or references at the loader boundary."""


@dataclass(frozen=True)
class ModelDefinition:
    """Identify a reference and its concrete client model."""

    tier: str
    model: str


class ModelRegistry:
    """Resolve aliases and validate single/multi-model references."""

    def __init__(
        self,
        tiers: Mapping[str, ModelDefinition] | None = None,
        llm_config: Mapping[str, Any] | None = None,
    ) -> None:
        """Store aliases and distinguish omitted/empty credentials."""
        self._tiers = dict(tiers or {})
        self._llm_config = dict(llm_config) if llm_config is not None else None

    @classmethod
    def from_config(
        cls,
        config_dict: Mapping[str, Any] | None,
        llm_config: Mapping[str, Any] | None = None,
    ) -> ModelRegistry:
        """Build from the strict loader's plain-string alias map."""
        cfg = config_dict or {}
        if "model_defaults" in cfg:
            message = (
                "'model_defaults' is not supported; set each stage's 'model' "
                "to a model string or alias instead"
            )
            raise ModelRegistryError(message)
        models = cfg.get("models")
        if models is None:
            models = {}
        if not isinstance(models, Mapping):
            message = "'models' must be a mapping of alias-name -> model-name"
            raise ModelRegistryError(message)
        registry = cls(
            tiers={
                name: ModelDefinition(tier=name, model=model)
                for name, model in models.items()
            },
            llm_config=llm_config,
        )
        registry._validate_structure()
        registry._tiers = {
            name: ModelDefinition(tier=name, model=definition.model.strip())
            for name, definition in registry._tiers.items()
        }
        return registry

    def get_model(self, tier_name: str | None) -> ModelDefinition:
        """Resolve an alias, literal, or omitted environment model."""
        if tier_name is not None and not isinstance(tier_name, str):
            message = (
                f"Model reference must be a string or None (got {tier_name!r})"
            )
            raise ModelRegistryError(message)
        if not tier_name or not tier_name.strip():
            model = llm_factory.resolve_model_name(
                None, llm_config=self._llm_config
            )
            return ModelDefinition(tier=model, model=model)
        if tier_name in self._tiers:
            return self._tiers[tier_name]
        return ModelDefinition(tier=tier_name, model=tier_name)

    def get_models(
        self, tier_names: Sequence[str] | None
    ) -> list[ModelDefinition]:
        """Resolve in order, deduplicating concrete models."""
        resolved: list[ModelDefinition] = []
        seen: set[str] = set()
        for name in tier_names or []:
            definition = self.get_model(name)
            if definition.model not in seen:
                seen.add(definition.model)
                resolved.append(definition)
        return resolved

    def to_llm_kwargs(self, tier_name: str | None) -> dict[str, Any]:
        """Resolve client kwargs through shared credential handling."""
        return llm_factory.resolve_llm_kwargs(
            self.get_model(tier_name).model,
            models={},  # Do not resolve an already-concrete model as an alias.
            llm_config=self._llm_config,
        )

    def validate(
        self,
        *,
        references: Iterable[str | None] | None = None,
        multi_references: Iterable[tuple[str, Sequence[str] | None]]
        | None = None,
    ) -> None:
        """Validate aliases, references, and distinct-model minimum."""
        self._validate_structure()
        for ref in references or []:
            if ref is not None and (
                not isinstance(ref, str) or not ref.strip()
            ):
                message = (
                    f"Model reference must be a non-empty string (got {ref!r})"
                )
                raise ModelRegistryError(message)
        for label, names in multi_references or []:
            self._validate_multi_reference(label, names)

    def _validate_structure(self) -> None:
        for name, definition in self._tiers.items():
            if not isinstance(name, str) or not name.strip():
                message = (
                    "Model alias names must be non-empty strings "
                    f"(got {name!r})"
                )
                raise ModelRegistryError(message)
            if (
                not isinstance(definition.model, str)
                or not definition.model.strip()
            ):
                message = (
                    f"Model alias '{name}' must map to a "
                    "non-empty model string; use "
                    "'models: {alias: model-name}', not a long-form mapping"
                )
                raise ModelRegistryError(message)

    def _validate_multi_reference(
        self,
        label: str,
        names: Sequence[str] | None,
    ) -> None:
        if names is None:
            message = f"{label} must list at least {MIN_MULTI_MODELS} models"
            raise ModelRegistryError(message)
        if not isinstance(names, (list, tuple)):
            message = (
                f"{label} must be a list of model names "
                f"(got {type(names).__name__})"
            )
            raise ModelRegistryError(message)
        for name in names:
            if not isinstance(name, str) or not name.strip():
                message = (
                    f"{label} entries must be non-empty strings (got {name!r})"
                )
                raise ModelRegistryError(message)
        distinct = {self.get_model(name).model for name in names}
        if len(distinct) < MIN_MULTI_MODELS:
            message = (
                f"{label} must resolve to at least "
                f"{MIN_MULTI_MODELS} distinct models (got {sorted(distinct)})"
            )
            raise ModelRegistryError(message)


__all__ = [
    "MIN_MULTI_MODELS",
    "ModelDefinition",
    "ModelRegistry",
    "ModelRegistryError",
]
