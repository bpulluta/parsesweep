"""
Utility CLI commands for StreamlineExtract.

This module contains helper and setup commands:
- init: Interactive project setup wizard
- preview: Preview document before processing
- estimate: Estimate cost and time for batch processing
- validate_schema: Validate JSON schema files
- config: Show current configuration

For core workflow commands (process, consolidate), see commands.py
"""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import click
from dotenv import load_dotenv
import yaml

from streamline_extract.core import ArtifactCompilerError, build_runtime_readiness_report
from streamline_extract.utils.config import get_config
from streamline_extract.utils.schema_metadata import SchemaMetadata
from streamline_extract.extraction import load_schema
from streamline_extract.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS
)
from streamline_extract.utils.model_pricing import get_model_pricing
from streamline_extract.cli.ui import (
    console,
    print_header,
    print_error,
    print_success,
    print_info,
    print_warning,
    print_cost_estimate,
    ask_choice,
    ask_confirm,
    ask_text,
    create_file_tree,
    create_config_table,
    display_json,
)


def _cli_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _infer_document_type_from_schema(schema_path: Path) -> str:
    with schema_path.open('r', encoding='utf-8') as handle:
        schema = json.load(handle)

    extraction = ((schema.get('$metadata') or {}).get('extraction') or {})
    document_type = extraction.get('document_type')
    if isinstance(document_type, str) and document_type.strip():
        return document_type.strip()

    return schema_path.stem.replace('_', ' ').title()


def _normalize_pack_schema_path(schema_path: Path, repo_root: Path) -> str:
    try:
        return schema_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return schema_path.resolve().as_posix()


def _display_cli_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _display_cli_reference(reference: str, repo_root: Path) -> str:
    candidate = Path(reference)
    if candidate.exists():
        return _display_cli_path(candidate, repo_root)
    return reference


def _default_pack_modules() -> list[dict[str, Any]]:
    return [
        {
            'module_id': 'value_semantics_classifier',
            'name': 'Value Semantics Classifier',
            'version': '1.0.0',
            'kind': 'classifier',
            'enabled': True,
        },
        {
            'module_id': 'eligibility_policy_engine',
            'name': 'Eligibility Policy Engine',
            'version': '1.0.0',
            'kind': 'policy',
            'enabled': True,
        },
        {
            'module_id': 'lane_projector',
            'name': 'Lane Projector',
            'version': '1.0.0',
            'kind': 'projector',
            'enabled': True,
        },
        {
            'module_id': 'consolidation_mapper',
            'name': 'Consolidation Mapper',
            'version': '1.0.0',
            'kind': 'mapper',
            'enabled': True,
        },
    ]


def _schema_type_options(schema_node: Dict[str, Any]) -> set[str]:
    type_value = schema_node.get('type')
    if isinstance(type_value, str):
        return {type_value}
    if isinstance(type_value, list):
        return {value for value in type_value if isinstance(value, str)}
    return set()


def _schema_properties(schema_node: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema_node.get('properties')
    return properties if isinstance(properties, dict) else {}


def _schema_array_items(schema_node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = schema_node.get('items')
    return items if isinstance(items, dict) else None


def _find_nested_object_array_field(schema_node: Dict[str, Any]) -> Optional[tuple[str, Dict[str, Any]]]:
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict):
            continue
        if 'array' not in _schema_type_options(field_schema):
            continue
        item_schema = _schema_array_items(field_schema)
        if isinstance(item_schema, dict) and 'object' in _schema_type_options(item_schema):
            return field_name, item_schema
    return None


def _scalar_field_names(schema_node: Dict[str, Any]) -> list[str]:
    scalar_fields: list[str] = []
    for field_name, field_schema in _schema_properties(schema_node).items():
        if not isinstance(field_schema, dict):
            continue
        field_types = _schema_type_options(field_schema)
        if 'object' in field_types or 'array' in field_types:
            continue
        scalar_fields.append(field_name)
    return scalar_fields


def _pick_fields(candidate_fields: list[str], preferred_fields: list[str], limit: int = 2) -> list[str]:
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


def _build_qaqc_scaffold(schema_metadata: SchemaMetadata) -> Optional[Dict[str, Any]]:
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
        'value',
        'rate',
        'amount',
        'ratedCapacityKW',
        'capacity',
        'unit',
        'units',
    ]
    qualitative_preferences = [
        'details',
        'description',
        'summary',
        'extracted_text',
        'condition',
        'conditions',
        'notes',
    ]

    nested_array = _find_nested_object_array_field(main_item_schema)
    projection: Optional[Dict[str, Any]] = None
    if nested_array is not None:
        nested_array_name, child_item_schema = nested_array
        parent_fields = [
            field_name for field_name in dedup_key_fields
            if field_name in _schema_properties(main_item_schema)
        ]
        child_scalar_fields = _scalar_field_names(child_item_schema)
        child_key_fields = [
            field_name for field_name in dedup_key_fields
            if field_name in _schema_properties(child_item_schema)
        ]
        key_fields = parent_fields + child_key_fields
        if not key_fields:
            key_fields = parent_fields + child_scalar_fields[:1]

        quantitative_fields = _pick_fields(child_scalar_fields, quantitative_preferences)
        qualitative_fields = _pick_fields(child_scalar_fields, qualitative_preferences, limit=1)
        if not qualitative_fields:
            qualitative_fields = _pick_fields(child_scalar_fields, child_scalar_fields, limit=1)

        projection = {
            'type': 'nested_array_items',
            'source_array': main_data_array,
            'nested_array': nested_array_name,
        }
        if parent_fields:
            projection['parent_fields'] = parent_fields
    else:
        scalar_fields = _scalar_field_names(main_item_schema)
        key_fields = [
            field_name for field_name in dedup_key_fields
            if field_name in _schema_properties(main_item_schema)
        ]
        if not key_fields:
            key_fields = scalar_fields[:2]

        quantitative_fields = _pick_fields(scalar_fields, quantitative_preferences)
        qualitative_fields = _pick_fields(scalar_fields, qualitative_preferences, limit=1)
        if not qualitative_fields:
            qualitative_fields = _pick_fields(scalar_fields, scalar_fields, limit=1)

    if not key_fields or not quantitative_fields:
        return None

    quantitative_lane: Dict[str, Any] = {
        'enabled': True,
        'mode': 'quantitative',
        'comparison_approach': 'numeric_only',
        'record_matching': {'key_fields': key_fields},
        'comparison': {'primary_fields': quantitative_fields},
    }
    if projection is not None:
        quantitative_lane['projection'] = projection

    qualitative_lane: Dict[str, Any] = {
        'enabled': False,
        'mode': 'qualitative',
        'comparison_approach': 'text_review',
        'record_matching': {'key_fields': key_fields},
        'comparison': {'primary_fields': qualitative_fields or quantitative_fields[:1]},
    }
    if projection is not None:
        qualitative_lane['projection'] = deepcopy(projection)

    return {
        'default_lane': 'quantitative',
        'lanes': {
            'quantitative': quantitative_lane,
            'qualitative': qualitative_lane,
        },
    }


