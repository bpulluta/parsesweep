"""Target-provider package: pluggable sources for acquisition targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseTargetProvider, TargetProviderError
from .providers import (
    CrossProductTargetProvider,
    CsvTargetProvider,
    DatasetTargetProvider,
    InlineTargetProvider,
    load_rows_from_file,
)

__all__ = [
    "BaseTargetProvider",
    "CrossProductTargetProvider",
    "CsvTargetProvider",
    "DatasetTargetProvider",
    "InlineTargetProvider",
    "TargetProviderError",
    "load_rows_from_file",
    "resolve_target_provider",
]


def resolve_target_provider(
    spec: dict[str, Any], *, config_dir: Path
) -> BaseTargetProvider:
    """Build a target provider from a ``targets`` config dict.

    The dict must carry a ``source`` key of ``dataset`` or ``cross_product``
    (``csv``/``inline`` are handled directly by the config loader from their
    shorthand forms). Relative dataset paths resolve against ``config_dir``.
    """
    source = str(spec.get("source") or "").strip()
    if not source:
        msg = "targets config object requires a 'source' field."
        raise TargetProviderError(msg)

    if DatasetTargetProvider.supports_source(source):
        path = Path(str(spec.get("path") or spec.get("dataset") or ""))
        if not str(path):
            msg = "dataset target source requires a 'path'."
            raise TargetProviderError(msg)
        if not path.is_absolute():
            path = config_dir / path
        limit = spec.get("limit")
        return DatasetTargetProvider(
            path, limit=limit if isinstance(limit, int) else None
        )

    if CrossProductTargetProvider.supports_source(source):
        dimensions = spec.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            msg = "cross_product target source requires 'dimensions'."
            raise TargetProviderError(msg)
        limit = spec.get("limit")
        return CrossProductTargetProvider(
            dimensions,
            config_dir=config_dir,
            filters=spec.get("filter"),
            limit=limit if isinstance(limit, int) else None,
        )

    msg = f"Unknown target source: '{source}'."
    raise TargetProviderError(msg)
