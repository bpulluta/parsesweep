"""Pure (I/O-free) builders for lean starter schemas used in domain onboarding."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from psweep.workspace import CONFIG_ROOT, SCHEMA_FILENAME


class ConfigValidationError(ValueError):
    """Config or schema validation error."""


def display_cli_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _humanize_domain_name(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title()


def _schema_type_options(schema_node: Dict[str, Any]) -> set[str]:
    type_value = schema_node.get("type")
    if isinstance(type_value, str):
        return {type_value}
    if isinstance(type_value, list):
        return {value for value in type_value if isinstance(value, str)}
    return set()


def _schema_properties(schema_node: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema_node.get("properties")
    return properties if isinstance(properties, dict) else {}


def _schema_array_items(
    schema_node: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    items = schema_node.get("items")
    return items if isinstance(items, dict) else None


def _prune_schema_for_starter(node: Any) -> Any:
    if isinstance(node, dict):
        pruned: Dict[str, Any] = {}
        for key, value in node.items():
            if key in {"examples", "enum", "default"}:
                continue
            pruned[key] = _prune_schema_for_starter(value)
        return pruned
    if isinstance(node, list):
        return [_prune_schema_for_starter(value) for value in node]
    return deepcopy(node)


def _main_array_item_schema(schema_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = schema_data.get("$metadata") or {}
    extraction = metadata.get("extraction") or {}
    main_data_array = extraction.get("main_data_array")
    if not isinstance(main_data_array, str) or not main_data_array:
        raise ConfigValidationError(
            "Reference schema is missing $metadata.extraction.main_data_array"
        )

    main_array_schema = _schema_properties(schema_data).get(main_data_array)
    if not isinstance(main_array_schema, dict):
        raise ConfigValidationError(
            f"Reference schema is missing properties.{main_data_array}"
        )

    item_schema = _schema_array_items(main_array_schema)
    if not isinstance(
        item_schema, dict
    ) or "object" not in _schema_type_options(item_schema):
        raise ConfigValidationError(
            f"Reference schema properties.{main_data_array}.items must be an object schema"
        )
    return item_schema


def available_main_array_fields(schema_data: Dict[str, Any]) -> list[str]:
    return list(
        _schema_properties(_main_array_item_schema(schema_data)).keys()
    )


def apply_main_array_field_selection(
    schema_data: Dict[str, Any],
    selected_fields: list[str],
) -> Dict[str, Any]:
    item_schema = _main_array_item_schema(schema_data)
    item_properties = _schema_properties(item_schema)
    if not selected_fields:
        raise ConfigValidationError(
            "Starter schema field selection cannot be empty"
        )

    unknown_fields = [
        field_name
        for field_name in selected_fields
        if field_name not in item_properties
    ]
    if unknown_fields:
        raise ConfigValidationError(
            "Unknown starter fields requested: " + ", ".join(unknown_fields)
        )

    item_schema["properties"] = {
        field_name: deepcopy(item_properties[field_name])
        for field_name in selected_fields
    }

    required_fields = item_schema.get("required")
    if isinstance(required_fields, list):
        filtered_required = [
            field_name
            for field_name in required_fields
            if field_name in selected_fields
        ]
        if filtered_required:
            item_schema["required"] = filtered_required
        else:
            item_schema.pop("required", None)

    metadata = schema_data.get("$metadata") or {}
    identity = metadata.get("identity") or {}
    deduplication = identity.get("deduplication") or {}
    if deduplication:
        key_fields = [
            field_name
            for field_name in deduplication.get("key_fields", [])
            if field_name in selected_fields
        ]
        ignore_fields = [
            field_name
            for field_name in deduplication.get("ignore_fields", [])
            if field_name in selected_fields
        ]

        if key_fields:
            deduplication["key_fields"] = key_fields
        else:
            deduplication.pop("key_fields", None)

        if ignore_fields:
            deduplication["ignore_fields"] = ignore_fields
        else:
            deduplication.pop("ignore_fields", None)

        if not deduplication:
            identity.pop("deduplication", None)
        if not identity:
            metadata.pop("identity", None)

    return schema_data


def build_schema_starter_from_reference(
    reference_schema_path: Path,
    *,
    schema_name: str,
    domain_name: Optional[str],
    document_type: Optional[str],
    include_fields: Optional[list[str]] = None,
) -> Dict[str, Any]:
    reference_schema = json.loads(
        reference_schema_path.read_text(encoding="utf-8")
    )
    starter_schema = _prune_schema_for_starter(reference_schema)

    reference_metadata = reference_schema.get("$metadata") or {}
    reference_extraction = reference_metadata.get("extraction") or {}
    reference_identity = reference_metadata.get("identity") or {}
    reference_deduplication = reference_identity.get("deduplication") or {}
    inferred_label = _humanize_domain_name(schema_name)

    resolved_document_type = (
        document_type
        or inferred_label
        or starter_schema.get("title")
        or inferred_label
    )
    resolved_domain_name = (
        domain_name or inferred_label or resolved_document_type
    )

    starter_metadata: Dict[str, Any] = {
        "domain": resolved_domain_name,
        "version": "0.1.0",
        "description": f"Lean starter schema for {resolved_document_type}.",
        "extraction": {
            "main_data_array": reference_extraction.get("main_data_array"),
            "context_objects": reference_extraction.get("context_objects", []),
            "display_name_template": reference_extraction.get(
                "display_name_template"
            ),
            "document_type": resolved_document_type,
        },
    }

    deduplication: Dict[str, Any] = {}
    if reference_deduplication.get("key_fields"):
        deduplication["key_fields"] = deepcopy(
            reference_deduplication["key_fields"]
        )
    if reference_deduplication.get("ignore_fields"):
        deduplication["ignore_fields"] = deepcopy(
            reference_deduplication["ignore_fields"]
        )
    if deduplication:
        starter_metadata["identity"] = {"deduplication": deduplication}

    starter_schema["$metadata"] = starter_metadata
    starter_schema["title"] = f"{resolved_document_type} Starter Schema"
    starter_schema["description"] = (
        f"Lean starter schema for {resolved_document_type}. Expand field detail and "
        "runtime presentation only after the first extraction pass."
    )

    if include_fields is not None:
        starter_schema = apply_main_array_field_selection(
            starter_schema, include_fields
        )

    return starter_schema


def write_schema_starter(
    schema_path: Path, schema_data: Dict[str, Any], force: bool
) -> None:
    if schema_path.exists() and not force:
        raise ConfigValidationError(
            f"Schema already exists: {schema_path.as_posix()}"
        )

    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(schema_data, indent=2) + "\n", encoding="utf-8"
    )


def default_schema_output_path(repo_root: Path, schema_name: str) -> Path:
    return repo_root / CONFIG_ROOT / schema_name / SCHEMA_FILENAME


__all__ = [
    "ConfigValidationError",
    "apply_main_array_field_selection",
    "available_main_array_fields",
    "build_schema_starter_from_reference",
    "default_schema_output_path",
    "display_cli_path",
    "write_schema_starter",
]
