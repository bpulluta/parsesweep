"""Target-provider abstraction for the discovery module.

A *target* is a plain ``dict[str, object]`` of context injected into query
templates. Providers generate that list from different sources — an authored
CSV, an inline list, an entity dataset, or a cross-product of dimensions — so
the pipeline can scale to thousands of targets (e.g. every US county, or a
manufacturer x power-class matrix) without hand-authoring each row.

Mirrors the connector pattern (ABC + ``supports_*`` + a ``resolve_*`` factory).
Everything downstream only needs ``list[dict]``, so any provider that emits
dicts works with the unchanged pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TargetProviderError(ValueError):
    """Raised when a target source is misconfigured or unreadable."""


class BaseTargetProvider(ABC):
    """Produces discovery targets from a configured source."""

    @abstractmethod
    def provide(self) -> list[dict[str, object]]:
        """Return the generated target rows (each a template context dict)."""

    @classmethod
    @abstractmethod
    def supports_source(cls, source_type: str) -> bool:
        """Return True if this provider handles the given ``source`` type."""
