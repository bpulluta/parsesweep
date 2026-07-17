"""Compile domain-pack and profile inputs into a runtime artifact."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from streamline_extract.consolidation.data_flattener import DataFlattener
from streamline_extract.utils.exceptions import SchemaMetadataError
from streamline_extract.utils.schema_metadata import SchemaMetadata


class ArtifactCompilerError(ValueError):
    """Raised when pack/profile resolution or compilation fails."""


def _build_readiness_check(
    name: str, status: str, detail: str
) -> Dict[str, str]:
    """Build a stable runtime-readiness check record."""
    return {
        "name": name,
        "status": status,
        "detail": detail,
    }


def _canonical_schema_refs(schema_path: Path, repo_root: Path) -> set[str]:
    """Build comparable schema path references for pack lookup."""
    refs: set[str] = set()

    refs.add(schema_path.as_posix())
    refs.add(schema_path.name)

    absolute_path = (
        schema_path if schema_path.is_absolute() else (repo_root / schema_path)
    )
    refs.add(absolute_path.as_posix())

    try:
        resolved = absolute_path.resolve()
        refs.add(resolved.as_posix())
        refs.add(resolved.relative_to(repo_root.resolve()).as_posix())
    except (FileNotFoundError, ValueError):
        pass

    return refs


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ArtifactCompilerError(
            f"Pack file must contain an object: {path}"
        )
    return loaded


def _load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)

    if not isinstance(loaded, dict):
        raise ArtifactCompilerError(
            f"Profile file must contain an object: {path}"
        )
    return loaded


def _resolve_pack_path(pack_ref: str, domain_packs_dir: Path) -> Path:
    direct = Path(pack_ref)
    if direct.is_file():
        return direct
    if direct.is_dir() and (direct / "pack.yaml").exists():
        return direct / "pack.yaml"

    candidate_file = domain_packs_dir / pack_ref
    candidate_dir = domain_packs_dir / pack_ref / "pack.yaml"
    if candidate_file.is_file():
        return candidate_file
    if candidate_file.is_dir() and (candidate_file / "pack.yaml").exists():
        return candidate_file / "pack.yaml"
    if candidate_dir.exists():
        return candidate_dir

    raise ArtifactCompilerError(
        f"Could not resolve pack '{pack_ref}' in {domain_packs_dir}"
    )


def _resolve_profile_path(profile_ref: str, profiles_dir: Path) -> Path:
    direct = Path(profile_ref)
    if direct.is_file():
        return direct

    raw_candidate = profiles_dir / profile_ref
    named_candidate = profiles_dir / f"{profile_ref}.profile.json"
    if raw_candidate.exists():
        return raw_candidate
    if named_candidate.exists():
        return named_candidate

    raise ArtifactCompilerError(
        f"Could not resolve profile '{profile_ref}' in {profiles_dir}"
    )


def resolve_pack_ref_for_schema(
    schema_path: Path,
    *,
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a domain-pack name by matching the configured schema path."""
    root = repo_root or _project_root()
    packs_root = domain_packs_dir or (root / "schemas/domain_packs")

    if not packs_root.exists():
        return None

    schema_refs = _canonical_schema_refs(schema_path, root)

    for pack_path in sorted(packs_root.glob("*/pack.yaml")):
        try:
            pack_data = _load_yaml_file(pack_path)
        except ArtifactCompilerError:
            continue

        configured_refs = []
        primary_schema = pack_data.get("schema_path")
        if primary_schema:
            configured_refs.append(str(primary_schema))
        aliases = pack_data.get("schema_aliases") or []
        if isinstance(aliases, list):
            configured_refs.extend(str(alias) for alias in aliases)

        if not configured_refs:
            continue

        for configured_ref in configured_refs:
            if configured_ref in schema_refs:
                return str(pack_data.get("name") or pack_path.parent.name)

            configured_path = Path(configured_ref)
            if _canonical_schema_refs(configured_path, root) & schema_refs:
                return str(pack_data.get("name") or pack_path.parent.name)

    return None


