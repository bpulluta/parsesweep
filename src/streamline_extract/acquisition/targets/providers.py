"""Concrete target providers: csv, inline, dataset, cross_product.

Each provider turns a configured source into ``list[dict]`` targets. The
cross-product provider is the scalable path: it takes N named dimensions
(each a value list or an entity dataset) and emits every combination, so a
domain scales to thousands of targets from a compact config.
"""

from __future__ import annotations

import csv
import json
from itertools import product
from pathlib import Path
from typing import Any

from .base import BaseTargetProvider, TargetProviderError


def load_rows_from_file(path: Path) -> list[dict[str, Any]]:
    """Load entity rows from a ``.csv`` or ``.json`` file.

    CSV headers (or JSON object keys) become target fields; empty CSV cells
    become ``None``. JSON must be a list of objects.
    """
    if not path.exists():
        msg = f"Target dataset file not found: {path.as_posix()}"
        raise TargetProviderError(msg)

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            msg = f"Failed to parse JSON dataset {path.as_posix()}: {exc}"
            raise TargetProviderError(msg) from exc
        if not isinstance(data, list) or not all(
            isinstance(r, dict) for r in data
        ):
            msg = f"JSON dataset must be a list of objects: {path.as_posix()}"
            raise TargetProviderError(msg)
        return [dict(r) for r in data]

    if suffix == ".csv":
        try:
            with path.open(encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                rows = [
                    {k: (v or None) for k, v in row.items()} for row in reader
                ]
        except Exception as exc:
            msg = f"Failed to read CSV dataset {path.as_posix()}: {exc}"
            raise TargetProviderError(msg) from exc
        if not fieldnames:
            msg = f"CSV dataset is empty: {path.as_posix()}"
            raise TargetProviderError(msg)
        return rows

    msg = f"Unsupported dataset type '{suffix}' ({path.as_posix()})"
    raise TargetProviderError(msg)


class CsvTargetProvider(BaseTargetProvider):
    """One target per row of an authored targets CSV."""

    def __init__(self, csv_path: Path) -> None:
        self._csv_path = csv_path

    @classmethod
    def supports_source(cls, source_type: str) -> bool:
        """Return True for the ``csv`` source."""
        return source_type == "csv"

    def provide(self) -> list[dict[str, object]]:
        """Return one target per CSV row."""
        return load_rows_from_file(self._csv_path)


class InlineTargetProvider(BaseTargetProvider):
    """Pass-through provider for inline target lists."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    @classmethod
    def supports_source(cls, source_type: str) -> bool:
        """Return True for the ``inline`` source."""
        return source_type == "inline"

    def provide(self) -> list[dict[str, object]]:
        """Return the inline target rows unchanged."""
        return [dict(r) for r in self._rows if isinstance(r, dict)]


class DatasetTargetProvider(BaseTargetProvider):
    """One target per entity row loaded from a dataset file (csv/json)."""

    def __init__(self, path: Path, limit: int | None = None) -> None:
        self._path = path
        self._limit = limit

    @classmethod
    def supports_source(cls, source_type: str) -> bool:
        """Return True for the ``dataset`` source."""
        return source_type == "dataset"

    def provide(self) -> list[dict[str, object]]:
        """Return one target per dataset row (optionally capped)."""
        rows = load_rows_from_file(self._path)
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows


class CrossProductTargetProvider(BaseTargetProvider):
    """Cartesian product of named dimensions: one target per combination.

    Each dimension is either a value list (``{name, values}``) or an entity
    dataset (``{name, dataset, [column], [limit]}``). Value dimensions
    contribute ``{name: value}``; dataset dimensions contribute the whole row
    (or ``{name: row[column]}`` when ``column`` is given). An optional
    ``filter`` (``{field: [allowed]}``) and total ``limit`` are applied last.
    """

    def __init__(
        self,
        dimensions: list[dict[str, Any]],
        *,
        config_dir: Path,
        filters: dict[str, list[Any]] | None = None,
        limit: int | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._config_dir = config_dir
        self._filters = filters or {}
        self._limit = limit

    @classmethod
    def supports_source(cls, source_type: str) -> bool:
        """Return True for the ``cross_product`` source."""
        return source_type == "cross_product"

    def _resolve_dimension(
        self, dimension: dict[str, Any]
    ) -> list[dict[str, Any]]:
        name = dimension.get("name")
        if not name:
            msg = "Each cross_product dimension requires a 'name'."
            raise TargetProviderError(msg)

        if "values" in dimension:
            values = dimension.get("values") or []
            if not isinstance(values, list):
                msg = f"Dimension '{name}' values must be a list."
                raise TargetProviderError(msg)
            return [{name: v} for v in values]

        if "dataset" in dimension:
            raw_path = Path(str(dimension["dataset"]))
            path = (
                raw_path
                if raw_path.is_absolute()
                else self._config_dir / raw_path
            )
            rows = load_rows_from_file(path)
            limit = dimension.get("limit")
            if isinstance(limit, int):
                rows = rows[:limit]
            column = dimension.get("column")
            if column:
                return [
                    {name: row.get(column)}
                    for row in rows
                    if row.get(column) is not None
                ]
            return rows

        msg = (
            f"Dimension '{name}' must define either 'values' or 'dataset'."
        )
        raise TargetProviderError(msg)

    def _passes_filters(self, target: dict[str, Any]) -> bool:
        for field_name, allowed in self._filters.items():
            allowed_list = allowed if isinstance(allowed, list) else [allowed]
            if target.get(field_name) not in allowed_list:
                return False
        return True

    def provide(self) -> list[dict[str, object]]:
        """Return the filtered, capped cross-product of all dimensions."""
        if not self._dimensions:
            return []
        resolved = [self._resolve_dimension(d) for d in self._dimensions]
        targets: list[dict[str, object]] = []
        for combo in product(*resolved):
            merged: dict[str, object] = {}
            for part in combo:
                merged.update(part)
            if self._passes_filters(merged):
                targets.append(merged)
                if self._limit is not None and len(targets) >= self._limit:
                    break
        return targets
