"""
Utility CLI commands for ParseSweep.

This module contains helper and setup commands:
- init: Interactive project setup wizard
- preview: Preview document before extraction
- estimate: Estimate cost and time for batch extraction
- check-schema: Check JSON schema files
- check-runtime: Check runtime onboarding readiness
- config: Show current configuration

For core workflow commands (extract, compile), see commands.py
"""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import click
from dotenv import load_dotenv
import yaml

from psweep.config import VARIABLE_CATALOG
from psweep.utils.config import get_config
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.extraction import load_schema
from psweep.extraction.document_utils import (
    SUPPORTED_EXTENSIONS,
    extract_text_from_document,
    is_supported_document,
)
from psweep.extraction.llm_factory import DEFAULT_MODEL
from psweep.cli.ui import (
    ask_choice,
    ask_confirm,
    ask_text,
    console,
    create_file_tree,
    key_values,
    print_cost_estimate,
    print_error,
    print_header,
    print_info,
    print_next_steps,
    print_success,
    print_warning,
    rule,
    section,
    status_item,
)
from psweep.utils.model_pricing import get_model_pricing
from psweep.utils.page_range import load_pages_csv


class ConfigValidationError(ValueError):
    """Config or schema validation error."""


def _cli_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _infer_document_type_from_schema(schema_path: Path) -> str:
    schema = load_schema(schema_path)

    extraction = (schema.get("$metadata") or {}).get("extraction") or {}
    document_type = extraction.get("document_type")
    if isinstance(document_type, str) and document_type.strip():
        return document_type.strip()

    return schema_path.stem.replace("_", " ").title()


def _display_cli_path(path: Path, repo_root: Path) -> str:
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


def _available_main_array_fields(schema_data: Dict[str, Any]) -> list[str]:
    return list(
        _schema_properties(_main_array_item_schema(schema_data)).keys()
    )