def _build_consolidation_scaffold(schema_metadata: SchemaMetadata) -> Dict[str, Any]:
    consolidation_metadata = (schema_metadata.metadata.get('consolidation') or {})
    deduplication_metadata = consolidation_metadata.get('deduplication') or {}
    output_metadata = consolidation_metadata.get('output') or {}

    consolidation: Dict[str, Any] = {
        'deduplication': {
            'key_fields': schema_metadata.get_deduplication_key_fields(),
            'ignore_fields': schema_metadata.get_deduplication_ignore_fields(),
        }
    }

    for field_name in ('strategy', 'comparison_mode'):
        if field_name in deduplication_metadata:
            consolidation['deduplication'][field_name] = deepcopy(deduplication_metadata[field_name])

    output: Dict[str, Any] = {}
    for field_name in (
        'default_format',
        'freeze_columns',
        'auto_width',
    ):
        if field_name in output_metadata:
            output[field_name] = deepcopy(output_metadata[field_name])

    if output:
        consolidation['output'] = output

    return consolidation


def _default_profile_template(profile_name: str) -> Dict[str, Any]:
    profile = {
        'profile_id': profile_name,
        'environment': profile_name,
        'runtime': {
            'emit_lineage': True,
            'emit_run_manifests': True,
            'strict_contracts': True,
        },
        'llm': {
            'provider_strategy': 'auto',
        },
    }

    if profile_name == 'dev':
        profile['runtime']['debug_logging'] = True

    return profile


def _profile_tier_names() -> list[str]:
    return ['dev', 'staging', 'prod']


def _profile_filename(profile_name: str) -> str:
    return f'{profile_name}.profile.json'


def _build_pack_scaffold(pack_name: str, schema_path: Path, document_type: str, repo_root: Path) -> Dict[str, Any]:
    schema_metadata = SchemaMetadata(schema_path)
    pack_scaffold = {
        'name': pack_name,
        'version': '1.0.0',
        'schema_path': _normalize_pack_schema_path(schema_path, repo_root),
        'document_type': document_type,
        'consolidation': _build_consolidation_scaffold(schema_metadata),
        'modules': _default_pack_modules(),
    }

    qaqc_scaffold = _build_qaqc_scaffold(schema_metadata)
    if qaqc_scaffold is not None:
        pack_scaffold['qaqc'] = qaqc_scaffold

    return pack_scaffold


def _write_pack_scaffold(pack_path: Path, pack_data: Dict[str, Any], force: bool) -> None:
    if pack_path.exists() and not force:
        raise ArtifactCompilerError(
            f"Domain pack already exists: {pack_path.as_posix()}"
        )

    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_text(
        yaml.safe_dump(pack_data, sort_keys=False, allow_unicode=False),
        encoding='utf-8',
    )


def _write_profile_scaffold(profile_path: Path, profile_data: Dict[str, Any], force: bool) -> None:
    if profile_path.exists() and not force:
        raise ArtifactCompilerError(
            f"Profile already exists: {profile_path.as_posix()}"
        )

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(profile_data, indent=2) + '\n',
        encoding='utf-8',
    )


def _workspace_category_name(pack_name: str) -> str:
    return pack_name


def _create_workspace_skeleton(repo_root: Path, category_name: str) -> list[str]:
    created_paths: list[str] = []
    for root_name in ('documents', 'processed', 'consolidated'):
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
    supported_extensions = ', '.join(SUPPORTED_EXTENSIONS)
    workflow_lines = [
        'Suggested workflow:',
        '1. Replace the placeholder entry in sample_manifest.csv with a real source file name',
        f'2. Add source documents under documents/{category_name}/',
    ]

    if page_ranges_ref:
        workflow_lines.append(f'3. Update {page_ranges_ref} if extraction should target a subset of pages')
        workflow_lines.append(
            '4. Run: '
            + _build_scaffold_process_command(
                documents_ref=f'documents/{category_name}',
                schema_ref=schema_ref,
                page_ranges_ref=page_ranges_ref,
                document_type=document_type,
                template_mode=template_mode,
            )
        )
    else:
        workflow_lines.append(
            '3. Run: '
            + _build_scaffold_process_command(
                documents_ref=f'documents/{category_name}',
                schema_ref=schema_ref,
                document_type=document_type,
                template_mode=template_mode,
            )
        )

    return (
        f'# {category_name} Source Documents\n\n'
        f'Document Type: {document_type}\n\n'
        'This directory stores raw source documents for the domain onboarding scaffold.\n\n'
        f'Supported extensions: {supported_extensions}\n\n'
        'Files:\n'
        '- sample_manifest.csv: Replace the placeholder row with a real source file name before the first run\n\n'
        + '\n'.join(workflow_lines)
        + '\n'
    )


