"""Pure (I/O-free) builders for lean starter schemas used in domain onboarding."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from psweep.extraction import load_schema
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.workspace import CONFIG_ROOT, SCHEMA_FILENAME


class ConfigValidationError(ValueError):
    """Config or schema validation error."""


def _infer_document_type_from_schema(schema_path: Path) -> str:
    schema = load_schema(schema_path)

    extraction = (schema.get("$metadata") or {}).get("extraction") or {}
    document_type = extraction.get("document_type")
    if isinstance(document_type, str) and document_type.strip():
        return document_type.strip()

    return schema_path.stem.replace("_", " ").title()


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


def _find_nested_object_array_field(
    schema_node: Dict[str, Any],
) -> Optional[tuple[str, Dict[str, Any]]]:
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict):
            continue
        if "array" not in _schema_type_options(field_schema):
            continue
        item_schema = _schema_array_items(field_schema)
        if isinstance(item_schema, dict) and "object" in _schema_type_options(
            item_schema
        ):
            return field_name, item_schema
    return None


def _scalar_field_names(schema_node: Dict[str, Any]) -> list[str]:
    scalar_fields: list[str] = []
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict):
            continue
        field_types = _schema_type_options(field_schema)
        if "object" in field_types or "array" in field_types:
            continue
        scalar_fields.append(field_name)
    return scalar_fields


def _pick_fields(
    candidate_fields: list[str], preferred_fields: list[str], limit: int = 2
) -> list[str]:
    selected: list[str] = []
    for field_name in preferred_fields:
        if field_name in candidate_fields and field_name not in selected:
            selected.append(field_name)
        if len(selected) >= limit:
            return selected

    for field_name in candidate_fields:
        if field_name not in selected:
            selected.append(field_name)
        if len(selected) >= limit:
            break

    return selected


def _build_validation_scaffold(
    schema_metadata: SchemaMetadata,
) -> Optional[Dict[str, Any]]:
    schema_root = schema_metadata.schema
    main_data_array = schema_metadata.get_main_data_array()
    main_array_schema = _schema_properties(schema_root).get(main_data_array)
    if not isinstance(main_array_schema, dict):
        return None

    main_item_schema = _schema_array_items(main_array_schema)
    if not isinstance(main_item_schema, dict):
        return None

    dedup_key_fields = schema_metadata.get_deduplication_key_fields()
    quantitative_preferences = [
        "value",
        "rate",
        "amount",
        "capacity",
        "unit",
        "units",
    ]
    nested_array = _find_nested_object_array_field(main_item_schema)
    projection: Optional[Dict[str, Any]] = None
    if nested_array is not None:
        nested_array_name, child_item_schema = nested_array
        parent_fields = [
            field_name
            for field_name in dedup_key_fields
            if field_name in _schema_properties(main_item_schema)
        ]
        child_scalar_fields = _scalar_field_names(child_item_schema)
        child_key_fields = [
            field_name
            for field_name in dedup_key_fields
            if field_name in _schema_properties(child_item_schema)
        ]
        key_fields = parent_fields + child_key_fields
        if not key_fields:
            key_fields = parent_fields + child_scalar_fields[:1]

        quantitative_fields = _pick_fields(
            child_scalar_fields, quantitative_preferences
        )

        projection = {
            "type": "nested_array_items",
            "source_array": main_data_array,
            "nested_array": nested_array_name,
        }
        if parent_fields:
            projection["parent_fields"] = parent_fields
    else:
        scalar_fields = _scalar_field_names(main_item_schema)
        key_fields = [
            field_name
            for field_name in dedup_key_fields
            if field_name in _schema_properties(main_item_schema)
        ]
        if not key_fields:
            key_fields = scalar_fields[:2]

        quantitative_fields = _pick_fields(
            scalar_fields, quantitative_preferences
        )

    if not key_fields or not quantitative_fields:
        return None

    scaffold: Dict[str, Any] = {
        "comparison_approach": "numeric_only",
        "record_matching": {"key_fields": deepcopy(key_fields)},
        "comparison": {"primary_fields": quantitative_fields},
    }
    if projection is not None:
        scaffold["projection"] = projection
    return scaffold


def _build_compilation_scaffold(
    schema_metadata: SchemaMetadata,
) -> Dict[str, Any]:
    identity_metadata = schema_metadata.metadata.get("identity") or {}
    deduplication_metadata = identity_metadata.get("deduplication") or {}

    compilation: Dict[str, Any] = {
        "deduplication": {
            "key_fields": schema_metadata.get_deduplication_key_fields(),
            "ignore_fields": schema_metadata.get_deduplication_ignore_fields(),
        }
    }

    for field_name in ("strategy", "comparison_mode"):
        if field_name in deduplication_metadata:
            compilation["deduplication"][field_name] = deepcopy(
                deduplication_metadata[field_name]
            )

    return compilation


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