def _apply_main_array_field_selection(
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


def _build_schema_starter_from_reference(
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
            "identifier_fields": reference_extraction.get(
                "identifier_fields", []
            ),
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
        starter_schema = _apply_main_array_field_selection(
            starter_schema, include_fields
        )

    return starter_schema


def _write_schema_starter(
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


def _default_schema_output_path(repo_root: Path, schema_name: str) -> Path:
    return repo_root / "schemas" / "personal" / f"{schema_name}_schema.json"


@click.command("init-domain-schema")
@click.option(
    "--interactive",
    is_flag=True,
    help="Prompt for missing starter-schema values",
)
@click.option(
    "--name", "schema_name", help="Base schema name (used for the output file)"
)
@click.option(
    "--reference-schema",
    type=click.Path(exists=True),
    help="Existing schema file used as the closest reference for a lean starter",
)
@click.option(
    "--domain", "domain_name", help="Override the metadata domain label"
)
@click.option(
    "--document-type", help="Override the human-readable document type"
)
@click.option(
    "--include-field",
    "include_fields",
    multiple=True,
    help="Limit the starter schema to these main-array item fields; repeat the option to keep multiple fields",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    help="Output schema path (defaults to schemas/personal/<name>_schema.json)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing schema file if it already exists",
)
@click.option(
    "--report-format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format for scaffold results",
)
def init_domain_schema_cmd(
    interactive: bool,
    schema_name: Optional[str],
    reference_schema: Optional[str],
    domain_name: Optional[str],
    document_type: Optional[str],
    include_fields: tuple[str, ...],
    output: Optional[str],
    force: bool,
    report_format: str,
):
    """
    Create a lean starter schema for a new domain.

    This command is the documents-first entry point for greenfield onboarding.
    It derives a compact starter schema from the closest existing reference schema,
    preserving only the minimal extraction contract and core row shape needed for
    the first smoke extraction pass.
    """
    repo_root = _cli_repo_root()

    try:
        if interactive and report_format == "json":
            raise ConfigValidationError(
                "--interactive only supports text output; omit --report-format json"
            )

        if interactive:
            section("Interactive Domain Schema Setup")
            console.print(
                "Answer the prompts to create a lean starter schema.\n"
            )

        if interactive and not schema_name:
            schema_name = ask_text("Schema name")
        if interactive and not reference_schema:
            reference_schema = ask_text("Reference schema path")
        if interactive and document_type is None:
            if ask_confirm(
                "Override the reference document type?", default=False
            ):
                document_type = ask_text("Document type")
        if interactive and domain_name is None:
            if ask_confirm(
                "Override the reference domain label?", default=False
            ):
                domain_name = ask_text("Domain label")

        if not schema_name:
            raise ConfigValidationError(
                "init-domain-schema requires --name unless --interactive is used"
            )
        if not reference_schema:
            raise ConfigValidationError(
                "init-domain-schema requires --reference-schema unless --interactive is used"
            )

        output_path = (
            Path(output).resolve()
            if output
            else _default_schema_output_path(repo_root, schema_name)
        )
        reference_schema_path = Path(reference_schema).resolve()
        selected_fields = list(include_fields)

        schema_data = _build_schema_starter_from_reference(
            reference_schema_path,
            schema_name=schema_name,
            domain_name=domain_name,
            document_type=document_type,
            include_fields=selected_fields or None,
        )
        _write_schema_starter(output_path, schema_data, force=force)

        main_item_fields = _available_main_array_fields(schema_data)
        result = {
            "status": "ready",
            "created": {
                "schema_name": schema_name,
                "schema_path": output_path.as_posix(),
                "reference_schema": reference_schema_path.as_posix(),
                "domain": schema_data["$metadata"]["domain"],
                "document_type": schema_data["$metadata"]["extraction"][
                    "document_type"
                ],
                "main_data_array": schema_data["$metadata"]["extraction"][
                    "main_data_array"
                ],
                "identifier_fields": schema_data["$metadata"]["extraction"][
                    "identifier_fields"
                ],
                "selected_fields": main_item_fields,
                "interactive": interactive,
                "force": force,
            },
            "next_steps": [
                {
                    "title": "Validate starter schema",
                    "command": f"pixi run psweep check-schema {_display_cli_path(output_path, repo_root)}",
                },
                {
                    "title": "Create runtime config from template",
                    "command": (
                        f"mkdir -p config/{schema_name.replace('_schema', '')} && "
                        f"cp config/TEMPLATE.yaml config/{schema_name.replace('_schema', '')}/run.yaml"
                    ),
                },
            ],
        }

        if report_format == "json":
            click.echo(json.dumps(result, indent=2))
            return

        print_header("Init Domain Schema")
        print_success("Lean starter schema created")
        console.print(
            key_values(
                {
                    "Schema Name": schema_name,
                    "Schema Path": output_path.as_posix(),
                    "Reference Schema": reference_schema_path.as_posix(),
                    "Domain": schema_data["$metadata"]["domain"],
                    "Document Type": schema_data["$metadata"]["extraction"][
                        "document_type"
                    ],
                    "Main Data Array": schema_data["$metadata"]["extraction"][
                        "main_data_array"
                    ],
                    "Selected Fields": ", ".join(main_item_fields),
                }
            )
        )
        print_next_steps(
            [
                f'{step["title"]}\n     {step["command"]}'
                for step in result["next_steps"]
            ]
        )
        console.print()
    except ConfigValidationError as exc:
        error_report = {
            "status": "error",
            "error": {
                "category": "domain_schema_init",
                "message": str(exc),
            },
            "target": {
                "schema_name": schema_name,
                "reference_schema": reference_schema,
                "output": output,
            },
        }
        if report_format == "json":
            click.echo(json.dumps(error_report, indent=2))
        else:
            print_header("Init Domain Schema")
            print_error("Schema scaffold failed", str(exc))
        raise click.exceptions.Exit(1)


def _workspace_category_name(pack_name: str) -> str:
    return pack_name


def _create_workspace_skeleton(
    repo_root: Path, category_name: str
) -> list[str]:
    created_paths: list[str] = []
    for root_name in ("documents", "extracted", "compiled"):
        path = repo_root / root_name / category_name
        path.mkdir(parents=True, exist_ok=True)
        created_paths.append(path.as_posix())
    return created_paths


def _sample_assets_readme_content(
    category_name: str,
    *,
    document_type: str,
    schema_ref: str,
    page_ranges_ref: Optional[str],
    template_mode: str,
) -> str:
    supported_extensions = ", ".join(SUPPORTED_EXTENSIONS)
    workflow_lines = [
        "Suggested workflow:",
        "1. Replace the placeholder entry in sample_manifest.csv with a real source file name",
        f"2. Add source documents under documents/{category_name}/",
    ]

    if page_ranges_ref:
        workflow_lines.append(
            f"3. Update {page_ranges_ref} if extraction should target a subset of pages"
        )
        workflow_lines.append(
            "4. Run: "
            + _build_scaffold_extract_command(
                documents_ref=f"documents/{category_name}",
                schema_ref=schema_ref,
                page_ranges_ref=page_ranges_ref,
                document_type=document_type,
                template_mode=template_mode,
            )
        )
    else:
        workflow_lines.append(
            "3. Run: "
            + _build_scaffold_extract_command(
                documents_ref=f"documents/{category_name}",
                schema_ref=schema_ref,
                document_type=document_type,
                template_mode=template_mode,
            )
        )

    return (
        f"# {category_name} Source Documents\n\n"
        f"Document Type: {document_type}\n\n"
        "This directory stores raw source documents for the domain onboarding scaffold.\n\n"
        f"Supported extensions: {supported_extensions}\n\n"
        "Files:\n"
        "- sample_manifest.csv: Replace the placeholder row with a real source file name before the first run\n\n"
        + "\n".join(workflow_lines)
        + "\n"
    )


def _sample_manifest_csv_content(document_type: str) -> str:
    return (
        "file_name,notes\n"
        f"{_suggest_page_ranges_filename(document_type)},Replace with a real source document before extraction\n"
    )


def _create_sample_asset_skeleton(
    repo_root: Path,
    category_name: str,
    *,
    document_type: str,
    schema_ref: str,
    page_ranges_ref: Optional[str],
    template_mode: str,
    force: bool,
) -> list[str]:
    documents_root = repo_root / "documents" / category_name
    readme_path = documents_root / "README.md"
    manifest_path = documents_root / "sample_manifest.csv"

    _write_text_file(
        readme_path,
        _sample_assets_readme_content(
            category_name,
            document_type=document_type,
            schema_ref=schema_ref,
            page_ranges_ref=page_ranges_ref,
            template_mode=template_mode,
        ),
        force=force,
    )
    _write_text_file(
        manifest_path, _sample_manifest_csv_content(document_type), force=force
    )

    return [
        documents_root.as_posix(),
        readme_path.as_posix(),
        manifest_path.as_posix(),
    ]


def _suggest_page_ranges_filename(document_type: str) -> str:
    """Suggest a sample filename based on document type (domain-agnostic)."""
    normalized = document_type.strip().lower()
    # Build a slug from the document type description
    slug = "_".join(normalized.split()[:3])
    if "html" in normalized or "filing" in normalized:
        return f"{slug}.html"
    return f"{slug}.pdf"


def _recommended_extract_flags(document_type: str) -> list[str]:
    """Return suggested CLI flags based on document type (domain-agnostic)."""
    # No domain-specific defaults — users configure max_context in run.yaml
    return []


def _build_scaffold_extract_command(
    *,
    documents_ref: str,
    schema_ref: str,
    profile_ref: Optional[str] = None,
    page_ranges_ref: Optional[str] = None,
    document_type: str,
    template_mode: str = "recommended",
) -> str:
    command_parts = [
        "pixi run psweep extract",
        f"{documents_ref}/",
        f"--schema {schema_ref}",
    ]
    if profile_ref:
        command_parts.append(f"--profile {profile_ref}")
    if template_mode == "recommended":
        command_parts.extend(_recommended_extract_flags(document_type))
    if page_ranges_ref:
        command_parts.append(f"--pages-csv {page_ranges_ref}")
    return " ".join(command_parts)


def _config_readme_content(
    category_name: str,
    *,
    document_type: str,
    domain_name: str,
    schema_ref: str,
    page_ranges_ref: str,
    run_config_ref: str,
    has_validation: bool,
    template_mode: str,
) -> str:
    workflow_lines = [
        "Suggested workflow:",
        f"1. Add source documents under documents/{category_name}/",
        "2. Update page_ranges.csv if extraction should target a subset of pages",
        (
            '3. Run: '
            f'{_build_scaffold_extract_command(documents_ref=f"documents/{category_name}", schema_ref=schema_ref, page_ranges_ref=page_ranges_ref, document_type=document_type, template_mode=template_mode)}'
        ),
        (
            f"4. Run: pixi run psweep compile extracted/{category_name} "
            f"--schema {schema_ref}"
        ),
        (
            f"5. Optional config-based workflow: pixi run psweep extract --config {run_config_ref} "
            f"--pages-csv {page_ranges_ref}"
        ),
        (
            f"6. Optional config-based compilation: pixi run psweep compile --config {run_config_ref}"
        ),
    ]

    recommended_flags = _recommended_extract_flags(document_type)
    if template_mode == "recommended" and recommended_flags:
        workflow_lines.extend(
            [
                "",
                "Recommended flags:",
                "- --max-context 1400000: Use for large tariff books so long schedule sections are less likely to be truncated.",
            ]
        )

    if template_mode == "recommended" and has_validation:
        workflow_lines.extend(
            [
                "",
                "Optional QA/QC workflow:",
                (
                    f"1. Run: pixi run psweep validate --config {run_config_ref}"
                ),
                (
                    f"2. Rebuild reports later without re-extraction: "
                    f"pixi run psweep validate --config {run_config_ref} --compare-only"
                ),
                (
                    "3. Tune validation.record_matching and validation.judge settings, then rerun --compare-only"
                ),
            ]
        )

    return (
        f"# {category_name} Configuration\n\n"
        f"Document Type: {document_type}\n\n"
        f"Domain: {domain_name}\n\n"
        "This directory contains configuration files for the domain onboarding scaffold.\n\n"
        "Files:\n"
        "- page_ranges.csv: Optional page-range overrides for document extraction\n"
        "- run.yaml: Starter runtime config for discover/extract/compile commands\n\n"
        + "\n".join(workflow_lines)
        + "\n"
    )


def _config_run_yaml_content(
    category_name: str,
    *,
    schema_ref: str,
) -> str:
    run_config = {
        "domain": category_name,
        "extraction": {
            "input_dir": f"documents/{category_name}",
            "schema": schema_ref,
            "output_dir": f"extracted/{category_name}",
        },
        "compilation": {
            "input_dir": f"extracted/{category_name}",
            "schema": schema_ref,
            "output_dir": f"compiled/{category_name}",
        },
    }
    return yaml.safe_dump(run_config, sort_keys=False, allow_unicode=False)


def _page_ranges_csv_content(
    document_type: str, *, documents_dir: Optional[Path] = None
) -> str:
    rows = ["file_path,start_page,end_page"]

    if documents_dir is not None and documents_dir.exists():
        source_files = sorted(
            path.name
            for path in documents_dir.iterdir()
            if path.is_file() and is_supported_document(path)
        )
        if source_files:
            rows.extend(f"{file_name},," for file_name in source_files)
            return "\n".join(rows) + "\n"

    rows.append(f"{_suggest_page_ranges_filename(document_type)},,")
    return "\n".join(rows) + "\n"


def _write_text_file(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise ConfigValidationError(
            f"Scaffold file already exists: {path.as_posix()}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_config_skeleton(
    repo_root: Path,
    config_root: Path,
    category_name: str,
    *,
    document_type: str,
    domain_name: str,
    schema_ref: str,
    has_validation: bool,
    template_mode: str,
    force: bool,
) -> list[str]:
    category_root = config_root / category_name
    readme_path = category_root / "README.md"
    page_ranges_path = category_root / "page_ranges.csv"
    run_config_path = category_root / "run.yaml"

    _write_text_file(
        readme_path,
        _config_readme_content(
            category_name,
            document_type=document_type,
            domain_name=domain_name,
            schema_ref=schema_ref,
            page_ranges_ref=_display_cli_path(page_ranges_path, repo_root),
            run_config_ref=_display_cli_path(run_config_path, repo_root),
            has_validation=has_validation,
            template_mode=template_mode,
        ),
        force=force,
    )
    _write_text_file(
        page_ranges_path,
        _page_ranges_csv_content(
            document_type,
            documents_dir=repo_root / "documents" / category_name,
        ),
        force=force,
    )
    _write_text_file(
        run_config_path,
        _config_run_yaml_content(
            category_name,
            schema_ref=schema_ref,
        ),
        force=force,
    )

    return [
        category_root.as_posix(),
        readme_path.as_posix(),
        page_ranges_path.as_posix(),
        run_config_path.as_posix(),
    ]


@click.command()
def init():
    """
    Interactive project setup wizard.

    Guides you through setting up your ParseSweep project including:
    - API credentials configuration
    - Document type selection
    - Schema selection
    - Directory structure creation

    \b
    EXAMPLE:
        psweep init
    """
    print_header("ParseSweep Setup Wizard")

    console.print("Let's set up your project!\n")

    # Check if .env already exists
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        if not ask_confirm(
            f".env file already exists at {env_path}. Overwrite?",
            default=False,
        ):
            print_info("Keeping existing .env file")
            return

    # API Provider Selection
    section("Step 1: API Configuration")
    provider = ask_choice(
        "Select API provider", choices=["azure", "openai"], default="azure"
    )

    env_content = []

    if provider == "azure":
        section("Azure OpenAI Configuration")
        api_key = ask_text("Azure OpenAI API Key")
        endpoint = ask_text(
            "Azure OpenAI Endpoint",
            default="https://your-endpoint.openai.azure.com/",
        )
        deployment = ask_text("Model Deployment Name", default=DEFAULT_MODEL)

        env_content = [
            "# Azure OpenAI Configuration",
            f"AZURE_OPENAI_API_KEY={api_key}",
            f"AZURE_OPENAI_ENDPOINT={endpoint}",
            f"AZURE_OPENAI_MODEL={deployment}",
            "AZURE_OPENAI_API_VERSION=2025-04-01-preview",
        ]
    else:
        section("OpenAI Configuration")
        api_key = ask_text("OpenAI API Key (starts with sk-)")

        env_content = [
            "# OpenAI Configuration",
            f"OPENAI_API_KEY={api_key}",
        ]

    # Write .env file
    with open(env_path, "w") as f:
        f.write("\n".join(env_content) + "\n")

    print_success(f"Created .env file at {env_path}")

    # Document Type Selection
    section("Step 2: Document Type")
    doc_type = ask_choice(
        "What type of documents will you process?",
        choices=["tariffs", "ordinances", "permits", "custom"],
        default="tariffs",
    )

    # Create directory structure
    section("Step 3: Directory Structure")

    project_root = Path.cwd()
    docs_dir = project_root / "documents" / doc_type
    processed_dir = project_root / "extracted" / doc_type
    compiled_dir = project_root / "compiled" / doc_type

    docs_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)

    print_success("Created directory structure:")
    console.print(
        create_file_tree(project_root / "documents", "Project Structure")
    )

    # Schema selection
    section("Step 4: Schema Selection")

    config = get_config()
    schemas = list(config.schema_dir.glob("*.json"))

    if schemas:
        schema_names = [s.stem for s in schemas]

        # Try to auto-detect based on doc_type via fuzzy stem matching
        import difflib

        suggested_schema = None
        close = difflib.get_close_matches(doc_type, schema_names, n=1, cutoff=0.3)
        if close:
            suggested_schema = close[0]

        if suggested_schema:
            print_info(f"Suggested schema: {suggested_schema}")

        if len(schema_names) > 1:
            console.print(f"\nAvailable schemas: {', '.join(schema_names)}")

    # Summary
    rule()
    print_success("Setup complete!")
    print_next_steps(
        [
            f"Add your documents to: {docs_dir}",
            f"Run extraction: psweep extract {docs_dir}",
            f"Compile results: psweep compile {processed_dir}",
        ]
    )
    console.print()


@click.command()
@click.argument("document_path", type=click.Path(exists=True))
def preview(document_path: str):
    """
    Preview a document before extraction.

    Shows document information, estimated costs, and sample content
    without performing the full extraction.

    \b
    EXAMPLES:
        psweep preview documents/sample.pdf
        psweep preview documents/tariffs/rate_schedule.docx
    """
    doc_path = Path(document_path)

    # Validate file type
    if not is_supported_document(doc_path):
        supported = ", ".join(SUPPORTED_EXTENSIONS)
        print_error(
            f"Unsupported file type: {doc_path.suffix}",
            f"Supported formats: {supported}",
        )
        return

    print_header(f"Document Preview: {doc_path.name}")

    # Get file info
    file_size = doc_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    section("Document Info")
    info_table = key_values(
        {
            "File": doc_path.name,
            "Size": f"{file_size_mb:.2f} MB",
            "Type": doc_path.suffix.upper(),
        }
    )
    console.print(info_table)

    # Extract text to analyze
    try:
        print_info("Analyzing document...")
        text = extract_text_from_document(doc_path)
        text_length = len(text)

        # Estimate tokens (rough approximation: 4 chars per token)
        estimated_tokens = text_length // 4

        # Estimate cost using the default model's pricing
        input_cost_per_1m, output_cost_per_1m = get_model_pricing(DEFAULT_MODEL)
        input_cost = (estimated_tokens / 1_000_000) * input_cost_per_1m
        output_cost = (estimated_tokens * 0.1 / 1_000_000) * output_cost_per_1m
        total_cost = input_cost + output_cost

        # Estimate time (rough: 100 tokens per second)
        estimated_time = estimated_tokens / 100

        section("Content Analysis")
        analysis_table = key_values(
            {
                "Text Length": f"{text_length:,} characters",
                "Estimated Tokens": f"~{estimated_tokens:,}",
            }
        )
        console.print(analysis_table)

        # Try to detect schema via fuzzy stem matching
        config = get_config()
        matched_schema = None

        candidates = sorted(config.schema_dir.glob("*.json"), key=lambda p: p.stem)
        if candidates:
            import difflib

            stems = [c.stem for c in candidates]
            close = difflib.get_close_matches(
                Path(doc_path).stem, stems, n=1, cutoff=0.3
            )
            if close:
                matched_schema = next(c for c in candidates if c.stem == close[0])
            else:
                matched_schema = candidates[0]

        if matched_schema:
            section("Schema Detection")
            status_item("success", f"Matched schema: {matched_schema.name}")

            # Load and show field count
            schema = load_schema(matched_schema)
            if "properties" in schema:
                field_count = len(schema["properties"])
                status_item("info", f"Expected fields: {field_count}")

        # Cost estimate
        print_cost_estimate(
            total_docs=1,
            estimated_tokens=estimated_tokens,
            estimated_cost=total_cost,
            estimated_time=estimated_time / 60,  # Convert to minutes
            model=DEFAULT_MODEL,
        )
        section("Sample Content Preview")
        sample = text[:500].replace("\n", " ")
        console.print(f"{sample}...\n")

        section("Ready to Extract")
        console.print(
            f"Run: psweep extract {doc_path} --schema schemas/example_utility_rate_schema.json\n"
        )

    except Exception as e:
        print_error("Failed to analyze document", str(e))


@click.command()
@click.argument("documents_path", type=click.Path(exists=True))
@click.option(
    "--workers", "-w", type=int, default=4, help="Number of parallel workers"
)
@click.option(
    "--pages-csv",
    type=click.Path(exists=True),
    default=None,
    help="CSV file mapping documents to page ranges",
)
def estimate(documents_path: str, workers: int, pages_csv: str):
    """
    Estimate cost and time for batch document extraction.

    Analyzes all documents in a directory and provides detailed
    cost and time estimates before you commit to processing.

    \b
    EXAMPLES:
        psweep estimate documents/tariffs/
        psweep estimate documents/permits/ --workers 8
        psweep estimate documents/tariffs/ --pages-csv config/tariffs/page_ranges.csv
    """
    docs_path = Path(documents_path)

    if not docs_path.exists():
        print_error(f"Path not found: {docs_path}")
        return

    print_header("Cost & Time Estimation")

    # Gather all documents
    if docs_path.is_file():
        if is_supported_document(docs_path):
            doc_files = [docs_path]
        else:
            print_error(f"Unsupported file type: {docs_path.suffix}")
            return
    else:
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(docs_path.rglob(f"*{ext}")))

        if not doc_files:
            supported = ", ".join(SUPPORTED_EXTENSIONS)
            print_error(
                f"No supported documents found in {docs_path}",
                f"Supported formats: {supported}",
            )
            return

    print_info(f"Analyzing {len(doc_files)} document(s)...")

    # Load page ranges if provided
    page_range_map = {}
    if pages_csv:
        from psweep.utils.page_range import load_pages_csv

        try:
            page_mappings = load_pages_csv(Path(pages_csv))

            # Match file names from doc_files to mappings
            for doc in doc_files:
                # Try exact match first
                if str(doc) in page_mappings:
                    page_range_map[doc] = page_mappings[str(doc)]
                elif doc.name in page_mappings:
                    page_range_map[doc] = page_mappings[doc.name]

            mapped_count = sum(
                1 for v in page_range_map.values() if v is not None
            )
            print_info(
                f"Loaded page ranges for {mapped_count} file(s) from CSV"
            )
        except Exception as e:
            print_error(f"Failed to load page ranges CSV: {e}")
            return

    # Analyze first few files to estimate average
    sample_size = min(5, len(doc_files))
    total_chars = 0

    for doc in doc_files[:sample_size]:
        try:
            page_range = page_range_map.get(doc) if pages_csv else None
            text = extract_text_from_document(doc, page_range=page_range)
            total_chars += len(text)
        except Exception:
            pass

    if total_chars == 0:
        print_warning("Could not analyze documents for estimation")
        return

    # Calculate averages and estimates
    avg_chars = total_chars / sample_size
    estimated_total_chars = avg_chars * len(doc_files)
    estimated_tokens = int(estimated_total_chars / 4)

    # Get current model pricing from config
    config = get_config()
    model_name = config.llm_config.get("model", DEFAULT_MODEL)

    # Get pricing for the configured model (returns tuple: input_cost, output_cost per 1M tokens)
    input_cost_per_1m, output_cost_per_1m = get_model_pricing(model_name)

    # Cost calculation using model-specific rates
    input_cost = (estimated_tokens / 1_000_000) * input_cost_per_1m
    output_tokens = estimated_tokens * 0.1
    output_cost = (output_tokens / 1_000_000) * output_cost_per_1m
    total_cost = input_cost + output_cost

    # Time calculation (assuming 100 tokens/sec with workers)
    total_seconds = estimated_tokens / (100 * workers)
    estimated_minutes = total_seconds / 60

    # Display analysis
    section("Document Analysis")
    doc_info = {
        "Total Documents": str(len(doc_files)),
        "Sample Analyzed": f"{sample_size} files",
        "Avg Characters/Doc": f"{avg_chars:,.0f}",
        "Estimated Total Tokens": f"~{estimated_tokens:,}",
    }

    # Add page range info if applicable
    if pages_csv and page_range_map:
        files_with_ranges = sum(
            1 for v in page_range_map.values() if v is not None
        )
        if files_with_ranges > 0:
            doc_info["Page Ranges"] = (
                f"{files_with_ranges} file(s) with specific ranges"
            )

    doc_table = key_values(doc_info)
    console.print(doc_table)

    # Display cost breakdown
    section(f"Cost Breakdown ({model_name})")
    cost_table = key_values(
        {
            "Input Tokens": f"~{estimated_tokens:,} @ ${input_cost_per_1m:.2f}/1M",
            "Output Tokens": f"~{int(output_tokens):,} @ ${output_cost_per_1m:.2f}/1M",
            "Total Estimated Cost": f"${total_cost:.2f}",
        }
    )
    console.print(cost_table)

    # Display time estimate
    section("Time Estimate")
    time_table = key_values(
        {
            "Workers": str(workers),
            "Processing Time": f"~{estimated_minutes:.1f} minutes",
        }
    )
    console.print(time_table)

    # Confirmation prompt
    console.print()
    if ask_confirm("Proceed with extraction?", default=False):
        console.print(
            f"\nRun: psweep extract {docs_path} --schema schemas/example_utility_rate_schema.json\n"
        )
    else:
        print_info("Operation cancelled")


@click.command("check-schema")
@click.argument("schema_path", type=click.Path(exists=True))
def check_schema_cmd(schema_path: str):
    """
    Check a JSON schema file.

    Checks schema syntax, structure, and ParseSweep-specific
    metadata requirements.

    \b
    EXAMPLES:
        psweep check-schema schemas/my_schema.json
        psweep check-schema schemas/example_utility_rate_schema.json
    """
    schema_file = Path(schema_path)

    print_header(f"Validating Schema: {schema_file.name}")

    # Load schema
    try:
        with open(schema_file) as f:
            schema = json.load(f)
        print_success("Valid JSON syntax")
    except json.JSONDecodeError as e:
        print_error("Invalid JSON syntax", str(e))
        return

    issues = []
    warnings = []

    # Check JSON Schema structure
    if "$schema" in schema:
        print_success("Has $schema declaration")
    else:
        warnings.append("Missing $schema declaration (recommended)")

    if "type" in schema:
        print_success(f"Schema type: {schema['type']}")
    else:
        issues.append("Missing 'type' field (required)")

    if "properties" in schema:
        field_count = len(schema["properties"])
        print_success(f"Has {field_count} properties defined")
    else:
        issues.append("Missing 'properties' field (required)")

    # Check ParseSweep metadata
    section("ParseSweep Metadata")

    if "$metadata" in schema:
        metadata = schema["$metadata"]
        extraction_metadata = (
            metadata.get("extraction")
            if isinstance(metadata.get("extraction"), dict)
            else {}
        )
        identity_metadata = (
            metadata.get("identity")
            if isinstance(metadata.get("identity"), dict)
            else {}
        )
        compilation_metadata = (
            metadata.get("compilation")
            if isinstance(metadata.get("compilation"), dict)
            else {}
        )
        deduplication_metadata = (
            identity_metadata.get("deduplication")
            if isinstance(identity_metadata.get("deduplication"), dict)
            else {}
        )
        print_success("Has $metadata section")

        if extraction_metadata.get("identifier_fields"):
            id_fields = extraction_metadata["identifier_fields"]
            status_item(
                "info", f"Identifier fields: {', '.join(id_fields)}"
            )
        else:
            warnings.append(
                "$metadata.extraction missing 'identifier_fields' (required for runtime document identification)"
            )

        if extraction_metadata.get("main_data_array"):
            main_array = extraction_metadata["main_data_array"]
            status_item("info", f"Main data array: {main_array}")
        else:
            warnings.append(
                "$metadata.extraction missing 'main_data_array' (required for extraction row generation)"
            )

        if deduplication_metadata:
            key_fields = deduplication_metadata.get("key_fields") or []
            if isinstance(key_fields, list) and key_fields:
                status_item(
                    "info",
                    f"Deduplication key fields: {', '.join(key_fields)}",
                )
            else:
                status_item("info", "Deduplication configured")
        else:
            status_item(
                "info", "No deduplication config (optional in starter schemas)"
            )

        if isinstance(compilation_metadata.get("deduplication"), dict):
            issues.append(
                "$metadata.compilation.deduplication is deprecated; move it to $metadata.identity.deduplication"
            )

        # Enforce a clean ownership boundary to reduce user confusion.
        if isinstance(compilation_metadata.get("output"), dict):
            warnings.append(
                "$metadata.compilation.output is runtime-owned and should be moved to config/<domain>/run.yaml under compilation.output"
            )
        if isinstance(compilation_metadata.get("normalization"), dict):
            warnings.append(
                "$metadata.compilation.normalization is runtime-owned and should be moved to config/<domain>/run.yaml under compilation.normalization"
            )
        if isinstance(compilation_metadata.get("value_typing"), dict):
            warnings.append(
                "$metadata.compilation.value_typing is deprecated; keep vocabularies in schema properties[*].enum"
            )
        if isinstance(metadata.get("validation"), dict):
            warnings.append(
                "$metadata.validation is deprecated in this runtime; use schema-level JSON Schema constraints (required/type/enum) and runtime QA/QC config"
            )
        if isinstance(metadata.get("qa_qc"), dict):
            warnings.append(
                "$metadata.qa_qc is deprecated for active workflows; define QA/QC settings in config/<domain>/run.yaml under 'validation'"
            )
    else:
        issues.append(
            "Missing $metadata section (required for the modernized runtime)"
        )

    # Summary
    rule()

    if issues:
        section("Issues Found")
        for issue in issues:
            status_item("error", issue)
        console.print()
    elif warnings:
        section("Recommendations")
        for warning in warnings:
            status_item("warning", warning)
        console.print()
        print_success("Schema is valid but could be improved!")
    else:
        print_success("Schema is perfect!")

    console.print()


@click.command()
@click.option(
    "--show-runtime-catalog",
    is_flag=True,
    help="Display runtime variable catalog grouped by required/optional/advanced",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format for catalog rendering",
)
def config(show_runtime_catalog: bool, output_format: str):
    """
    Show current configuration.

    Displays environment settings, paths, and available schemas.

    \b
    EXAMPLE:
        psweep config
        psweep config --show-runtime-catalog
    """
    if show_runtime_catalog and output_format == "json":
        click.echo(json.dumps(VARIABLE_CATALOG, indent=2, sort_keys=True))
        return

    load_dotenv()

    print_header("Configuration")

    # API Configuration
    section("API Configuration")

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    openai_key = os.getenv("OPENAI_API_KEY")

    if azure_key and azure_endpoint:
        console.print(
            key_values(
                {
                    "Provider": "Azure OpenAI",
                    "Endpoint": azure_endpoint,
                    "Model": os.getenv("AZURE_OPENAI_MODEL", "Not set"),
                    "API Key": f"{'*' * 20}...{azure_key[-4:]}",
                }
            )
        )
    elif openai_key:
        console.print(
            key_values(
                {
                    "Provider": "OpenAI",
                    "API Key": f"{'*' * 20}...{openai_key[-4:]}",
                }
            )
        )
    else:
        print_warning(
            "No API credentials configured",
            "Run 'psweep init' to set up",
        )

    # Paths
    config_obj = get_config()
    section("Paths")
    console.print(
        key_values(
            {
                "Project Root": str(config_obj.project_root),
                "Schemas": str(config_obj.schema_dir),
            }
        )
    )

    # Available schemas
    schemas = list(config_obj.schema_dir.glob("*.json"))
    if schemas:
        section("Available Schemas")
        for schema in schemas:
            status_item("info", schema.name)

    if show_runtime_catalog:
        section("Runtime Variable Catalog")
        for section_name in ("extraction", "compilation", "discovery"):
            entries = VARIABLE_CATALOG.get(section_name, [])
            if not entries:
                continue

            section(section_name.title())
            grouped = {"required": [], "optional": [], "advanced": []}
            for entry in entries:
                grouped.setdefault(entry.get("level", "optional"), []).append(
                    entry
                )

            for level in ("required", "optional", "advanced"):
                level_entries = grouped.get(level) or []
                if not level_entries:
                    continue
                section(level.title())
                for entry in level_entries:
                    status_item(
                        "info",
                        f"{entry['name']}: {entry['description']}",
                    )

    console.print()
