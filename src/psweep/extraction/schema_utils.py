"""Schema loading utilities."""

import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from psweep.config.file_loader import load_json_file


def load_schema(schema_path: str | Path) -> dict[str, Any]:
    """
    Load JSON schema from file.

    Parameters
    ----------
    schema_path : Union[str, Path]
        Path to JSON schema file

    Returns
    -------
    Dict[str, Any]
        Parsed JSON schema as dictionary

    Raises
    ------
    ConfigurationError
        If the file is missing or not valid JSON
    """
    schema = load_json_file(schema_path)
    build_schema_validator(schema)
    evidence_paths(schema)
    return schema


def _resolve_ref(schema: dict[str, Any], node: Any) -> Any:
    seen = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in seen:
            return node
        seen.add(ref)
        target: Any = schema
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return node
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def evidence_paths(schema: dict[str, Any]) -> list[tuple[str, ...]]:
    """Return paths of ``{value, quote}`` evidence objects (``*`` = array item).

    An object whose properties are exactly ``value`` and ``quote`` is an
    evidence field; both must be required, ``value`` must accept null, and
    ``quote`` must be a nullable string.
    """
    from jsonschema.validators import validator_for

    from psweep.exceptions import SchemaValidationError

    root = validator_for(schema)(schema)
    paths: list[tuple[str, ...]] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        node = _resolve_ref(schema, node)
        if not isinstance(node, dict) or len(path) > 32:
            return
        properties = node.get("properties")
        if isinstance(properties, dict) and set(properties) == {"value", "quote"}:
            quote = _resolve_ref(schema, properties["quote"])
            types = quote.get("type") if isinstance(quote, dict) else None
            if (
                set(node.get("required", [])) != {"value", "quote"}
                or not isinstance(types, list)
                or set(types) != {"string", "null"}
                or not root.evolve(schema=properties["value"]).is_valid(None)
            ):
                message = (
                    "Evidence object at "
                    f"{'.'.join(path) or '<root>'} must require 'value' and "
                    "'quote', with 'value' accepting null and 'quote' a "
                    "nullable string"
                )
                raise SchemaValidationError(message)
            paths.append(path)
            return
        for key, child in (properties or {}).items():
            walk(child, (*path, key))
        if "items" in node:
            walk(node["items"], (*path, "*"))
        for keyword in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(keyword) or []:
                walk(branch, path)

    walk(schema, ())
    return list(dict.fromkeys(paths))


_STRICT_UNSUPPORTED = frozenset(
    {
        "$ref",
        "allOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "dependentRequired",
        "dependentSchemas",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "format",
        "pattern",
    }
)


def strict_compatible(node: Any) -> bool:
    """True when every object is closed and fully required, without unsupported keywords."""
    if isinstance(node, list):
        return all(strict_compatible(child) for child in node)
    if not isinstance(node, dict):
        return True
    if _STRICT_UNSUPPORTED.intersection(node):
        return False
    types = node.get("type")
    if "object" in (types if isinstance(types, list) else [types]) or "properties" in node:
        properties = node.get("properties")
        if (
            not isinstance(properties, dict)
            or node.get("additionalProperties") is not False
            or set(node.get("required", [])) != set(properties)
        ):
            return False
    return all(strict_compatible(node[key]) for key in ("items", "anyOf") if key in node) and all(
        strict_compatible(child)
        for key in ("properties", "$defs")
        for child in (node.get(key) or {}).values()
    )


def build_schema_validator(schema: dict[str, Any]):
    """Validate a schema definition and return its format-aware validator."""
    from jsonschema import FormatChecker, SchemaError
    from jsonschema.validators import validator_for
    from psweep.exceptions import SchemaValidationError

    validator_class = validator_for(schema)
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        message = f"Invalid extraction schema: {exc.message}"
        raise SchemaValidationError(message) from exc
    return validator_class(schema, format_checker=FormatChecker())


@lru_cache(maxsize=1)
def record_validator():
    """Load the canonical record-envelope validator once per process."""
    path = files("psweep.extraction").joinpath("data/extraction_record.schema.json")
    return build_schema_validator(
        json.loads(path.read_text(encoding="utf-8")),
    )
