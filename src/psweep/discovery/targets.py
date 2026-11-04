"""Discovery target specs: validation and lazy iteration.

A spec is one of: a ``.csv``/``.json`` path, an inline list of rows, or
``{cross_product: {dimensions: [...]}}``. Rows stream from generators so a
large cross product with ``--target-limit`` never materializes.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from difflib import get_close_matches
from itertools import islice, product
from pathlib import Path
from typing import Any

from .urls import is_http_url

TargetSpec = str | list | dict
# Row fields that drive discovery; every other field is target metadata.
TARGET_INPUT_FIELDS = frozenset({"urls", "query"})
_FILE_SUFFIXES = (".csv", ".json")
_VALUES_DIMENSION = {"name", "values"}
_DATASET_DIMENSION = {"name", "dataset", "column"}


class TargetSpecError(ValueError):
    """Raised when a target spec or target row is invalid."""


def target_identity(
    target: Mapping[str, Any], fields: Sequence[str] | None = None
) -> dict[str, Any]:
    """Fields that identify a target: ``partition_by`` when set, else every
    non-input field. Other (passthrough) fields never affect identity."""
    keys = fields or [key for key in target if key not in TARGET_INPUT_FIELDS]
    return {key: target.get(key) for key in keys}


def identify_target(
    target: Mapping[str, Any], fields: Sequence[str] | None = None
) -> str:
    """Stable, path-safe target id hashed from :func:`target_identity`."""
    encoded = json.dumps(target_identity(target, fields), sort_keys=True, default=str)
    return hashlib.md5(encoded.encode()).hexdigest()[:12]


def _normalize_urls(value: Any, where: str, *, from_csv: bool) -> list[str]:
    if from_csv and isinstance(value, str):
        value = [segment.strip() for segment in value.split("|")]
    if not isinstance(value, list):
        expected = "a '|'-separated string" if from_csv else "a list"
        raise TargetSpecError(f"{where}: 'urls' must be {expected}")
    for url in value:
        if not is_http_url(url):
            raise TargetSpecError(
                f"{where}: invalid url {url!r} (expected http(s) URL with a host)"
            )
    return [url.strip() for url in value]


def _normalize_row(row: Any, where: str, *, from_csv: bool) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TargetSpecError(f"{where}: target must be an object")
    normalized = (
        {key: value or None for key, value in row.items()} if from_csv else dict(row)
    )
    if normalized.get("urls") is None:
        normalized.pop("urls", None)
    else:
        normalized["urls"] = _normalize_urls(
            normalized["urls"], where, from_csv=from_csv
        )
    return normalized


def _iter_file(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    if not path.is_file():
        raise TargetSpecError(f"targets file not found: {path.as_posix()}")
    if path.suffix.lower() == ".json":
        try:
            rows = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise TargetSpecError(f"{path.as_posix()}: invalid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise TargetSpecError(f"{path.as_posix()}: must contain a list of objects")
        for index, row in enumerate(rows, start=1):
            where = f"{path.as_posix()}[{index}]"
            yield where, _normalize_row(row, where, from_csv=False)
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise TargetSpecError(f"{path.as_posix()}: CSV has no header")
        for row in reader:
            where = f"{path.as_posix()}:{reader.line_num}"
            if None in row:
                raise TargetSpecError(
                    f"{where}: row has more cells than the "
                    f"{len(reader.fieldnames)}-column header"
                )
            yield where, _normalize_row(row, where, from_csv=True)


def _dimension_rows(
    dimension: dict[str, Any], where: str = "dimension"
) -> list[dict[str, Any]]:
    name = dimension["name"]
    if "values" in dimension:
        return [{name: value} for value in dimension["values"]]
    rows = [row for _, row in _iter_file(Path(dimension["dataset"]))]
    column = dimension.get("column")
    if column is None:
        return rows
    columns = sorted({key for row in rows for key in row})
    if rows and column not in columns:
        close = get_close_matches(str(column), columns, n=1)
        hint = (
            f" — did you mean '{close[0]}'?"
            if close
            else f" (columns: {', '.join(columns)})"
        )
        raise TargetSpecError(
            f"{where}.column: unknown column '{column}' in "
            f"{dimension['dataset']}{hint}"
        )
    return [{name: row.get(column)} for row in rows if row.get(column) is not None]


def _iter_rows(spec: TargetSpec) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(location, row)`` pairs; locations name rows in errors."""
    if isinstance(spec, str):
        yield from _iter_file(Path(spec))
    elif isinstance(spec, list):
        for index, row in enumerate(spec):
            where = f"targets[{index}]"
            yield where, _normalize_row(row, where, from_csv=False)
    else:
        dimensions = [_dimension_rows(d) for d in spec["cross_product"]["dimensions"]]
        for index, combo in enumerate(product(*dimensions)):
            row = {key: value for part in combo for key, value in part.items()}
            yield f"targets.cross_product row {index + 1}", row


