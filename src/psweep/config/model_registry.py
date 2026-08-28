"""Unified model registry — one normalization boundary for model tiers.

This module centralizes how a run's model *tiers* (the top-level ``models:``
alias map) and per-model context windows (``model_context_windows:``) become
resolvable :class:`ModelDefinition` objects. It is a thin **facade** over the
low-level primitives in :mod:`psweep.extraction.llm_factory`:

* :func:`ModelRegistry.from_config` is the ONLY place where the various config
  shapes (a plain model string, a long-form ``{model, provider, ...}`` dict, or
  the legacy ``model_context_windows`` side-map) get normalized into
  :class:`ModelDefinition` instances. Everything downstream consumes the
  normalized form.
* :meth:`ModelRegistry.to_llm_kwargs` delegates to
  ``llm_factory.resolve_llm_kwargs`` so credentials and the concrete provider
  are resolved through the single, env-driven path — never re-derived here.

Import-cycle discipline
-----------------------
The registry may call into ``llm_factory`` (lazily, or via an injected
callable), but ``llm_factory`` NEVER imports this module. That keeps the
dependency arrow one-directional (registry → factory) so there is no cycle.

Provider stays env-driven
-------------------------
A :class:`ModelDefinition` may carry a *declared* ``provider`` hint (from a
long-form config entry), but the runtime provider and credentials always come
from :func:`resolve_llm_kwargs`, which reads ``llm_config['provider']`` (the
environment-derived value). The registry never sniffs a provider from a model
name for credential purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# Import the single sources of truth from the factory. The dependency arrow is
# one-directional (registry -> factory; ``llm_factory`` NEVER imports this
# module), so a top-level import here is cycle-safe.
from ..extraction.llm_factory import DEFAULT_MODEL, KNOWN_PROVIDERS

# Concrete fallback for ``_floor_model`` when the (defensive) lazy re-import of
# the factory fails. Bound to ``llm_factory.DEFAULT_MODEL`` so there is exactly
# one place the "gpt-4o-mini" floor is defined.
_DEFAULT_MODEL_FALLBACK = DEFAULT_MODEL

# ``KNOWN_PROVIDERS`` is re-exported (see ``__all__``) so existing importers of
# ``psweep.config.model_registry.KNOWN_PROVIDERS`` keep working. It is defined in
# ``llm_factory`` — a *declared* provider outside this set is a config error.

# Minimum distinct models required for a multi-model (e.g. QA/QC) reference.
MIN_MULTI_MODELS = 2


class ModelRegistryError(ValueError):
    """Raised when a model registry cannot be built or validated.

    Kept local to this module (rather than reusing ``RuntimeConfigError``) so
    the registry has no dependency back on the loader; the loader converts these
    into ``RuntimeConfigError`` at its boundary.
    """


@dataclass(frozen=True)
class ModelDefinition:
    """A fully normalized model tier — immutable after normalization.

    Attributes
    ----------
    tier:
        The tier/alias name this definition was resolved from (e.g. ``primary``)
        or the literal model name when the reference was not a defined alias.
    model:
        The concrete model / deployment name passed to the LLM client.
    provider:
        Optional *declared* provider hint (from a long-form config entry). This
        is metadata only; runtime credentials come from ``resolve_llm_kwargs``
        (env-driven). ``None`` means "use the environment-configured provider".
    context_window:
        Maximum prompt tokens for this model, or ``None`` if unknown. Folded in
        from a per-tier ``context_window`` or the top-level
        ``model_context_windows`` map at normalization time.
    params:
        Optional extra generation parameters declared in long-form config.
        Frozen semantics: treat as read-only after construction.
    """

    tier: str
    model: str
    provider: str | None = None
    context_window: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


class ModelRegistry:
    """Resolve model tier references to :class:`ModelDefinition` objects.

    The registry is built once per run via :meth:`from_config` (the single
    normalization boundary) and then queried by every LLM-using stage.
    """

    def __init__(
        self,
        tiers: Mapping[str, ModelDefinition] | None = None,
        defaults: Mapping[str, str] | None = None,
        llm_config: Mapping[str, Any] | None = None,
        *,
        context_windows: Mapping[str, int] | None = None,
        resolve_kwargs: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        """Construct a registry from already-normalized tiers.

        Prefer :meth:`from_config` for building from raw config. Direct
        construction is primarily for tests and internal use.

        Parameters
        ----------
        tiers:
            Alias/tier name -> :class:`ModelDefinition`.
        defaults:
            Optional phase-name -> tier/model default used by
            :meth:`resolve_for_phase` when a phase supplies no explicit model.
        llm_config:
            Env-derived provider/credentials dict (``get_config().llm_config``).
            May be ``None``; the delegated ``resolve_llm_kwargs`` will lazily
            load the global config in that case.
        context_windows:
            Concrete-model-name -> context window map (the legacy
            ``model_context_windows`` block). Used to fold a window into
            on-the-fly literal definitions.
        resolve_kwargs:
            Injected ``resolve_llm_kwargs``-compatible callable. When ``None``,
            it is imported lazily from ``psweep.extraction.llm_factory`` so this
            module never imports the factory at import time (cycle-safe).
        """
        self._tiers: dict[str, ModelDefinition] = dict(tiers or {})
        self._defaults: dict[str, str] = dict(defaults or {})
        self._llm_config: dict[str, Any] = dict(llm_config or {})
        self._context_windows: dict[str, int] = dict(context_windows or {})
        self._resolve_kwargs = resolve_kwargs

    # ── construction / normalization boundary ──────────────────────────────

    @classmethod
    def from_config(
        cls,
        config_dict: Mapping[str, Any] | None,
        llm_config: Mapping[str, Any] | None = None,
        *,
        resolve_kwargs: Callable[..., dict[str, Any]] | None = None,
    ) -> "ModelRegistry":
        """Normalize a runtime config dict into a :class:`ModelRegistry`.

        This is the ONE place that understands the raw config shapes:

        * ``models: {primary: gpt-4.1}`` — string form.
        * ``models: {primary: {model: gpt-4.1, provider: azure,
          context_window: 200000, params: {...}}}`` — long-form dict.
        * ``model_context_windows: {gpt-4.1: 200000}`` — legacy side-map folded
          into each matching tier's ``context_window``.
        * ``model_defaults: {extraction: primary}`` — optional per-phase
          defaults (additive; safe to omit).

        A missing or empty ``models`` block yields an empty-tier registry that
        still resolves every reference as a literal model name (backward-compat).
        """
        cfg = dict(config_dict or {})
        models_block = cfg.get("models") or {}
        windows_block = cfg.get("model_context_windows") or {}
        defaults_block = cfg.get("model_defaults") or {}

        if not isinstance(models_block, Mapping):
            raise ModelRegistryError(
                "'models' must be a mapping of tier-name -> model spec"
            )
        if not isinstance(windows_block, Mapping):
            raise ModelRegistryError(
                "'model_context_windows' must be a mapping of model-name -> "
                "max prompt tokens"
            )
        if not isinstance(defaults_block, Mapping):
            raise ModelRegistryError(
                "'model_defaults' must be a mapping of phase-name -> tier-name"
            )

        tiers: dict[str, ModelDefinition] = {}
        for name, spec in models_block.items():
            tiers[name] = cls._normalize_tier(name, spec, windows_block)

        return cls(
            tiers=tiers,
            defaults=dict(defaults_block),
            llm_config=llm_config,
            context_windows=dict(windows_block),
            resolve_kwargs=resolve_kwargs,
        )

    @staticmethod
    def _normalize_tier(
        name: Any,
        spec: Any,
        windows_block: Mapping[str, Any],
    ) -> ModelDefinition:
        """Normalize a single ``models`` entry into a :class:`ModelDefinition`."""
        if not isinstance(name, str) or not name.strip():
            raise ModelRegistryError(
                f"Model tier names must be non-empty strings (got {name!r})"
            )

        if isinstance(spec, str):
            model = spec.strip()
            provider = None
            context_window = windows_block.get(model)
            params: Mapping[str, Any] = {}
        elif isinstance(spec, Mapping):
            model_value = spec.get("model")
            if not isinstance(model_value, str) or not model_value.strip():
                raise ModelRegistryError(
                    f"Model tier '{name}' must define a non-empty 'model' string"
                )
            model = model_value.strip()
            provider = spec.get("provider")
            context_window = spec.get("context_window")
            if context_window is None:
                context_window = windows_block.get(model)
            params = dict(spec.get("params") or {})
        else:
            raise ModelRegistryError(
                f"Model tier '{name}' must be a model string or a mapping "
                f"(got {type(spec).__name__})"
            )

        if not model:
            raise ModelRegistryError(
                f"Model tier '{name}' resolves to an empty model name"
            )

        return ModelDefinition(
            tier=name,
            model=model,
            provider=provider,
            context_window=context_window,
            params=params,
        )

    # ── resolution primitives ──────────────────────────────────────────────

    def get_model(self, tier_name: str | None) -> ModelDefinition:
        """Resolve one tier/model reference to a :class:`ModelDefinition`.

        Precedence:

        1. ``tier_name`` matches a defined alias -> that tier's definition.
        2. ``tier_name`` is any other non-empty string -> a literal model
           definition (with a context window folded in if one is declared).
        3. ``tier_name`` is ``None``/empty -> the environment "floor" model.
        """
        if tier_name is None:
            return self._floor_definition()
        if not isinstance(tier_name, str):
            raise ModelRegistryError(
                f"Model reference must be a string or None (got {tier_name!r})"
            )
        if not tier_name.strip():
            return self._floor_definition()

        if tier_name in self._tiers:
            return self._tiers[tier_name]

        return ModelDefinition(
            tier=tier_name,
            model=tier_name,
            provider=None,
            context_window=self._context_windows.get(tier_name),
            params={},
        )

    def get_models(
        self, tier_names: Sequence[str] | None
    ) -> list[ModelDefinition]:
        """Resolve a list of references, de-duplicating by concrete model name.

        Order is preserved (first occurrence wins). This is the primitive a
        multi-model consumer (e.g. QA/QC) uses to expand a ``models:`` list.
        """
        resolved: list[ModelDefinition] = []
        seen: set[str] = set()
        for name in tier_names or []:
            definition = self.get_model(name)
            if definition.model in seen:
                continue
            seen.add(definition.model)
            resolved.append(definition)
        return resolved

    def resolve_for_phase(
        self,
        phase_name: str,
        explicit: str | None = None,
    ) -> ModelDefinition:
        """Resolve the model for a pipeline phase using hybrid precedence.

        Precedence: an ``explicit`` per-phase reference (the phase's ``model:``
        value) wins; otherwise a registry-level default for ``phase_name``;
        otherwise the environment floor.
        """
        if explicit is not None and str(explicit).strip():
            return self.get_model(explicit)
        default = self._defaults.get(phase_name)
        if default is not None and str(default).strip():
            return self.get_model(default)
        return self._floor_definition()

    def to_llm_kwargs(self, tier_name: str | None) -> dict[str, Any]:
        """Resolve LLM client kwargs for a tier via the single credential path.

        Returns ``{model, api_key, provider, azure_endpoint, azure_api_version}``
        by delegating to ``resolve_llm_kwargs``. The concrete provider and
        credentials come from the env-driven ``llm_config`` — never re-derived
        in the registry.
        """
        definition = self.get_model(tier_name)
        resolve = self._resolve_kwargs or self._default_resolve_kwargs
        # ``models={}`` forces the already-concrete model name to pass through
        # ``resolve_llm_kwargs`` as a literal (no second alias lookup).
        return resolve(
            definition.model,
            models={},
            llm_config=self._llm_config or None,
        )

    # ── validation ─────────────────────────────────────────────────────────

    def validate(
        self,
        *,
        references: Iterable[str | None] | None = None,
        multi_references: Iterable[tuple[str, Sequence[str] | None]]
        | None = None,
    ) -> None:
        """Fail-fast load-time validation.

        Validates, in order:

        * **Structure** — every tier has a non-empty model, a valid (or absent)
          declared provider, a positive-or-absent context window, and a mapping
          of params.
        * **Defaults** — each per-phase default is a non-empty string.
        * **References** (single ``model:`` fields) — each must be a non-empty
          string (both aliases and literals are accepted, so resolvability is
          guaranteed once the value is a valid string).
        * **Multi-references** (``models:`` lists, e.g. QA/QC) — each must
          resolve to at least :data:`MIN_MULTI_MODELS` distinct models.

        Raises
        ------
        ModelRegistryError
            With a clear, actionable message on the first violation found.
        """
        self._validate_structure()
        self._validate_defaults()

        for ref in references or []:
            self._validate_single_reference(ref)

        for label, names in multi_references or []:
            self._validate_multi_reference(label, names)

    def _validate_structure(self) -> None:
        for name, definition in self._tiers.items():
            if not isinstance(name, str) or not name.strip():
                raise ModelRegistryError(
                    f"Model tier names must be non-empty strings (got {name!r})"
                )
            if not isinstance(definition.model, str) or not definition.model.strip():
                raise ModelRegistryError(
                    f"Model tier '{name}' must map to a non-empty model name"
                )
            provider = definition.provider
            if provider is not None and provider not in KNOWN_PROVIDERS:
                known = ", ".join(sorted(KNOWN_PROVIDERS))
                raise ModelRegistryError(
                    f"Model tier '{name}' declares unknown provider "
                    f"'{provider}'. Known providers: {known}"
                )
            window = definition.context_window
            if window is not None and (
                isinstance(window, bool) or not isinstance(window, int) or window <= 0
            ):
                raise ModelRegistryError(
                    f"Model tier '{name}' context_window must be a positive "
                    f"integer (got {window!r})"
                )
            if not isinstance(definition.params, Mapping):
                raise ModelRegistryError(
                    f"Model tier '{name}' params must be a mapping "
                    f"(got {type(definition.params).__name__})"
                )

    def _validate_defaults(self) -> None:
        for phase, tier in self._defaults.items():
            if not isinstance(phase, str) or not phase.strip():
                raise ModelRegistryError(
                    "model_defaults phase names must be non-empty strings "
                    f"(got {phase!r})"
                )
            if not isinstance(tier, str) or not tier.strip():
                raise ModelRegistryError(
                    f"model_defaults['{phase}'] must be a non-empty tier or "
                    f"model name (got {tier!r})"
                )

    def _validate_single_reference(self, ref: str | None) -> None:
        if ref is None:
            return
        if not isinstance(ref, str) or not ref.strip():
            raise ModelRegistryError(
                f"Model reference must be a non-empty string (got {ref!r})"
            )

    def _validate_multi_reference(
        self,
        label: str,
        names: Sequence[str] | None,
    ) -> None:
        if names is None:
            raise ModelRegistryError(
                f"{label} must list at least {MIN_MULTI_MODELS} models"
            )
        if not isinstance(names, (list, tuple)):
            raise ModelRegistryError(
                f"{label} must be a list of model names (got "
                f"{type(names).__name__})"
            )
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise ModelRegistryError(
                    f"{label} entries must be non-empty strings (got {name!r})"
                )
        distinct = {self.get_model(name).model for name in names}
        if len(distinct) < MIN_MULTI_MODELS:
            raise ModelRegistryError(
                f"{label} must resolve to at least {MIN_MULTI_MODELS} distinct "
                f"models (got {sorted(distinct)})"
            )

    # ── internals ──────────────────────────────────────────────────────────

    def _floor_definition(self) -> ModelDefinition:
        """Definition for the environment "floor" model (no reference given)."""
        model = self._floor_model()
        return ModelDefinition(
            tier=model,
            model=model,
            provider=None,
            context_window=self._context_windows.get(model),
            params={},
        )

    def _floor_model(self) -> str:
        configured = self._llm_config.get("model") if self._llm_config else None
        if configured:
            return str(configured)
        try:
            from ..extraction.llm_factory import DEFAULT_MODEL

            return DEFAULT_MODEL
        except Exception:  # pragma: no cover - defensive; keeps registry usable
            return _DEFAULT_MODEL_FALLBACK

    @staticmethod
    def _default_resolve_kwargs(
        stage_value: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Lazy bridge to ``llm_factory.resolve_llm_kwargs`` (cycle-safe)."""
        from ..extraction.llm_factory import resolve_llm_kwargs

        return resolve_llm_kwargs(stage_value, **kwargs)


__all__ = [
    "ModelDefinition",
    "ModelRegistry",
    "ModelRegistryError",
    "KNOWN_PROVIDERS",
    "MIN_MULTI_MODELS",
]