def _contract_versions(repo_root: Path) -> Dict[str, str]:
    extraction_path = repo_root / "schemas/core/extraction_record.schema.json"
    modules_path = repo_root / "schemas/core/modules_catalog.schema.json"

    for contract_path in (extraction_path, modules_path):
        if not contract_path.exists():
            raise ArtifactCompilerError(
                "Required core contract not found: "
                f"{contract_path.as_posix()}"
            )

    extraction_schema = _load_json_file(extraction_path)
    modules_schema = _load_json_file(modules_path)

    extraction_version = extraction_schema.get("$metadata", {}).get("version")
    modules_version = modules_schema.get("$metadata", {}).get("version")

    if not extraction_version or not modules_version:
        raise ArtifactCompilerError(
            "Core contracts must include $metadata.version fields"
        )

    return {
        "extraction_record": extraction_version,
        "modules_catalog": modules_version,
    }


def _pack_identity(
    pack_data: Dict[str, Any], fallback: str
) -> Tuple[str, str]:
    pack_name = pack_data.get("name") or pack_data.get("pack_name") or fallback
    pack_version = pack_data.get("version") or pack_data.get("pack_version")

    if not pack_version:
        raise ArtifactCompilerError(
            "Pack definition must include 'version' or 'pack_version'"
        )

    return str(pack_name), str(pack_version)


def _profile_identity(profile_data: Dict[str, Any], fallback: str) -> str:
    profile_name = profile_data.get("profile_id") or profile_data.get("name")
    if not profile_name:
        profile_name = fallback
    return str(profile_name)


def _artifact_id(seed_data: Dict[str, Any]) -> str:
    canonical = json.dumps(seed_data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"artifact://runtime/{digest[:16]}"


def _resolve_schema_file_path(schema_ref: str, repo_root: Path) -> Path:
    schema_path = Path(schema_ref)
    if not schema_path.is_absolute():
        schema_path = repo_root / schema_path
    return schema_path


def _schema_type_options(schema_node: Dict[str, Any]) -> set[str]:
    type_value = schema_node.get("type")
    if isinstance(type_value, str):
        return {type_value}
    if isinstance(type_value, list):
        return {value for value in type_value if isinstance(value, str)}

    options: set[str] = set()
    for branch_name in ("anyOf", "oneOf", "allOf"):
        branches = schema_node.get(branch_name)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, dict):
                options.update(_schema_type_options(branch))
    return options


def _schema_has_type(schema_node: Dict[str, Any], expected_type: str) -> bool:
    return expected_type in _schema_type_options(schema_node)


def _schema_properties(schema_node: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema_node.get("properties")
    if isinstance(properties, dict):
        return properties

    for branch_name in ("anyOf", "oneOf", "allOf"):
        branches = schema_node.get(branch_name)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, dict):
                nested_properties = _schema_properties(branch)
                if nested_properties:
                    return nested_properties
    return {}


def _schema_array_items(
    schema_node: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    items = schema_node.get("items")
    if isinstance(items, dict):
        return items

    for branch_name in ("anyOf", "oneOf", "allOf"):
        branches = schema_node.get(branch_name)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, dict):
                nested_items = _schema_array_items(branch)
                if nested_items is not None:
                    return nested_items
    return None


def _resolve_schema_path_node(
    schema_root: Dict[str, Any], field_path: str
) -> Dict[str, Any]:
    current_node = schema_root

    for segment in field_path.split("."):
        if not segment:
            raise ArtifactCompilerError(
                f"Schema path contains an empty segment for runtime validation: {field_path}"
            )

        properties = _schema_properties(current_node)
        next_node = properties.get(segment)

        if next_node is None and _schema_has_type(current_node, "array"):
            items = _schema_array_items(current_node)
            if items is not None:
                next_node = _schema_properties(items).get(segment)

        if not isinstance(next_node, dict):
            raise ArtifactCompilerError(
                "Schema path could not be resolved for runtime validation: "
                f"{field_path}"
            )

        current_node = next_node

    return current_node


