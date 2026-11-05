"""Payload normalizers applied after extraction."""

from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for


def _is_empty(value: Any) -> bool:
    """True for null/blank leaves and for containers whose leaves are all empty."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return all(_is_empty(child) for child in value)
    return value is None or value == ""  # noqa: PLC1901 - 0 and False are not empty


def find_data_arrays(data: dict[str, Any]) -> list[tuple[str, list]]:
    """Return ``(field_name, list_value)`` for every non-empty list-valued
    top-level field, in document (insertion) order.

    This is the single source of truth for "which top-level fields hold the
    extracted item arrays", used by item counting, completeness scoring, and
    record writing so the "find the data arrays" scan is not reimplemented.
    """
    return [(key, value) for key, value in data.items() if isinstance(value, list) and value]


def normalize_with_schema(
    data: dict[str, Any], schema: dict[str, Any], validator=None
) -> dict[str, Any]:
    """Fill absent optional fields with null only when their schema permits it."""
    validator = validator or validator_for(schema)(schema, format_checker=FormatChecker())

    def fill_nullable(value, subschema):
        if not isinstance(subschema, dict):
            return
        if isinstance(value, dict):
            required = subschema.get("required", [])
            for field, field_schema in subschema.get("properties", {}).items():
                if field in value:
                    fill_nullable(value[field], field_schema)
                elif field not in required and validator.evolve(schema=field_schema).is_valid(None):
                    value[field] = None
        elif isinstance(value, list):
            item_schema = subschema.get("items", {})
            for item in value:
                fill_nullable(item, item_schema)

    fill_nullable(data, schema)
    return data


def normalize_string_fields(data: Any) -> Any:
    """Collapse whitespace in every string value, recursively."""
    if isinstance(data, dict):
        return {key: normalize_string_fields(value) for key, value in data.items()}
    if isinstance(data, list):
        return [normalize_string_fields(item) for item in data]
    if isinstance(data, str):
        return " ".join(data.split())
    return data


def calculate_completeness(data: dict[str, Any]) -> float:
    """Heuristic 0-1 fill score: 0.3 base, 0.2 context fields, 0.5 items."""
    score = 0.3
    context = [
        field for value in data.values() if isinstance(value, dict) for field in value.values()
    ]
    if context:
        score += 0.2 * sum(not _is_empty(field) for field in context) / len(context)
    items = [item for _, values in find_data_arrays(data) for item in values]
    if items:
        complete = sum(
            isinstance(item, dict)
            and bool(item)
            and sum(not _is_empty(v) for v in item.values()) / len(item) >= 0.5
            for item in items
        )
        score += 0.5 * complete / len(items)
    return min(score, 1.0)