def _sample_manifest_csv_content(document_type: str) -> str:
    return (
        'file_name,notes\n'
        f'{_suggest_page_ranges_filename(document_type)},Replace with a real source document before processing\n'
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
    documents_root = repo_root / 'documents' / category_name
    readme_path = documents_root / 'README.md'
    manifest_path = documents_root / 'sample_manifest.csv'

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
    _write_text_file(manifest_path, _sample_manifest_csv_content(document_type), force=force)

    return [
        documents_root.as_posix(),
        readme_path.as_posix(),
        manifest_path.as_posix(),
    ]


def _suggest_page_ranges_filename(document_type: str) -> str:
    normalized = document_type.strip().lower()
    if 'tariff' in normalized or 'rate' in normalized:
        return 'utility_tariff.pdf'
    if 'permit' in normalized:
        return 'air_quality_permit.pdf'
    if 'ordinance' in normalized:
        return 'geothermal_ordinance.pdf'
    return 'example_document.pdf'


def _recommended_process_flags(document_type: str) -> list[str]:
    normalized = document_type.strip().lower()
    if 'tariff' in normalized or 'rate' in normalized:
        return ['--max-context 1400000']
    return []


def _build_scaffold_process_command(
    *,
    documents_ref: str,
    schema_ref: str,
    profile_ref: Optional[str] = None,
    page_ranges_ref: Optional[str] = None,
    document_type: str,
    template_mode: str = 'recommended',
    enable_qaqc: bool = False,
) -> str:
    command_parts = [
        'pixi run streamline-extract process',
        f'{documents_ref}/',
        f'--schema {schema_ref}',
    ]
    if profile_ref:
        command_parts.append(f'--profile {profile_ref}')
    if template_mode == 'recommended':
        command_parts.extend(_recommended_process_flags(document_type))
    if page_ranges_ref:
        command_parts.append(f'--pages-csv {page_ranges_ref}')
    if enable_qaqc:
        command_parts.append('--enable-qa-qc')

    return ' '.join(command_parts)


def _config_readme_content(
    category_name: str,
    *,
    document_type: str,
    domain_name: str,
    schema_ref: str,
    page_ranges_ref: str,
    has_qaqc: bool,
    template_mode: str,
) -> str:
    workflow_lines = [
        'Suggested workflow:',
        f'1. Add source documents under documents/{category_name}/',
        '2. Update page_ranges.csv if extraction should target a subset of pages',
        (
            '3. Run: '
            f'{_build_scaffold_process_command(documents_ref=f"documents/{category_name}", schema_ref=schema_ref, page_ranges_ref=page_ranges_ref, document_type=document_type, template_mode=template_mode)}'
        ),
        (
            f'4. Run: pixi run streamline-extract consolidate processed/{category_name} '
            f'--schema {schema_ref}'
        ),
    ]

    recommended_flags = _recommended_process_flags(document_type)
    if template_mode == 'recommended' and recommended_flags:
        workflow_lines.extend([
            '',
            'Recommended flags:',
            '- --max-context 1400000: Use for large tariff books so long schedule sections are less likely to be truncated.',
        ])

    if template_mode == 'recommended' and has_qaqc:
        workflow_lines.extend([
            '',
            'Optional QA/QC workflow:',
            (
                '1. Run: '
                f'{_build_scaffold_process_command(documents_ref=f"documents/{category_name}", schema_ref=schema_ref, page_ranges_ref=page_ranges_ref, document_type=document_type, template_mode=template_mode, enable_qaqc=True)}'
            ),
            (
                f'2. Run: pixi run streamline-extract compare processed/{category_name}/qa_qc '
                f'--schema {schema_ref}'
            ),
        ])

    return (
        f"# {category_name} Configuration\n\n"
        f"Document Type: {document_type}\n\n"
        f"Domain: {domain_name}\n\n"
        "This directory contains configuration files for the domain onboarding scaffold.\n\n"
        "Files:\n"
        "- page_ranges.csv: Optional page-range overrides for document processing\n\n"
        + "\n".join(workflow_lines)
        + "\n"
    )


def _page_ranges_csv_content(document_type: str) -> str:
    return (
        "file_path,start_page,end_page\n"
        f"{_suggest_page_ranges_filename(document_type)},,\n"
    )


def _write_text_file(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise ArtifactCompilerError(f"Scaffold file already exists: {path.as_posix()}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _create_config_skeleton(
    config_root: Path,
    category_name: str,
    *,
    document_type: str,
    domain_name: str,
    schema_ref: str,
    has_qaqc: bool,
    template_mode: str,
    force: bool,
) -> list[str]:
    category_root = config_root / category_name
    readme_path = category_root / 'README.md'
    page_ranges_path = category_root / 'page_ranges.csv'

    _write_text_file(
        readme_path,
        _config_readme_content(
            category_name,
            document_type=document_type,
            domain_name=domain_name,
            schema_ref=schema_ref,
            page_ranges_ref=page_ranges_path.as_posix(),
            has_qaqc=has_qaqc,
            template_mode=template_mode,
        ),
        force=force,
    )
    _write_text_file(page_ranges_path, _page_ranges_csv_content(document_type), force=force)

    return [
        category_root.as_posix(),
        readme_path.as_posix(),
        page_ranges_path.as_posix(),
    ]


def _build_onboarding_next_steps(
    *,
    repo_root: Path,
    pack_path: Path,
    schema_path: Path,
    profile_ref: str,
    category_name: str,
    config_root: Path,
    document_type: str,
    create_config: bool,
    create_sample_assets: bool,
    has_qaqc: bool,
    template_mode: str,
) -> list[dict[str, str]]:
    pack_ref = _display_cli_path(pack_path, repo_root)
    schema_ref = _display_cli_path(schema_path, repo_root)
    profile_display = _display_cli_reference(profile_ref, repo_root)
    documents_dir = _display_cli_path(repo_root / 'documents' / category_name, repo_root)
    processed_dir = _display_cli_path(repo_root / 'processed' / category_name, repo_root)
    page_ranges_path = _display_cli_path(config_root / category_name / 'page_ranges.csv', repo_root)

    process_command = _build_scaffold_process_command(
        documents_ref=documents_dir,
        schema_ref=schema_ref,
        profile_ref=profile_display,
        page_ranges_ref=page_ranges_path if create_config else None,
        document_type=document_type,
        template_mode=template_mode,
    )
    next_steps = [
        {
            'title': 'Add source documents',
            'detail': f'Place source files under {documents_dir}/ before the first run.',
        },
        {
            'title': 'Validate runtime seam',
            'command': (
                'pixi run streamline-extract validate-runtime '
                f'--pack {pack_ref} --profile {profile_display}'
            ),
        },
        {
            'title': 'Process documents',
            'command': process_command,
        },
        {
            'title': 'Consolidate extracted records',
            'command': (
                'pixi run streamline-extract consolidate '
                f'{processed_dir} --schema {schema_ref}'
            ),
        },
    ]

    if create_sample_assets:
        next_steps.insert(0, {
            'title': 'Review scaffolded sample assets',
            'detail': (
                f'Update {documents_dir}/sample_manifest.csv and replace placeholder files '
                f'under {documents_dir}/ before the first run.'
            ),
        })

    if template_mode == 'recommended' and has_qaqc:
        next_steps.extend([
            {
                'title': 'Optional multi-model QA/QC run',
                'command': _build_scaffold_process_command(
                    documents_ref=documents_dir,
                    schema_ref=schema_ref,
                    profile_ref=profile_display,
                    page_ranges_ref=page_ranges_path if create_config else None,
                    document_type=document_type,
                    template_mode=template_mode,
                    enable_qaqc=True,
                ),
            },
            {
                'title': 'Generate QA/QC comparison reports',
                'command': (
                    'pixi run streamline-extract compare '
                    f'{processed_dir}/qa_qc --schema {schema_ref}'
                ),
            },
        ])

    return next_steps


def _resolve_scaffold_root_input(root_value: str, repo_root: Path) -> str:
    candidate = Path(root_value)
    if candidate.is_absolute():
        return candidate.as_posix()
    return (repo_root / candidate).resolve().as_posix()


def _collect_existing_scaffold_targets(
    *,
    pack_path: Path,
    create_profile: bool,
    profile_tiering: bool,
    profile_name: str,
    profiles_root: Path,
    create_config: bool,
    create_sample_assets: bool,
    config_root: Path,
    category_name: str,
) -> list[Path]:
    existing_paths: list[Path] = []

    if pack_path.exists():
        existing_paths.append(pack_path)

    if create_profile:
        profile_path = profiles_root / _profile_filename(profile_name)
        if profile_path.exists():
            existing_paths.append(profile_path)
    elif profile_tiering:
        for tier_name in _profile_tier_names():
            profile_path = profiles_root / _profile_filename(tier_name)
            if profile_path.exists():
                existing_paths.append(profile_path)

    if create_config:
        category_root = config_root / category_name
        for scaffold_path in (category_root / 'README.md', category_root / 'page_ranges.csv'):
            if scaffold_path.exists():
                existing_paths.append(scaffold_path)

    if create_sample_assets:
        category_root = _cli_repo_root() / 'documents' / category_name
        for scaffold_path in (category_root / 'README.md', category_root / 'sample_manifest.csv'):
            if scaffold_path.exists():
                existing_paths.append(scaffold_path)

    return existing_paths


def _confirm_interactive_overwrite(existing_paths: list[Path]) -> bool:
    console.print('\n[bold yellow]Existing scaffold targets detected[/bold yellow]')
    for path in existing_paths:
        console.print(f'  - {path.as_posix()}')
    console.print()
    return click.confirm('Overwrite existing scaffold files?', default=False)


def _resolve_init_domain_pack_inputs(
    *,
    repo_root: Path,
    interactive: bool,
    pack_name: Optional[str],
    schema_path: Optional[str],
    document_type: Optional[str],
    profile_ref: str,
    create_profile: bool,
    profile_tiering: bool,
    profile_name: str,
    output_root: Optional[str],
    profiles_root: Optional[str],
    create_workspace: bool,
    create_config: bool,
    create_sample_assets: bool,
    config_root: Optional[str],
    template_mode: Optional[str],
    report_format: str,
) -> dict[str, Any]:
    if interactive and report_format == 'json':
        raise ArtifactCompilerError('--interactive only supports text output; omit --report-format json')

    if interactive:
        console.print('[bold]Interactive Domain Pack Setup[/bold]')
        console.print('Answer the prompts to scaffold a new runtime-ready domain pack.\n')

    if interactive and not pack_name:
        pack_name = click.prompt('Pack name')
    if interactive and not schema_path:
        schema_path = click.prompt('Schema path', type=click.Path(exists=True))

    if not pack_name:
        raise ArtifactCompilerError('init-domain-pack requires --name unless --interactive is used')
    if not schema_path:
        raise ArtifactCompilerError('init-domain-pack requires --schema unless --interactive is used')

    if interactive and document_type is None:
        if click.confirm('Override the schema document type?', default=False):
            document_type = click.prompt('Document type')

    if interactive and not create_profile and not profile_tiering:
        profile_mode = click.prompt(
            'Profile scaffold mode',
            type=click.Choice(['none', 'single', 'tiered'], case_sensitive=False),
            default='none',
            show_choices=True,
        ).lower()
        create_profile = profile_mode == 'single'
        profile_tiering = profile_mode == 'tiered'

    if interactive and create_profile and profile_name == 'default':
        profile_name = click.prompt('Profile name', default='default', show_default=True)

    if interactive and profile_tiering and profile_ref == 'default':
        profile_ref = click.prompt(
            'Validation profile tier',
            type=click.Choice(_profile_tier_names(), case_sensitive=False),
            default='dev',
            show_choices=True,
        ).lower()

    if interactive and not create_workspace:
        create_workspace = click.confirm('Create workspace folders under documents/ processed/ and consolidated/?', default=False)

    if interactive and not create_config:
        create_config = click.confirm('Create config/<domain>/ starter files?', default=True)

    if interactive and template_mode is None:
        template_mode = click.prompt(
            'Template mode',
            type=click.Choice(['recommended', 'minimal'], case_sensitive=False),
            default='recommended',
            show_choices=True,
        ).lower()

    template_mode = template_mode or 'recommended'

    if interactive and output_root is None:
        output_root = _resolve_scaffold_root_input(
            click.prompt('Pack output root', default='schemas/domain_packs', show_default=True),
            repo_root,
        )

    if interactive and profiles_root is None and (create_profile or profile_tiering):
        profiles_root = _resolve_scaffold_root_input(
            click.prompt('Profiles root', default='schemas/profiles', show_default=True),
            repo_root,
        )

    if interactive and config_root is None and create_config:
        config_root = _resolve_scaffold_root_input(
            click.prompt('Config root', default='config', show_default=True),
            repo_root,
        )

    return {
        'pack_name': pack_name,
        'schema_path': schema_path,
        'document_type': document_type,
        'profile_ref': profile_ref,
        'create_profile': create_profile,
        'profile_tiering': profile_tiering,
        'profile_name': profile_name,
        'output_root': output_root,
        'profiles_root': profiles_root,
        'create_workspace': create_workspace,
        'create_config': create_config,
        'create_sample_assets': create_sample_assets,
        'config_root': config_root,
        'template_mode': template_mode,
    }


@click.command()
def init():
    """
    Interactive project setup wizard.
    
    Guides you through setting up your StreamlineExtract project including:
    - API credentials configuration
    - Document type selection
    - Schema selection
    - Directory structure creation
    
    \b
    EXAMPLE:
        streamline-extract init
    """
    print_header("🚀 StreamlineExtract Setup Wizard")
    
    console.print("Let's set up your project!\n")
    
    # Check if .env already exists
    env_path = Path.cwd() / '.env'
    if env_path.exists():
        if not ask_confirm(f".env file already exists at {env_path}. Overwrite?", default=False):
            console.print("[yellow]Keeping existing .env file[/yellow]\n")
            return
    
    # API Provider Selection
    console.print("[bold]Step 1: API Configuration[/bold]")
    provider = ask_choice(
        "Select API provider",
        choices=["azure", "openai"],
        default="azure"
    )
    
    env_content = []
    
    if provider == "azure":
        console.print("\n[cyan]Azure OpenAI Configuration[/cyan]")
        api_key = ask_text("Azure OpenAI API Key")
        endpoint = ask_text("Azure OpenAI Endpoint", default="https://your-endpoint.openai.azure.com/")
        deployment = ask_text("Model Deployment Name", default="gpt-4o-mini")
        
        env_content = [
            "# Azure OpenAI Configuration",
            f"AZURE_OPENAI_API_KEY={api_key}",
            f"AZURE_OPENAI_ENDPOINT={endpoint}",
            f"AZURE_OPENAI_MODEL={deployment}",
            "AZURE_OPENAI_API_VERSION=2025-04-01-preview",
        ]
    else:
        console.print("\n[cyan]OpenAI Configuration[/cyan]")
        api_key = ask_text("OpenAI API Key (starts with sk-)")
        
        env_content = [
            "# OpenAI Configuration",
            f"OPENAI_API_KEY={api_key}",
        ]
    
    # Write .env file
    with open(env_path, 'w') as f:
        f.write('\n'.join(env_content) + '\n')
    
    print_success(f"Created .env file at {env_path}")
    
    # Document Type Selection
    console.print("\n[bold]Step 2: Document Type[/bold]")
    doc_type = ask_choice(
        "What type of documents will you process?",
        choices=["tariffs", "ordinances", "permits", "custom"],
        default="tariffs"
    )
    
    # Create directory structure
    console.print("\n[bold]Step 3: Directory Structure[/bold]")
    
    project_root = Path.cwd()
    docs_dir = project_root / 'documents' / doc_type
    processed_dir = project_root / 'processed' / doc_type
    consolidated_dir = project_root / 'consolidated' / doc_type
    
    docs_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    consolidated_dir.mkdir(parents=True, exist_ok=True)
    
    print_success("Created directory structure:")
    console.print(create_file_tree(project_root / 'documents', "Project Structure"))
    
    # Schema selection
    console.print("\n[bold]Step 4: Schema Selection[/bold]")
    
    config = get_config()
    schemas = list(config.schema_dir.glob("*.json"))
    
    if schemas:
        schema_names = [s.stem for s in schemas]
        
        # Try to auto-detect based on doc_type
        suggested_schema = None
        if doc_type == "tariffs" and any("tariff" in s for s in schema_names):
            suggested_schema = next(s for s in schema_names if "tariff" in s)
        elif doc_type == "ordinances" and any("ordinance" in s for s in schema_names):
            suggested_schema = next(s for s in schema_names if "ordinance" in s)
        
        if suggested_schema:
            console.print(f"[dim]Suggested schema: [cyan]{suggested_schema}[/cyan][/dim]")
        
        if len(schema_names) > 1:
            console.print(f"\nAvailable schemas: {', '.join(schema_names)}")
    
    # Summary
    console.print("\n" + "="*80)
    print_success("Setup complete! 🎉")
    console.print("\n[bold]Next steps:[/bold]")
    console.print(f"  1. Add your documents to: [cyan]{docs_dir}[/cyan]")
    console.print(f"  2. Run extraction: [green]streamline-extract process {docs_dir}[/green]")
    console.print(f"  3. Consolidate results: [green]streamline-extract consolidate {processed_dir}[/green]")
    console.print()


@click.command()
@click.argument('document_path', type=click.Path(exists=True))
def preview(document_path: str):
    """
    Preview a document before extraction.
    
    Shows document information, estimated costs, and sample content
    without performing the full extraction.
    
    \b
    EXAMPLES:
        streamline-extract preview documents/sample.pdf
        streamline-extract preview documents/tariffs/rate_schedule.docx
    """
    doc_path = Path(document_path)
    
    # Validate file type
    if not is_supported_document(doc_path):
        supported = ', '.join(SUPPORTED_EXTENSIONS)
        print_error(
            f"Unsupported file type: {doc_path.suffix}",
            f"Supported formats: {supported}"
        )
        return
    
    print_header(f"📄 Document Preview: {doc_path.name}")
    
    # Get file info
    file_size = doc_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)
    
    console.print("[bold]Document Info[/bold]")
    info_table = create_config_table("", {
        "File": doc_path.name,
        "Size": f"{file_size_mb:.2f} MB",
        "Type": doc_path.suffix.upper(),
    })
    console.print(info_table)
    
    # Extract text to analyze
    try:
        console.print("\n[dim]Analyzing document...[/dim]")
        text = extract_text_from_document(doc_path)
        text_length = len(text)
        
        # Estimate tokens (rough approximation: 4 chars per token)
        estimated_tokens = text_length // 4
        
        # Estimate cost for gpt-4o-mini
        # Input: $0.15/1M tokens, Output: $0.60/1M tokens
        # Assume output is ~10% of input
        input_cost = (estimated_tokens / 1_000_000) * 0.15
        output_cost = (estimated_tokens * 0.1 / 1_000_000) * 0.60
        total_cost = input_cost + output_cost
        
        # Estimate time (rough: 100 tokens per second)
        estimated_time = estimated_tokens / 100
        
        console.print("\n[bold]Content Analysis[/bold]")
        analysis_table = create_config_table("", {
            "Text Length": f"{text_length:,} characters",
            "Estimated Tokens": f"~{estimated_tokens:,}",
        })
        console.print(analysis_table)
        
        # Try to detect schema
        config = get_config()
        path_lower = str(doc_path).lower()
        matched_schema = None
        
        for candidate in config.schema_dir.glob("*.json"):
            candidate_name_lower = candidate.stem.lower()
            if any(keyword in path_lower for keyword in ['geothermal', 'ordinance']) and 'geothermal' in candidate_name_lower:
                matched_schema = candidate
                break
            elif any(keyword in path_lower for keyword in ['tariff', 'rate', 'electric']) and 'tariff' in candidate_name_lower:
                matched_schema = candidate
                break
        
        if matched_schema:
            console.print(f"\n[bold]Schema Detection[/bold]")
            console.print(f"  ✓ Matched schema: [cyan]{matched_schema.name}[/cyan]")
            
            # Load and show field count
            schema = load_schema(matched_schema)
            if 'properties' in schema:
                field_count = len(schema['properties'])
                console.print(f"  [dim]Expected fields: {field_count}[/dim]")
        
        # Cost estimate
        print_cost_estimate(
            total_docs=1,
            estimated_tokens=estimated_tokens,
            estimated_cost=total_cost,
            estimated_time=estimated_time / 60,  # Convert to minutes
            model="gpt-4o-mini"
        )
        
        # Show sample content
        console.print("[bold]Sample Content Preview[/bold]")
        sample = text[:500].replace('\n', ' ')
        console.print(f"[dim]{sample}...[/dim]\n")
        
        console.print("[bold green]Ready to extract?[/bold green]")
        console.print(f"Run: [cyan]streamline-extract process {doc_path} --schema schemas/example_utility_rate_schema.json[/cyan]\n")
        
    except Exception as e:
        print_error("Failed to analyze document", str(e))


@click.command()
@click.argument('documents_path', type=click.Path(exists=True))
@click.option('--workers', '-w', type=int, default=4, help='Number of parallel workers')
@click.option('--pages-csv', type=click.Path(exists=True), default=None, help='CSV file mapping documents to page ranges')
def estimate(documents_path: str, workers: int, pages_csv: str):
    """
    Estimate cost and time for batch document extraction.
    
    Analyzes all documents in a directory and provides detailed
    cost and time estimates before you commit to processing.
    
    \b
    EXAMPLES:
        streamline-extract estimate documents/tariffs/
        streamline-extract estimate documents/permits/ --workers 8
        streamline-extract estimate documents/tariffs/ --pages-csv config/tariffs/page_ranges.csv
    """
    docs_path = Path(documents_path)
    
    if not docs_path.exists():
        print_error(f"Path not found: {docs_path}")
        return
    
    print_header("💰 Cost & Time Estimation")
    
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
            doc_files.extend(sorted(docs_path.glob(f"*{ext}")))
        
        if not doc_files:
            supported = ', '.join(SUPPORTED_EXTENSIONS)
            print_error(
                f"No supported documents found in {docs_path}",
                f"Supported formats: {supported}"
            )
            return
    
    console.print(f"[dim]Analyzing {len(doc_files)} document(s)...[/dim]\n")
    
    # Load page ranges if provided
    page_range_map = {}
    if pages_csv:
        from streamline_extract.utils.page_range import load_pages_csv
        try:
            page_mappings = load_pages_csv(Path(pages_csv))
            
            # Match file names from doc_files to mappings
            for doc in doc_files:
                # Try exact match first
                if str(doc) in page_mappings:
                    page_range_map[doc] = page_mappings[str(doc)]
                elif doc.name in page_mappings:
                    page_range_map[doc] = page_mappings[doc.name]
            
            mapped_count = sum(1 for v in page_range_map.values() if v is not None)
            console.print(f"[dim]Loaded page ranges for {mapped_count} file(s) from CSV[/dim]\n")
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
    model_name = config.llm_config.get('model', 'gpt-4o-mini')
    provider = config.llm_config.get('provider', 'openai')
    
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
    console.print("[bold]Document Analysis[/bold]")
    doc_info = {
        "Total Documents": str(len(doc_files)),
        "Sample Analyzed": f"{sample_size} files",
        "Avg Characters/Doc": f"{avg_chars:,.0f}",
        "Estimated Total Tokens": f"~{estimated_tokens:,}",
    }
    
    # Add page range info if applicable
    if pages_csv and page_range_map:
        files_with_ranges = sum(1 for v in page_range_map.values() if v is not None)
        if files_with_ranges > 0:
            doc_info["Page Ranges"] = f"{files_with_ranges} file(s) with specific ranges"
    
    doc_table = create_config_table("", doc_info)
    console.print(doc_table)
    
    # Display cost breakdown
    console.print(f"\n[bold]Cost Breakdown ({model_name})[/bold]")
    cost_table = create_config_table("", {
        "Input Tokens": f"~{estimated_tokens:,} @ ${input_cost_per_1m:.2f}/1M",
        "Output Tokens": f"~{int(output_tokens):,} @ ${output_cost_per_1m:.2f}/1M",
        "Total Estimated Cost": f"[magenta bold]${total_cost:.2f}[/magenta bold]",
    })
    console.print(cost_table)
    
    # Display time estimate
    console.print("\n[bold]Time Estimate[/bold]")
    time_table = create_config_table("", {
        "Workers": str(workers),
        "Processing Time": f"~{estimated_minutes:.1f} minutes",
    })
    console.print(time_table)
    
    # Confirmation prompt
    console.print()
    if ask_confirm("Proceed with extraction?", default=False):
        console.print(f"\n[green]Run:[/green] [cyan]streamline-extract process {docs_path} --schema schemas/example_utility_rate_schema.json[/cyan]\n")
    else:
        console.print("[yellow]Operation cancelled[/yellow]\n")


@click.command('validate-schema')
@click.argument('schema_path', type=click.Path(exists=True))
def validate_schema_cmd(schema_path: str):
    """
    Validate a JSON schema file.
    
    Checks schema syntax, structure, and StreamlineExtract-specific
    metadata requirements.
    
    \b
    EXAMPLES:
        streamline-extract validate-schema schemas/my_schema.json
        streamline-extract validate-schema schemas/example_utility_rate_schema.json
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
    if '$schema' in schema:
        print_success("Has $schema declaration")
    else:
        warnings.append("Missing $schema declaration (recommended)")
    
    if 'type' in schema:
        print_success(f"Schema type: {schema['type']}")
    else:
        issues.append("Missing 'type' field (required)")
    
    if 'properties' in schema:
        field_count = len(schema['properties'])
        print_success(f"Has {field_count} properties defined")
    else:
        issues.append("Missing 'properties' field (required)")
    
    # Check StreamlineExtract metadata
    console.print("\n[bold]StreamlineExtract Metadata[/bold]")
    
    if '$metadata' in schema:
        metadata = schema['$metadata']
        print_success("Has $metadata section")
        
        if 'identifier_fields' in metadata:
            id_fields = metadata['identifier_fields']
            console.print(f"  [dim]Identifier fields: {', '.join(id_fields)}[/dim]")
        else:
            warnings.append("$metadata missing 'identifier_fields' (recommended for deduplication)")
        
        if 'main_data_array' in metadata:
            main_array = metadata['main_data_array']
            console.print(f"  [dim]Main data array: {main_array}[/dim]")
        else:
            warnings.append("$metadata missing 'main_data_array' (recommended for consolidation)")
        
        if 'deduplication' in metadata:
            console.print(f"  [dim]Deduplication configured[/dim]")
        else:
            console.print(f"  [dim]No deduplication config (optional)[/dim]")
    else:
        warnings.append("Missing $metadata section (recommended for better consolidation)")
    
    # Summary
    console.print("\n" + "="*80)
    
    if issues:
        console.print("\n[bold red]❌ Issues Found:[/bold red]")
        for issue in issues:
            console.print(f"  • {issue}")
        console.print()
    elif warnings:
        console.print("\n[bold yellow]⚠ Recommendations:[/bold yellow]")
        for warning in warnings:
            console.print(f"  • {warning}")
        console.print()
        print_success("\nSchema is valid but could be improved! ✨")
    else:
        print_success("\nSchema is perfect! ✨")
    
    console.print()


@click.command('validate-runtime')
@click.option('--schema', 'schema_path', type=click.Path(exists=True), help='Schema path to validate through runtime pack resolution')
@click.option('--pack', 'pack_ref', help='Pack name or pack.yaml path to validate directly')
@click.option('--profile', 'profile_ref', default='default', show_default=True, help='Runtime profile name or profile path to validate')
@click.option('--report-format', type=click.Choice(['text', 'json']), default='text', show_default=True, help='Output format for the readiness report')
def validate_runtime_cmd(
    schema_path: Optional[str],
    pack_ref: Optional[str],
    profile_ref: str,
    report_format: str,
):
    """
    Validate runtime onboarding readiness for a schema or pack.

    This command resolves the canonical runtime artifact seam used by the
    modernized pipeline and reports whether the selected input is ready for
    profile-backed execution.

    \b
    EXAMPLES:
        streamline-extract validate-runtime --schema schemas/personal/electricity_tariff_schema.json
        streamline-extract validate-runtime --pack tariffs --profile prod
        streamline-extract validate-runtime --pack schemas/domain_packs/tariffs/pack.yaml --report-format json
    """
    try:
        report = build_runtime_readiness_report(
            schema_path=Path(schema_path) if schema_path else None,
            pack_ref=pack_ref,
            profile_ref=profile_ref,
        )
    except ArtifactCompilerError as exc:
        error_report = {
            'status': 'error',
            'target': {
                'schema_path': schema_path,
                'pack_ref': pack_ref,
                'profile_ref': profile_ref,
            },
            'error': {
                'category': 'runtime_validation',
                'message': str(exc),
            },
        }
        if report_format == 'json':
            click.echo(json.dumps(error_report, indent=2))
        else:
            print_header('Runtime Validation')
            print_error('Runtime onboarding validation failed', str(exc))
        raise click.exceptions.Exit(1)

    if report_format == 'json':
        click.echo(json.dumps(report, indent=2))
        return

    print_header('Runtime Validation')
    print_success('Runtime onboarding seam is ready')
    summary = create_config_table('', {
        'Pack': report['resolved']['pack_name'],
        'Profile': report['resolved']['profile_name'],
        'Artifact': report['resolved']['artifact_id'].rsplit('/', 1)[-1],
        'Schema Path': report['resolved']['schema_path'],
        'Schema File': report['resolved']['schema_file_path'],
        'Main Data Array': report['resolved']['main_data_array'],
        'Identifier Fields': ', '.join(report['resolved']['identifier_fields']),
        'Pack Path': report['resolved']['pack_path'],
        'Profile Path': report['resolved']['profile_path'],
        'Enabled Modules': ', '.join(report['resolved']['enabled_module_ids']),
    })
    console.print(summary)

    console.print('\n[bold]Checks[/bold]')
    for check in report['checks']:
        label = 'PASS' if check['status'] == 'pass' else 'WARN'
        console.print(f"  [{ 'green' if check['status'] == 'pass' else 'yellow' }]{label}[/{ 'green' if check['status'] == 'pass' else 'yellow' }] {check['name']}: {check['detail']}")

    console.print()


@click.command('init-domain-pack')
@click.option('--interactive', is_flag=True, help='Prompt for missing values and onboarding scaffold choices')
@click.option('--name', 'pack_name', help='Pack name to create under schemas/domain_packs/')
@click.option('--schema', 'schema_path', type=click.Path(exists=True), help='Existing schema file the new pack should target')
@click.option('--document-type', help='Optional human-readable document type; defaults to schema metadata document_type')
@click.option('--profile', 'profile_ref', default='default', show_default=True, help='Runtime profile to validate after scaffolding')
@click.option('--with-profile', 'create_profile', is_flag=True, help='Also scaffold a profile JSON and validate against the created profile path')
@click.option('--profile-tiering', is_flag=True, help='Scaffold the standard dev/staging/prod profile set and validate against one generated tier')
@click.option('--profile-name', default='default', show_default=True, help='Profile name to create when --with-profile is used')
@click.option('--output-root', type=click.Path(file_okay=False, dir_okay=True), help='Optional directory for writing the pack scaffold (defaults to schemas/domain_packs)')
@click.option('--profiles-root', type=click.Path(file_okay=False, dir_okay=True), help='Optional directory for writing scaffolded profiles (defaults to schemas/profiles)')
@click.option('--with-workspace', 'create_workspace', is_flag=True, help='Also scaffold matching documents/processed/consolidated folders for the new domain')
@click.option('--with-config', 'create_config', is_flag=True, help='Also scaffold config/<domain>/ starter files for the new domain')
@click.option('--with-sample-assets', 'create_sample_assets', is_flag=True, help='Also scaffold placeholder source-document assets under documents/<domain>/')
@click.option('--config-root', type=click.Path(file_okay=False, dir_okay=True), help='Optional directory for writing scaffolded config files (defaults to config/)')
@click.option('--template-mode', type=click.Choice(['recommended', 'minimal']), help='Starter guidance level for scaffolded docs and next-step output')
@click.option('--force', is_flag=True, help='Overwrite an existing pack.yaml if it already exists')
@click.option('--report-format', type=click.Choice(['text', 'json']), default='text', show_default=True, help='Output format for scaffold and validation results')
def init_domain_pack_cmd(
    interactive: bool,
    pack_name: str,
    schema_path: str,
    document_type: Optional[str],
    profile_ref: str,
    create_profile: bool,
    profile_tiering: bool,
    profile_name: str,
    output_root: Optional[str],
    profiles_root: Optional[str],
    create_workspace: bool,
    create_config: bool,
    create_sample_assets: bool,
    config_root: Optional[str],
    template_mode: Optional[str],
    force: bool,
    report_format: str,
):
    """
    Create a starter domain pack and immediately validate it.

    This command is a minimal onboarding scaffold for the modernization runtime.
    It writes a starter `pack.yaml` with the default module set, then runs the
    same runtime validation path used by `validate-runtime`.

    \b
    EXAMPLES:
        streamline-extract init-domain-pack --name my_domain --schema schemas/my_schema.json
        streamline-extract init-domain-pack --name my_domain --schema schemas/my_schema.json --profile prod
        streamline-extract init-domain-pack --interactive
        streamline-extract init-domain-pack --interactive --output-root /tmp/domain_packs
        streamline-extract init-domain-pack --name my_domain --schema schemas/my_schema.json --template-mode minimal
        streamline-extract init-domain-pack --name my_domain --schema schemas/my_schema.json --report-format json
    """
    repo_root = _cli_repo_root()
    schema_file = Path(schema_path).resolve() if schema_path else None
    target_root = Path(output_root).resolve() if output_root else (repo_root / 'schemas/domain_packs')
    target_profiles_root = Path(profiles_root).resolve() if profiles_root else (repo_root / 'schemas/profiles')
    target_config_root = Path(config_root).resolve() if config_root else (repo_root / 'config')
    pack_path = target_root / (pack_name or '__pending__') / 'pack.yaml'
    created_profile_path: Optional[Path] = None
    created_profile_paths: list[str] = []
    created_workspace_paths: list[str] = []
    created_config_paths: list[str] = []
    created_sample_asset_paths: list[str] = []
    validation_profile_ref = profile_ref

    try:
        resolved_inputs = _resolve_init_domain_pack_inputs(
            repo_root=repo_root,
            interactive=interactive,
            pack_name=pack_name,
            schema_path=schema_path,
            document_type=document_type,
            profile_ref=profile_ref,
            create_profile=create_profile,
            profile_tiering=profile_tiering,
            profile_name=profile_name,
            output_root=output_root,
            profiles_root=profiles_root,
            create_workspace=create_workspace,
            create_config=create_config,
            create_sample_assets=create_sample_assets,
            config_root=config_root,
            template_mode=template_mode,
            report_format=report_format,
        )
        pack_name = resolved_inputs['pack_name']
        schema_path = resolved_inputs['schema_path']
        document_type = resolved_inputs['document_type']
        profile_ref = resolved_inputs['profile_ref']
        create_profile = resolved_inputs['create_profile']
        profile_tiering = resolved_inputs['profile_tiering']
        profile_name = resolved_inputs['profile_name']
        output_root = resolved_inputs['output_root']
        profiles_root = resolved_inputs['profiles_root']
        create_workspace = resolved_inputs['create_workspace']
        create_config = resolved_inputs['create_config']
        create_sample_assets = resolved_inputs['create_sample_assets']
        config_root = resolved_inputs['config_root']
        template_mode = resolved_inputs['template_mode']

        target_root = Path(output_root).resolve() if output_root else (repo_root / 'schemas/domain_packs')
        target_profiles_root = Path(profiles_root).resolve() if profiles_root else (repo_root / 'schemas/profiles')
        target_config_root = Path(config_root).resolve() if config_root else (repo_root / 'config')

        schema_file = Path(schema_path).resolve()
        pack_path = target_root / pack_name / 'pack.yaml'
        validation_profile_ref = profile_ref

        existing_scaffold_targets = _collect_existing_scaffold_targets(
            pack_path=pack_path,
            create_profile=create_profile,
            profile_tiering=profile_tiering,
            profile_name=profile_name,
            profiles_root=target_profiles_root,
            create_config=create_config,
            create_sample_assets=create_sample_assets,
            config_root=target_config_root,
            category_name=_workspace_category_name(pack_name),
        )
        if interactive and existing_scaffold_targets and not force:
            if _confirm_interactive_overwrite(existing_scaffold_targets):
                force = True
            else:
                raise ArtifactCompilerError(
                    'Interactive onboarding cancelled because scaffold targets already exist; rerun and confirm overwrite or pass --force'
                )

        if create_profile and profile_tiering:
            raise ArtifactCompilerError(
                'Specify only one of --with-profile or --profile-tiering when scaffolding profiles'
            )

        scaffold = _build_pack_scaffold(
            pack_name,
            schema_file,
            document_type or _infer_document_type_from_schema(schema_file),
            repo_root,
        )
        _write_pack_scaffold(pack_path, scaffold, force=force)
        if profile_tiering:
            validation_profile_name = profile_ref if profile_ref != 'default' else 'dev'
            if validation_profile_name not in _profile_tier_names():
                raise ArtifactCompilerError(
                    'When using --profile-tiering, --profile must be one of dev, staging, or prod'
                )

            for tier_name in _profile_tier_names():
                profile_path = target_profiles_root / _profile_filename(tier_name)
                _write_profile_scaffold(
                    profile_path,
                    _default_profile_template(tier_name),
                    force=force,
                )
                created_profile_paths.append(profile_path.as_posix())
                if tier_name == validation_profile_name:
                    created_profile_path = profile_path

            if created_profile_path is None:
                raise ArtifactCompilerError(
                    'Failed to resolve validation profile from scaffolded tier set'
                )
            validation_profile_ref = str(created_profile_path)
        elif create_profile:
            created_profile_path = target_profiles_root / _profile_filename(profile_name)
            _write_profile_scaffold(
                created_profile_path,
                _default_profile_template(profile_name),
                force=force,
            )
            created_profile_paths.append(created_profile_path.as_posix())
            validation_profile_ref = str(created_profile_path)
        if create_workspace:
            created_workspace_paths = _create_workspace_skeleton(
                repo_root,
                _workspace_category_name(pack_name),
            )
        if create_config:
            created_config_paths = _create_config_skeleton(
                target_config_root,
                _workspace_category_name(pack_name),
                document_type=scaffold['document_type'],
                domain_name=(SchemaMetadata(schema_file).metadata.get('domain') or scaffold['document_type']),
                schema_ref=scaffold['schema_path'],
                has_qaqc='qaqc' in scaffold,
                template_mode=template_mode,
                force=force,
            )
        if create_sample_assets:
            created_sample_asset_paths = _create_sample_asset_skeleton(
                repo_root,
                _workspace_category_name(pack_name),
                document_type=scaffold['document_type'],
                schema_ref=scaffold['schema_path'],
                page_ranges_ref=(
                    _display_cli_path(target_config_root / _workspace_category_name(pack_name) / 'page_ranges.csv', repo_root)
                    if create_config else None
                ),
                template_mode=template_mode,
                force=force,
            )
        readiness = build_runtime_readiness_report(
            pack_ref=str(pack_path),
            profile_ref=validation_profile_ref,
            repo_root=repo_root,
        )
    except (ArtifactCompilerError, json.JSONDecodeError) as exc:
        error_report = {
            'status': 'error',
            'error': {
                'category': 'domain_pack_init',
                'message': str(exc),
            },
            'target': {
                'pack_name': pack_name,
                'schema_path': str(schema_file) if schema_file else schema_path,
                'pack_path': pack_path.as_posix(),
                'profile_ref': validation_profile_ref,
            },
        }
        if report_format == 'json':
            click.echo(json.dumps(error_report, indent=2))
        else:
            print_header('Init Domain Pack')
            print_error('Domain pack scaffold failed', str(exc))
        raise click.exceptions.Exit(1)

    result = {
        'status': 'ready',
        'created': {
            'pack_name': pack_name,
            'pack_path': pack_path.as_posix(),
            'schema_path': str(schema_file),
            'interactive': interactive,
            'profile_ref': validation_profile_ref,
            'profile_path': created_profile_path.as_posix() if created_profile_path else None,
            'profile_paths': created_profile_paths,
            'output_root': target_root.as_posix(),
            'profiles_root': target_profiles_root.as_posix(),
            'workspace_paths': created_workspace_paths,
            'config_paths': created_config_paths,
            'sample_asset_paths': created_sample_asset_paths,
            'config_root': target_config_root.as_posix(),
            'document_type': scaffold['document_type'],
            'template_mode': template_mode,
            'force': force,
        },
        'readiness': readiness,
        'next_steps': _build_onboarding_next_steps(
            repo_root=repo_root,
            pack_path=pack_path,
            schema_path=schema_file,
            profile_ref=validation_profile_ref,
            category_name=_workspace_category_name(pack_name),
            config_root=target_config_root,
            document_type=scaffold['document_type'],
            create_config=create_config,
            create_sample_assets=create_sample_assets,
            has_qaqc='qaqc' in scaffold,
            template_mode=template_mode,
        ),
    }

    if report_format == 'json':
        click.echo(json.dumps(result, indent=2))
        return

    print_header('Init Domain Pack')
    print_success('Domain pack scaffold created and validated')
    if interactive:
        print_info('Input mode: interactive prompts resolved missing onboarding options')
    if created_profile_path is not None:
        print_success(f'Profile scaffold created at {created_profile_path.as_posix()}')
    if len(created_profile_paths) > 1:
        print_success(f'Profile tier set created ({len(created_profile_paths)} profiles)')
    if created_workspace_paths:
        print_success(f'Workspace skeleton created ({len(created_workspace_paths)} folders)')
    if created_config_paths:
        print_success(f'Config scaffold created ({len(created_config_paths)} path entries)')
    if created_sample_asset_paths:
        print_success(f'Sample asset scaffold created ({len(created_sample_asset_paths)} path entries)')
    console.print(create_config_table('', {
        'Pack Name': pack_name,
        'Pack Path': pack_path.as_posix(),
        'Schema': str(schema_file),
        'Document Type': scaffold['document_type'],
        'Template Mode': template_mode,
        'Profile': readiness['resolved']['profile_name'],
        'Profile Path': created_profile_path.as_posix() if created_profile_path else '(existing profile)',
        'Profile Count': str(len(created_profile_paths)) if created_profile_paths else '0',
        'Workspace Folders': str(len(created_workspace_paths)) if created_workspace_paths else '0',
        'Config Entries': str(len(created_config_paths)) if created_config_paths else '0',
        'Sample Assets': str(len(created_sample_asset_paths)) if created_sample_asset_paths else '0',
        'Artifact': readiness['resolved']['artifact_id'].rsplit('/', 1)[-1],
    }))
    console.print('\n[bold]Next Steps[/bold]')
    for index, step in enumerate(result['next_steps'], start=1):
        console.print(f'{index}. {step["title"]}')
        if 'detail' in step:
            console.print(f'   {step["detail"]}', soft_wrap=True)
        if 'command' in step:
            console.print(f'   {step["command"]}', soft_wrap=True)

    console.print()


@click.command()
def config():
    """
    Show current configuration.
    
    Displays environment settings, paths, and available schemas.
    
    \b
    EXAMPLE:
        streamline-extract config
    """
    load_dotenv()
    
    print_header("Configuration")
    
    # API Configuration
    console.print("[bold]API Configuration[/bold]")
    
    azure_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    openai_key = os.getenv('OPENAI_API_KEY')
    
    if azure_key and azure_endpoint:
        console.print("  Provider: [magenta]Azure OpenAI[/magenta]")
        console.print(f"  Endpoint: [dim]{azure_endpoint}[/dim]")
        console.print(f"  Model: [dim]{os.getenv('AZURE_OPENAI_MODEL', 'Not set')}[/dim]")
        console.print(f"  API Key: [dim]{'*' * 20}...{azure_key[-4:]}[/dim]")
    elif openai_key:
        console.print("  Provider: [cyan]OpenAI[/cyan]")
        console.print(f"  API Key: [dim]{'*' * 20}...{openai_key[-4:]}[/dim]")
    else:
        console.print("  [yellow]No API credentials configured[/yellow]")
        console.print("  [dim]Run 'streamline-extract init' to set up[/dim]")
    
    # Paths
    config_obj = get_config()
    console.print("\n[bold]Paths[/bold]")
    console.print(f"  Project Root: [dim]{config_obj.project_root}[/dim]")
    console.print(f"  Schemas: [dim]{config_obj.schema_dir}[/dim]")
    
    # Available schemas
    schemas = list(config_obj.schema_dir.glob("*.json"))
    if schemas:
        console.print("\n[bold]Available Schemas[/bold]")
        for schema in schemas:
            console.print(f"  • {schema.name}")
    
    console.print()