def _validate_qaqc_lane_against_schema(
    lane_name: str,
    lane_config: Dict[str, Any],
    *,
    schema_root: Dict[str, Any],
    main_data_array: str,
) -> None:
    item_array_node = _resolve_schema_path_node(schema_root, main_data_array)
    if not _schema_has_type(item_array_node, "array"):
        raise ArtifactCompilerError(
            "Schema main data array must resolve to an array for QA/QC runtime validation: "
            f"{main_data_array}"
        )

    item_schema = _schema_array_items(item_array_node)
    if not isinstance(item_schema, dict):
        raise ArtifactCompilerError(
            "Schema main data array must declare item schema for QA/QC runtime validation: "
            f"{main_data_array}"
        )

    projection = lane_config.get("projection")
    field_root = item_schema
    projection_parent_fields: set[str] = set()
    projection_child_properties: Dict[str, Any] = _schema_properties(
        item_schema
    )

    if isinstance(projection, dict):
        projection_type = projection.get("type")
        if projection_type != "nested_array_items":
            raise ArtifactCompilerError(
                "Unsupported QA/QC projection type for runtime validation: "
                f"{projection_type}"
            )

        source_array = projection.get("source_array") or main_data_array
        nested_array = projection.get("nested_array")
        if not nested_array:
            raise ArtifactCompilerError(
                f"QA/QC lane '{lane_name}' must define projection.nested_array for runtime validation"
            )

        source_array_node = _resolve_schema_path_node(
            schema_root, str(source_array)
        )
        if not _schema_has_type(source_array_node, "array"):
            raise ArtifactCompilerError(
                f"QA/QC lane '{lane_name}' projection source_array must resolve to an array: {source_array}"
            )

        parent_item_schema = _schema_array_items(source_array_node)
        if not isinstance(parent_item_schema, dict):
            raise ArtifactCompilerError(
                f"QA/QC lane '{lane_name}' projection source_array must declare item schema: {source_array}"
            )

        nested_array_node = _resolve_schema_path_node(
            parent_item_schema, str(nested_array)
        )
        if not _schema_has_type(nested_array_node, "array"):
            raise ArtifactCompilerError(
                f"QA/QC lane '{lane_name}' projection nested_array must resolve to an array: {nested_array}"
            )

        child_item_schema = _schema_array_items(nested_array_node)
        if not isinstance(child_item_schema, dict):
            raise ArtifactCompilerError(
                f"QA/QC lane '{lane_name}' projection nested_array must declare item schema: {nested_array}"
            )

        projection_parent_fields = set(projection.get("parent_fields") or [])
        for parent_field in projection_parent_fields:
            _resolve_schema_path_node(parent_item_schema, parent_field)

        field_root = child_item_schema
        projection_child_properties = _schema_properties(child_item_schema)

    record_matching = lane_config.get("record_matching") or {}
    comparison = lane_config.get("comparison") or {}

    for field_group_name, field_names in (
        (
            "record_matching.key_fields",
            record_matching.get("key_fields") or [],
        ),
        ("comparison.primary_fields", comparison.get("primary_fields") or []),
    ):
        for field_name in field_names:
            if not isinstance(field_name, str) or not field_name:
                raise ArtifactCompilerError(
                    f"QA/QC lane '{lane_name}' contains an invalid field reference in {field_group_name}"
                )

            if field_name in projection_parent_fields:
                continue

            if "." in field_name:
                _resolve_schema_path_node(field_root, field_name)
                continue

            if field_name not in projection_child_properties:
                raise ArtifactCompilerError(
                    f"QA/QC lane '{lane_name}' field '{field_name}' in {field_group_name} could not be resolved against the schema"
                )


def _context_column_name(field_name: str) -> str:
    return DataFlattener().make_column_name(field_name)


def _find_nested_object_array_field(
    schema_node: Dict[str, Any],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict) or not _schema_has_type(
            field_schema, "array"
        ):
            continue
        item_schema = _schema_array_items(field_schema)
        if isinstance(item_schema, dict) and _schema_has_type(
            item_schema, "object"
        ):
            return field_name, item_schema
    return None