def iter_targets(spec: TargetSpec, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield normalized target rows, stopping after ``limit`` rows."""
    return islice((row for _, row in _iter_rows(spec)), limit)


def _resolve_path(value: Any, where: str, config_dir: Path) -> str:
    if not isinstance(value, str) or Path(value).suffix.lower() not in _FILE_SUFFIXES:
        raise TargetSpecError(f"{where} must be a .csv or .json path")
    path = Path(value)
    return (path if path.is_absolute() else config_dir / path).as_posix()


def _check_unique_identities(spec: TargetSpec, fields: Sequence[str] | None) -> None:
    seen: dict[str, str] = {}
    for where, row in _iter_rows(spec):
        first = seen.setdefault(identify_target(row, fields), where)
        if first != where:
            identity = ", ".join(
                f"{key}={value}" for key, value in target_identity(row, fields).items()
            )
            basis = "discovery.partition_by" if fields else "the non-input target fields"
            raise TargetSpecError(
                f"{where}: duplicate target identity ({identity or 'empty'}) "
                f"already used by {first}; {basis} must identify each target"
            )


def resolve_targets_spec(
    spec: Any, config_dir: Path, identity_fields: Sequence[str] | None = None
) -> TargetSpec:
    """Validate a spec and return it with paths resolved against ``config_dir``.

    Rows are streamed once to validate each row and to reject duplicate
    target identities (see :func:`target_identity`).
    """
    if isinstance(spec, list):
        resolved: TargetSpec = spec
    elif isinstance(spec, str):
        resolved = _resolve_path(spec, "targets", config_dir)
    elif isinstance(spec, dict) and set(spec) == {"cross_product"}:
        cross = spec["cross_product"]
        if not isinstance(cross, dict) or set(cross) != {"dimensions"}:
            raise TargetSpecError("targets.cross_product must be {dimensions: [...]}")
        dimensions = cross["dimensions"]
        if not isinstance(dimensions, list) or not dimensions:
            raise TargetSpecError("targets.cross_product.dimensions must be a non-empty list")
        resolved_dims = []
        for index, dim in enumerate(dimensions):
            where = f"targets.cross_product.dimensions[{index}]"
            if not (
                isinstance(dim, dict)
                and "name" in dim
                and ("values" in dim) != ("dataset" in dim)
                and set(dim) <= (_VALUES_DIMENSION if "values" in dim else _DATASET_DIMENSION)
            ):
                raise TargetSpecError(
                    f"{where} must be {{name, values}} or {{name, dataset, column?}}"
                )
            if not isinstance(dim["name"], str) or not dim["name"].strip():
                raise TargetSpecError(f"{where}.name must be a non-empty string")
            dim = dict(dim)
            if "values" in dim:
                if not isinstance(dim["values"], list) or not dim["values"]:
                    raise TargetSpecError(f"{where}.values must be a non-empty list")
            else:
                dim["dataset"] = _resolve_path(dim["dataset"], f"{where}.dataset", config_dir)
            resolved_dims.append(dim)
        resolved = {"cross_product": {"dimensions": resolved_dims}}
        for index, dim in enumerate(resolved_dims):
            _dimension_rows(dim, f"targets.cross_product.dimensions[{index}]")
    else:
        raise TargetSpecError(
            "targets must be a .csv/.json path, a list of objects, "
            "or {cross_product: {dimensions: [...]}}"
        )
    _check_unique_identities(resolved, identity_fields)
    return resolved