def _collect_flattened_columns(
    schema_node: Dict[str, Any],
    *,
    flattener: DataFlattener,
    prefix: Optional[str] = None,
) -> set[str]:
    columns: set[str] = set()
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict):
            continue

        output_name = f"{prefix}_{field_name}" if prefix else field_name
        field_types = _schema_type_options(field_schema)

        if "object" in field_types:
            nested_properties = _schema_properties(field_schema)
            if nested_properties:
                columns.update(
                    _collect_flattened_columns(
                        field_schema,
                        flattener=flattener,
                        prefix=output_name,
                    )
                )
                continue

        columns.add(flattener.make_column_name(output_name))

    return columns


def _validate_consolidation_config_against_schema(
    consolidation_config: Dict[str, Any],
    *,
    schema_metadata: SchemaMetadata,
) -> None:
    flattener = DataFlattener()
    schema_root = schema_metadata.schema
    main_data_array = schema_metadata.get_main_data_array()
    main_array_node = _resolve_schema_path_node(schema_root, main_data_array)
    if not _schema_has_type(main_array_node, "array"):
        raise ArtifactCompilerError(
            "Schema main data array must resolve to an array for consolidation runtime validation: "
            f"{main_data_array}"
        )

    main_item_schema = _schema_array_items(main_array_node)
    if not isinstance(main_item_schema, dict):
        raise ArtifactCompilerError(
            "Schema main data array must declare item schema for consolidation runtime validation: "
            f"{main_data_array}"
        )

    nested_object_array = _find_nested_object_array_field(main_item_schema)
    if nested_object_array is not None:
        _, child_item_schema = nested_object_array
        row_field_nodes = [main_item_schema, child_item_schema]
        row_columns = _collect_flattened_columns(
            main_item_schema, flattener=flattener
        )
        row_columns.update(
            _collect_flattened_columns(child_item_schema, flattener=flattener)
        )
    else:
        row_field_nodes = [main_item_schema]
        row_columns = _collect_flattened_columns(
            main_item_schema, flattener=flattener
        )

    for context_object in schema_metadata.get_context_objects():
        context_node = _resolve_schema_path_node(schema_root, context_object)
        row_columns.update(
            _context_column_name(field_name)
            for field_name in _schema_properties(context_node)
        )

    deduplication = consolidation_config.get("deduplication") or {}
    key_fields = deduplication.get("key_fields") or []
    for key_field in key_fields:
        if not isinstance(key_field, str) or not key_field:
            raise ArtifactCompilerError(
                "Consolidation deduplication key_fields contain an invalid field reference"
            )
        if "." in key_field:
            resolved = False
            for candidate_root in row_field_nodes:
                try:
                    _resolve_schema_path_node(candidate_root, key_field)
                    resolved = True
                    break
                except ArtifactCompilerError:
                    continue
            if not resolved:
                raise ArtifactCompilerError(
                    "Consolidation deduplication field could not be resolved against the schema: "
                    f"{key_field}"
                )
            continue

        if not any(
            key_field in _schema_properties(candidate_root)
            for candidate_root in row_field_nodes
        ):
            raise ArtifactCompilerError(
                "Consolidation deduplication field could not be resolved against the schema: "
                f"{key_field}"
            )

    strategy = deduplication.get("strategy")
    if strategy is not None and str(strategy) not in {
        "latest",
        "earliest",
        "merge",
    }:
        raise ArtifactCompilerError(
            f"Unsupported consolidation deduplication strategy for runtime validation: {strategy}"
        )

    comparison_mode = deduplication.get("comparison_mode")
    if comparison_mode is not None and str(comparison_mode) not in {
        "exact",
        "fuzzy",
    }:
        raise ArtifactCompilerError(
            f"Unsupported consolidation comparison_mode for runtime validation: {comparison_mode}"
        )

    output_config = consolidation_config.get("output") or {}
    default_format = output_config.get("default_format")
    if default_format is not None and str(
        default_format
    ).strip().lower() not in {"excel", "csv", "both", "all"}:
        raise ArtifactCompilerError(
            f"Unsupported consolidation output.default_format for runtime validation: {default_format}"
        )

    exclude_fields = output_config.get("exclude_fields") or []
    for field_name in exclude_fields:
        if field_name not in row_columns:
            raise ArtifactCompilerError(
                "Consolidation output.exclude_fields entry could not be resolved against consolidated columns: "
                f"{field_name}"
            )

    column_renames = output_config.get("column_renames") or {}
    for source_name, target_name in column_renames.items():
        if source_name not in row_columns:
            raise ArtifactCompilerError(
                "Consolidation output.column_renames source could not be resolved against consolidated columns: "
                f"{source_name}"
            )
        if not isinstance(target_name, str) or not target_name.strip():
            raise ArtifactCompilerError(
                f"Consolidation output.column_renames target must be a non-empty string for source: {source_name}"
            )

    renamed_columns = {
        column_renames.get(column_name, column_name)
        for column_name in row_columns
        if column_name not in exclude_fields
    }
    column_order = output_config.get("column_order") or []
    for ordered_column in column_order:
        if ordered_column not in renamed_columns:
            raise ArtifactCompilerError(
                "Consolidation output.column_order entry could not be resolved after renames/exclusions: "
                f"{ordered_column}"
            )

    freeze_columns = output_config.get("freeze_columns")
    if freeze_columns is not None and (
        not isinstance(freeze_columns, int) or freeze_columns < 0
    ):
        raise ArtifactCompilerError(
            f"Consolidation output.freeze_columns must be a non-negative integer for runtime validation: {freeze_columns}"
        )

    auto_width = output_config.get("auto_width")
    if auto_width is not None and not isinstance(auto_width, bool):
        raise ArtifactCompilerError(
            f"Consolidation output.auto_width must be a boolean for runtime validation: {auto_width}"
        )


def compile_runtime_artifact(
    pack_ref: str,
    profile_ref: str,
    *,
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
    compiled_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resolve a pack and profile and compile a deterministic runtime artifact."""
    root = repo_root or _project_root()
    packs_root = domain_packs_dir or (root / "schemas/domain_packs")
    profiles_root = profiles_dir or (root / "schemas/profiles")

    pack_path = _resolve_pack_path(pack_ref, packs_root)
    profile_path = _resolve_profile_path(profile_ref, profiles_root)

    pack_data = _load_yaml_file(pack_path)
    profile_data = _load_json_file(profile_path)
    contract_versions = _contract_versions(root)

    pack_name, pack_version = _pack_identity(
        pack_data, fallback=pack_path.stem
    )
    profile_name = _profile_identity(profile_data, fallback=profile_path.stem)

    identity_seed = {
        "pack_name": pack_name,
        "pack_version": pack_version,
        "profile_name": profile_name,
        "pack": pack_data,
        "profile": profile_data,
        "contract_versions": contract_versions,
    }
    artifact_id = _artifact_id(identity_seed)

    timestamp = compiled_at or datetime.now(timezone.utc)
    compiled_at_iso = timestamp.isoformat().replace("+00:00", "Z")

    return {
        "artifact_id": artifact_id,
        "pack_name": pack_name,
        "pack_version": pack_version,
        "profile_name": profile_name,
        "compiled_at": compiled_at_iso,
        "contract_versions": contract_versions,
        "lineage": {
            "artifact_id": artifact_id,
            "profile_id": profile_name,
            "pack_name": pack_name,
            "pack_version": pack_version,
            "compiled_at": compiled_at_iso,
        },
        "source": {
            "pack_path": pack_path.as_posix(),
            "profile_path": profile_path.as_posix(),
        },
        "resolved": {
            "pack": pack_data,
            "profile": profile_data,
        },
    }


def build_runtime_readiness_report(
    *,
    schema_path: Optional[Path] = None,
    pack_ref: Optional[str] = None,
    profile_ref: str = "default",
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compile and validate the runtime artifact seam for onboarding workflows."""
    if (schema_path is None) == (pack_ref is None):
        raise ArtifactCompilerError(
            "Specify exactly one of 'schema_path' or 'pack_ref' for runtime validation"
        )

    root = repo_root or _project_root()
    normalized_schema_path = (
        Path(schema_path) if schema_path is not None else None
    )
    resolved_pack_ref = pack_ref

    if normalized_schema_path is not None:
        resolved_pack_ref = resolve_pack_ref_for_schema(
            normalized_schema_path,
            repo_root=root,
            domain_packs_dir=domain_packs_dir,
        )
        if not resolved_pack_ref:
            raise ArtifactCompilerError(
                f"Could not resolve pack for schema '{normalized_schema_path.as_posix()}'"
            )

    artifact = compile_runtime_artifact(
        str(resolved_pack_ref),
        profile_ref,
        repo_root=root,
        domain_packs_dir=domain_packs_dir,
        profiles_dir=profiles_dir,
    )

    pack_data = artifact["resolved"]["pack"]
    profile_data = artifact["resolved"]["profile"]
    modules = pack_data.get("modules")
    schema_ref = pack_data.get("schema_path")
    runtime_config = profile_data.get("runtime")
    resolved_schema_file = None
    enabled_module_ids = (
        [
            str(
                module.get("module_id")
                or module.get("name")
                or "unknown-module"
            )
            for module in modules
            if isinstance(module, dict) and module.get("enabled", True)
        ]
        if isinstance(modules, list)
        else []
    )

    checks = []

    if not schema_ref:
        raise ArtifactCompilerError(
            "Pack definition must include 'schema_path' for runtime validation"
        )
    resolved_schema_file = _resolve_schema_file_path(str(schema_ref), root)
    if not resolved_schema_file.is_file():
        raise ArtifactCompilerError(
            "Pack schema_path must resolve to an existing schema file for runtime validation: "
            f"{resolved_schema_file.as_posix()}"
        )
    checks.append(
        _build_readiness_check(
            "schema_path",
            "pass",
            f"Pack resolves schema path '{schema_ref}'",
        )
    )
    checks.append(
        _build_readiness_check(
            "schema_file",
            "pass",
            f"Resolved schema file '{resolved_schema_file.as_posix()}'",
        )
    )

    try:
        schema_metadata = SchemaMetadata(resolved_schema_file)
    except SchemaMetadataError as exc:
        raise ArtifactCompilerError(
            f"Schema file failed runtime metadata validation: {exc}"
        ) from exc

    schema_properties = schema_metadata.schema.get("properties", {})
    main_data_array = schema_metadata.get_main_data_array()
    identifier_fields = schema_metadata.get_identifier_fields()
    context_objects = schema_metadata.get_context_objects()

    if main_data_array not in schema_properties:
        raise ArtifactCompilerError(
            "Schema extraction.main_data_array must reference a top-level schema property "
            f"for runtime validation: {main_data_array}"
        )

    checks.append(
        _build_readiness_check(
            "schema_metadata",
            "pass",
            "Schema includes required $metadata extraction contract",
        )
    )
    checks.append(
        _build_readiness_check(
            "main_data_array",
            "pass",
            f"Schema main data array is '{main_data_array}'",
        )
    )
    checks.append(
        _build_readiness_check(
            "identifier_fields",
            "pass",
            f"Schema defines {len(identifier_fields)} identifier field(s)",
        )
    )
    checks.append(
        _build_readiness_check(
            "context_objects",
            "pass",
            f"Schema defines {len(context_objects)} context object(s)",
        )
    )

    for context_object in context_objects:
        context_node = _resolve_schema_path_node(
            schema_metadata.schema, context_object
        )
        if not _schema_has_type(context_node, "object"):
            raise ArtifactCompilerError(
                "Schema context object must resolve to an object node for runtime validation: "
                f"{context_object}"
            )

    checks.append(
        _build_readiness_check(
            "context_object_paths",
            "pass",
            f"Validated {len(context_objects)} context object path(s) against the schema",
        )
    )

    for identifier_field in identifier_fields:
        _resolve_schema_path_node(schema_metadata.schema, identifier_field)

    checks.append(
        _build_readiness_check(
            "identifier_field_paths",
            "pass",
            f"Validated {len(identifier_fields)} identifier field path(s) against the schema",
        )
    )

    consolidation_config = (
        pack_data.get("consolidation")
        if isinstance(pack_data.get("consolidation"), dict)
        else None
    )
    if consolidation_config:
        _validate_consolidation_config_against_schema(
            consolidation_config,
            schema_metadata=schema_metadata,
        )
        checks.append(
            _build_readiness_check(
                "consolidation_paths",
                "pass",
                "Validated consolidation deduplication and output references against the schema",
            )
        )
    else:
        checks.append(
            _build_readiness_check(
                "consolidation_paths",
                "warn",
                "Pack does not define consolidation overrides; schema metadata remains authoritative",
            )
        )

    if not isinstance(modules, list) or not modules:
        raise ArtifactCompilerError(
            "Pack definition must include a non-empty 'modules' list for runtime validation"
        )
    if not enabled_module_ids:
        raise ArtifactCompilerError(
            "Pack definition must include at least one enabled module for runtime validation"
        )
    checks.append(
        _build_readiness_check(
            "modules",
            "pass",
            f"Resolved {len(enabled_module_ids)} enabled module(s)",
        )
    )

    if not isinstance(runtime_config, dict) or not runtime_config:
        raise ArtifactCompilerError(
            "Profile definition must include a non-empty 'runtime' object for runtime validation"
        )
    checks.append(
        _build_readiness_check(
            "profile_runtime",
            "pass",
            f"Profile runtime exposes {len(runtime_config)} setting(s)",
        )
    )

    contract_versions = artifact.get("contract_versions") or {}
    if not contract_versions.get(
        "extraction_record"
    ) or not contract_versions.get("modules_catalog"):
        raise ArtifactCompilerError(
            "Compiled runtime artifact is missing required core contract versions"
        )
    checks.append(
        _build_readiness_check(
            "contract_versions",
            "pass",
            "Runtime artifact includes extraction-record and modules-catalog versions",
        )
    )

    qaqc_lanes = (
        (((pack_data.get("qaqc") or {}).get("lanes")) or {})
        if isinstance(pack_data.get("qaqc"), dict)
        else {}
    )
    default_lane = (
        ((pack_data.get("qaqc") or {}).get("default_lane"))
        if isinstance(pack_data.get("qaqc"), dict)
        else None
    )
    if qaqc_lanes:
        if default_lane and default_lane not in qaqc_lanes:
            raise ArtifactCompilerError(
                f"Pack QA/QC default_lane '{default_lane}' does not exist in lane definitions"
            )

        for lane_name, lane_config in qaqc_lanes.items():
            if not isinstance(lane_config, dict):
                raise ArtifactCompilerError(
                    f"Pack QA/QC lane '{lane_name}' must be an object for runtime validation"
                )
            _validate_qaqc_lane_against_schema(
                str(lane_name),
                lane_config,
                schema_root=schema_metadata.schema,
                main_data_array=main_data_array,
            )

        checks.append(
            _build_readiness_check(
                "qaqc",
                "pass",
                f"Pack exposes {len(qaqc_lanes)} QA/QC lane(s); default lane is '{default_lane}'",
            )
        )
        checks.append(
            _build_readiness_check(
                "qaqc_lane_paths",
                "pass",
                f"Validated QA/QC lane field references for {len(qaqc_lanes)} lane(s)",
            )
        )
    else:
        checks.append(
            _build_readiness_check(
                "qaqc",
                "warn",
                "Pack does not define QA/QC lanes; runtime validation remains ready",
            )
        )

    return {
        "status": "ready",
        "target": {
            "schema_path": normalized_schema_path.as_posix()
            if normalized_schema_path is not None
            else None,
            "pack_ref": pack_ref,
            "profile_ref": profile_ref,
        },
        "resolved": {
            "pack_ref": str(resolved_pack_ref),
            "pack_name": artifact["pack_name"],
            "pack_version": artifact["pack_version"],
            "profile_name": artifact["profile_name"],
            "artifact_id": artifact["artifact_id"],
            "pack_path": artifact["source"]["pack_path"],
            "profile_path": artifact["source"]["profile_path"],
            "schema_path": str(schema_ref),
            "schema_file_path": resolved_schema_file.as_posix(),
            "main_data_array": main_data_array,
            "identifier_fields": identifier_fields,
            "context_objects": context_objects,
            "enabled_module_ids": enabled_module_ids,
            "default_qaqc_lane": default_lane,
        },
        "contract_versions": contract_versions,
        "checks": checks,
    }
