"""Tests for Phase 2 lineage wiring in process output."""

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from streamline_extract.cli.commands import (
    _build_dedup_preview_report,
    _context_budget_suggestions_for_process,
    _extract_and_save_result,
    _format_runtime_artifact_summary,
    _generate_run_id,
    _run_qa_qc_extraction,
    _resolve_consolidation_output_formats,
    _resolve_runtime_artifact,
    _resolve_schema_ref,
    _should_fail_on_suspicious,
)
from streamline_extract.cli.main import cli
from streamline_extract.qa_qc.utils import resolve_qaqc_runtime_config
from streamline_extract.utils.exceptions import SchemaMetadataError
from streamline_extract.utils.schema_metadata import SchemaMetadata


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding='utf-8')


def test_resolve_schema_ref_passes_json_through(tmp_path) -> None:
    schema = tmp_path / 'schema.json'
    _write_json(schema, {'type': 'object'})
    assert _resolve_schema_ref(schema) == schema


def test_resolve_schema_ref_unwraps_domain_pack(tmp_path, monkeypatch) -> None:
    schema = tmp_path / 'schemas' / 'my_schema.json'
    _write_json(schema, {'type': 'object'})
    pack = tmp_path / 'schemas' / 'domain_packs' / 'x' / 'pack.yaml'
    _write_file(pack, 'name: x\nschema_path: schemas/my_schema.json\n')
    # schema_path in a pack is repo-root-relative; resolve from cwd.
    monkeypatch.chdir(tmp_path)
    assert _resolve_schema_ref(pack) == (tmp_path / 'schemas' / 'my_schema.json')


def test_resolve_schema_ref_unwraps_relative_to_pack_dir(tmp_path) -> None:
    pack_dir = tmp_path / 'domain_packs' / 'x'
    schema = pack_dir / 'nested.json'
    _write_json(schema, {'type': 'object'})
    pack = pack_dir / 'pack.yaml'
    _write_file(pack, 'name: x\nschema_path: nested.json\n')
    assert _resolve_schema_ref(pack) == schema


def test_resolve_runtime_artifact_returns_none_without_pack_or_profile_dirs(tmp_path) -> None:
    resolved = _resolve_runtime_artifact(
        category='tariffs',
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        repo_root=tmp_path,
    )

    assert resolved is None


def test_context_budget_suggestions_for_process_use_matching_repo_page_ranges_config() -> None:
    suggestions = _context_budget_suggestions_for_process(
        error_record={'code': 'context_window_exceeded'},
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        document_path=Path('documents/tariffs/PSCo_Electric_Entire_Tariff.pdf'),
        repo_root=REPO_ROOT,
    )

    assert suggestions is not None
    assert '--pages START-END' in suggestions[0]
    assert 'config/tariffs/page_ranges.csv' in suggestions[1]
    assert '25-100 pages' in suggestions[2]


def test_context_budget_suggestions_for_process_without_matching_config_remain_generic(tmp_path) -> None:
    document_path = tmp_path / 'documents' / 'custom_domain' / 'sample.pdf'
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text('placeholder', encoding='utf-8')

    suggestions = _context_budget_suggestions_for_process(
        error_record={'code': 'context_window_exceeded'},
        schema_path=Path('schemas/custom_schema.json'),
        document_path=document_path,
        repo_root=tmp_path,
    )

    assert suggestions is not None
    assert '--pages START-END' in suggestions[0]
    assert all('--pages-csv config/' not in suggestion for suggestion in suggestions)


def test_resolve_runtime_artifact_compiles_when_pack_and_profile_exist(tmp_path) -> None:
    packs_dir = tmp_path / 'schemas/domain_packs'
    profiles_dir = tmp_path / 'schemas/profiles'

    _write_file(
        packs_dir / 'tariffs/pack.yaml',
        'name: tariffs\nversion: 1.0.0\n',
    )
    _write_file(
        profiles_dir / 'default.profile.json',
        '{"profile_id": "default"}',
    )

    resolved = _resolve_runtime_artifact(
        category='tariffs',
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        repo_root=REPO_ROOT,
        domain_packs_dir=packs_dir,
        profiles_dir=profiles_dir,
    )

    assert resolved is not None
    assert resolved['artifact_id'].startswith('artifact://runtime/')
    assert resolved['lineage']['profile_id'] == 'default'


def test_resolve_runtime_artifact_uses_repo_assets_and_selected_profile() -> None:
    resolved = _resolve_runtime_artifact(
        category='geothermal_ordinances',
        schema_path=Path('schemas/personal/geothermal_ordinance_schema.json'),
        profile_name='staging',
        repo_root=REPO_ROOT,
    )

    assert resolved is not None
    assert resolved['artifact_id'].startswith('artifact://runtime/')
    assert resolved['lineage']['profile_id'] == 'staging'
    assert resolved['lineage']['pack_name'] == 'geothermal_ordinances'


def test_resolve_runtime_artifact_matches_pack_from_schema_path() -> None:
    resolved = _resolve_runtime_artifact(
        category=None,
        schema_path=REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json',
        repo_root=REPO_ROOT,
    )

    assert resolved is not None
    assert resolved['lineage']['pack_name'] == 'geothermal_ordinances'


def test_resolve_runtime_artifact_matches_pack_from_personal_geothermal_schema_path() -> None:
    resolved = _resolve_runtime_artifact(
        category=None,
        schema_path=REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json',
        repo_root=REPO_ROOT,
    )

    assert resolved is not None
    assert resolved['lineage']['pack_name'] == 'geothermal_ordinances'


def test_resolve_consolidation_output_formats_defaults_to_both() -> None:
    assert _resolve_consolidation_output_formats() == ['csv', 'excel']


def test_format_runtime_artifact_summary_returns_schema_only_without_artifact() -> None:
    assert _format_runtime_artifact_summary(None) == (
        'schema-only (no pack/profile runtime artifact resolved)'
    )


def test_format_runtime_artifact_summary_includes_pack_profile_and_artifact_suffix() -> None:
    summary = _format_runtime_artifact_summary(
        {
            'artifact_id': 'artifact://runtime/abc123def4567890',
            'lineage': {
                'pack_name': 'geothermal_ordinances',
                'profile_id': 'staging',
            },
        }
    )

    assert summary == 'pack=geothermal_ordinances, profile=staging, artifact=abc123def4567890'


def test_validate_runtime_cli_json_reports_ready_for_schema() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--profile',
            'prod',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['resolved']['pack_name'] == 'tariffs'
    assert payload['resolved']['profile_name'] == 'prod'
    assert payload['resolved']['schema_file_path'].endswith(
        'schemas/personal/electricity_tariff_schema.json'
    )
    assert payload['resolved']['main_data_array'] == 'rate_schedules'
    assert payload['resolved']['identifier_fields'] == [
        'utility_info.utility_name',
        'utility_info.state',
    ]
    assert {check['name'] for check in payload['checks']} >= {
        'context_object_paths',
        'identifier_field_paths',
        'consolidation_paths',
        'qaqc_lane_paths',
    }


def test_validate_runtime_cli_json_reports_ready_for_geothermal_repo_pack() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--profile',
            'default',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['resolved']['pack_name'] == 'geothermal_ordinances'
    assert payload['resolved']['profile_name'] == 'default'
    assert {check['name'] for check in payload['checks']} >= {
        'context_object_paths',
        'identifier_field_paths',
        'consolidation_paths',
        'qaqc_lane_paths',
    }


def test_validate_runtime_cli_json_reports_ready_for_air_quality_repo_pack() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/air_quality_permits_schema.json'),
            '--profile',
            'default',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['resolved']['pack_name'] == 'aq_permits'
    assert payload['resolved']['profile_name'] == 'default'
    assert {check['name'] for check in payload['checks']} >= {
        'context_object_paths',
        'identifier_field_paths',
        'consolidation_paths',
        'qaqc_lane_paths',
    }


def test_validate_runtime_cli_json_reports_ready_for_solar_repo_pack() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
            '--profile',
            'default',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['resolved']['pack_name'] == 'solar'
    assert payload['resolved']['profile_name'] == 'default'
    assert {check['name'] for check in payload['checks']} >= {
        'context_object_paths',
        'identifier_field_paths',
        'consolidation_paths',
        'qaqc_lane_paths',
    }


def test_validate_runtime_cli_json_reports_actionable_error_for_invalid_pack(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'

    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            'schema_path: schemas/personal/electricity_tariff_schema.json\n'
            'modules: []\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert payload['error']['category'] == 'runtime_validation'
    assert "non-empty 'modules' list" in payload['error']['message']


def test_validate_runtime_cli_json_reports_missing_schema_file_error(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'

    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            f"schema_path: {(tmp_path / 'schemas/personal/missing.json').as_posix()}\n"
            'modules:\n'
            '  - module_id: mapper\n'
            '    enabled: true\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'existing schema file' in payload['error']['message']


def test_validate_runtime_cli_json_reports_schema_metadata_error(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'
    schema_path = tmp_path / 'schemas/personal/broken.json'

    _write_file(
        schema_path,
        '{"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}}',
    )
    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            f'schema_path: {schema_path.as_posix()}\n'
            'modules:\n'
            '  - module_id: mapper\n'
            '    enabled: true\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'runtime metadata validation' in payload['error']['message']


def test_validate_runtime_cli_json_reports_identifier_path_error(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'
    schema_path = tmp_path / 'schemas/personal/broken.json'

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.missing"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            f'schema_path: {schema_path.as_posix()}\n'
            'modules:\n'
            '  - module_id: mapper\n'
            '    enabled: true\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'Schema path could not be resolved' in payload['error']['message']


def test_validate_runtime_cli_json_reports_qaqc_lane_field_error(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'
    schema_path = tmp_path / 'schemas/personal/broken.json'

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {'
            '    "name": {"type": "string"},'
            '    "value": {"type": "number"}'
            '  }}}'
            '}'
            '}'
        ),
    )
    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            f'schema_path: {schema_path.as_posix()}\n'
            'qaqc:\n'
            '  default_lane: quantitative\n'
            '  lanes:\n'
            '    quantitative:\n'
            '      enabled: true\n'
            '      record_matching:\n'
            '        key_fields:\n'
            '          - missing_field\n'
            '      comparison:\n'
            '        primary_fields:\n'
            '          - value\n'
            'modules:\n'
            '  - module_id: mapper\n'
            '    enabled: true\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'could not be resolved against the schema' in payload['error']['message']


def test_validate_runtime_cli_json_reports_consolidation_output_error(tmp_path) -> None:
    pack_path = tmp_path / 'broken/pack.yaml'
    profile_path = tmp_path / 'profiles/default.profile.json'
    schema_path = tmp_path / 'schemas/personal/broken.json'

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}, "jurisdiction": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "number"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        pack_path,
        (
            'name: broken\n'
            'version: 1.0.0\n'
            f'schema_path: {schema_path.as_posix()}\n'
            'consolidation:\n'
            '  output:\n'
            '    exclude_fields:\n'
            '      - Missing Column\n'
            'modules:\n'
            '  - module_id: mapper\n'
            '    enabled: true\n'
        ),
    )
    _write_file(
        profile_path,
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'validate-runtime',
            '--pack',
            str(pack_path),
            '--profile',
            str(profile_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'output.exclude_fields entry could not be resolved' in payload['error']['message']


def test_init_domain_pack_cli_creates_pack_and_validates_runtime(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'sample_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['created']['pack_name'] == 'sample_domain'
    assert payload['readiness']['status'] == 'ready'
    pack_path = Path(payload['created']['pack_path'])
    assert pack_path.exists()
    pack_text = pack_path.read_text(encoding='utf-8')
    assert 'name: sample_domain' in pack_text
    assert 'schema_path: schemas/personal/electricity_tariff_schema.json' in pack_text
    assert 'consolidation:' in pack_text
    assert 'qaqc:' in pack_text
    assert 'module_id: value_semantics_classifier' in pack_text
    assert 'nested_array: charges' in pack_text
    assert 'default_format: excel' in pack_text


def test_init_domain_schema_cli_creates_lean_starter_from_reference(tmp_path) -> None:
    reference_schema_path = tmp_path / 'reference_schema.json'
    _write_json(
        reference_schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Reference Domain',
                'version': '3.4.5',
                'description': 'Detailed reference schema.',
                'extraction': {
                    'main_data_array': 'items',
                    'identifier_fields': ['jurisdiction.county', 'jurisdiction.state'],
                    'context_objects': ['jurisdiction'],
                    'display_name_template': '{jurisdiction.county}',
                    'document_type': 'Reference Permit',
                    'normalization': {'trim_strings': True},
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['category', 'citation'],
                        'ignore_fields': ['notes'],
                        'strategy': 'smart',
                    }
                },
                'output': {'default_format': 'excel'},
                'validation': {'require_units': True},
            },
            'title': 'Reference Schema',
            'description': 'Verbose reference schema.',
            'type': 'object',
            'properties': {
                'jurisdiction': {
                    'type': 'object',
                    'properties': {
                        'county': {'type': 'string', 'default': 'Ada'},
                        'state': {'type': 'string', 'enum': ['ID', 'WA']},
                    },
                },
                'items': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'category': {'type': 'string', 'examples': ['setback']},
                            'citation': {'type': 'string'},
                            'value': {'type': 'string', 'default': '100'},
                        },
                    },
                },
            },
        },
    )

    runner = CliRunner()
    output_path = tmp_path / 'schemas/personal/starter_domain_schema.json'
    result = runner.invoke(
        cli,
        [
            'init-domain-schema',
            '--name',
            'starter_domain',
            '--reference-schema',
            str(reference_schema_path),
            '--domain',
            'Starter Domain',
            '--document-type',
            'Starter Permit',
            '--output',
            str(output_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['created']['schema_name'] == 'starter_domain'
    assert payload['created']['main_data_array'] == 'items'
    assert payload['created']['identifier_fields'] == ['jurisdiction.county', 'jurisdiction.state']

    starter_schema = json.loads(output_path.read_text(encoding='utf-8'))
    metadata = starter_schema['$metadata']
    assert metadata['domain'] == 'Starter Domain'
    assert metadata['version'] == '0.1.0'
    assert metadata['extraction']['document_type'] == 'Starter Permit'
    assert metadata['consolidation']['deduplication'] == {
        'key_fields': ['category', 'citation'],
        'ignore_fields': ['notes'],
    }
    assert 'normalization' not in metadata['extraction']
    assert 'output' not in metadata
    assert 'validation' not in metadata
    assert 'strategy' not in metadata['consolidation']['deduplication']
    assert starter_schema['title'] == 'Starter Permit Starter Schema'
    assert 'examples' not in json.dumps(starter_schema)
    assert 'enum' not in json.dumps(starter_schema)
    assert 'default' not in json.dumps(starter_schema)


def test_init_domain_schema_cli_starter_feeds_init_domain_pack(tmp_path) -> None:
    runner = CliRunner()
    starter_schema_path = tmp_path / 'schemas/personal/solar_starter_schema.json'

    starter_result = runner.invoke(
        cli,
        [
            'init-domain-schema',
            '--name',
            'solar_starter',
            '--reference-schema',
            str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
            '--output',
            str(starter_schema_path),
            '--report-format',
            'json',
        ],
    )

    assert starter_result.exit_code == 0
    starter_payload = json.loads(starter_result.output)
    assert starter_payload['status'] == 'ready'

    pack_result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'solar_starter_runtime',
            '--schema',
            str(starter_schema_path),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--report-format',
            'json',
        ],
    )

    assert pack_result.exit_code == 0
    pack_payload = json.loads(pack_result.output)
    assert pack_payload['status'] == 'ready'
    assert pack_payload['readiness']['status'] == 'ready'
    pack_path = Path(pack_payload['created']['pack_path'])
    pack_text = pack_path.read_text(encoding='utf-8')
    assert 'name: solar_starter_runtime' in pack_text
    assert str(starter_schema_path) in pack_text


def test_init_domain_schema_cli_does_not_inherit_reference_domain_labels(tmp_path) -> None:
    runner = CliRunner()
    starter_schema_path = tmp_path / 'schemas/personal/sec_filings_schema.json'

    starter_result = runner.invoke(
        cli,
        [
            'init-domain-schema',
            '--name',
            'sec_filings',
            '--reference-schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--output',
            str(starter_schema_path),
            '--report-format',
            'json',
        ],
    )

    assert starter_result.exit_code == 0
    starter_schema = json.loads(starter_schema_path.read_text(encoding='utf-8'))
    assert starter_schema['$metadata']['domain'] == 'Sec Filings'
    assert starter_schema['$metadata']['extraction']['document_type'] == 'Sec Filings'
    assert starter_schema['title'] == 'Sec Filings Starter Schema'

    pack_result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'sec_filings',
            '--schema',
            str(starter_schema_path),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert pack_result.exit_code == 0
    pack_path = tmp_path / 'domain_packs/sec_filings/pack.yaml'
    readme_path = tmp_path / 'config/sec_filings/README.md'
    csv_path = tmp_path / 'config/sec_filings/page_ranges.csv'
    assert 'document_type: Sec Filings' in pack_path.read_text(encoding='utf-8')
    assert 'Document Type: Sec Filings' in readme_path.read_text(encoding='utf-8')
    assert 'Domain: Sec Filings' in readme_path.read_text(encoding='utf-8')
    assert 'Geothermal Ordinance' not in readme_path.read_text(encoding='utf-8')
    assert 'Energy - Geothermal Regulations' not in readme_path.read_text(encoding='utf-8')
    assert 'sec_filing.html' in csv_path.read_text(encoding='utf-8')


def test_init_domain_schema_cli_can_limit_main_array_fields(tmp_path) -> None:
    reference_schema_path = tmp_path / 'reference_schema.json'
    _write_json(
        reference_schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Reference Domain',
                'version': '1.0.0',
                'extraction': {
                    'main_data_array': 'requirements',
                    'identifier_fields': ['jurisdiction.county'],
                    'context_objects': ['jurisdiction'],
                    'document_type': 'Reference Ordinance',
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['feature', 'citation'],
                        'ignore_fields': ['notes'],
                    }
                },
            },
            'type': 'object',
            'properties': {
                'jurisdiction': {
                    'type': 'object',
                    'properties': {'county': {'type': 'string'}},
                },
                'requirements': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'required': ['feature', 'citation', 'value'],
                        'properties': {
                            'feature': {'type': 'string'},
                            'citation': {'type': 'string'},
                            'value': {'type': 'string'},
                            'units': {'type': 'string'},
                            'notes': {'type': 'string'},
                        },
                    },
                },
            },
        },
    )

    runner = CliRunner()
    output_path = tmp_path / 'schemas/personal/trimmed_schema.json'
    result = runner.invoke(
        cli,
        [
            'init-domain-schema',
            '--name',
            'trimmed_domain',
            '--reference-schema',
            str(reference_schema_path),
            '--include-field',
            'feature',
            '--include-field',
            'value',
            '--output',
            str(output_path),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['created']['selected_fields'] == ['feature', 'value']

    starter_schema = json.loads(output_path.read_text(encoding='utf-8'))
    requirement_properties = starter_schema['properties']['requirements']['items']['properties']
    assert list(requirement_properties.keys()) == ['feature', 'value']
    assert starter_schema['properties']['requirements']['items']['required'] == ['feature', 'value']
    assert starter_schema['$metadata']['consolidation']['deduplication']['key_fields'] == ['feature']
    assert 'ignore_fields' not in starter_schema['$metadata']['consolidation']['deduplication']


def test_init_domain_pack_cli_scaffolds_flat_qaqc_and_consolidation(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'flat_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    pack_path = Path(payload['created']['pack_path'])
    pack_text = pack_path.read_text(encoding='utf-8')
    assert 'schema_path: schemas/personal/geothermal_ordinance_schema.json' in pack_text
    assert 'projection:' not in pack_text
    assert 'key_fields:' in pack_text
    assert 'default_format:' not in pack_text
    assert 'comparison_mode: exact' not in pack_text
    assert 'comparison_mode:' not in pack_text
    assert 'strategy:' not in pack_text
    assert 'default_lane: quantitative' in pack_text
    assert 'primary_fields:' in pack_text
    assert '      - value' in pack_text


def test_init_domain_pack_cli_reports_existing_pack_error(tmp_path) -> None:
    output_root = tmp_path / 'domain_packs'
    pack_path = output_root / 'sample_domain/pack.yaml'
    _write_file(pack_path, 'name: sample_domain\nversion: 1.0.0\n')

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'sample_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(output_root),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert payload['error']['category'] == 'domain_pack_init'
    assert 'already exists' in payload['error']['message']


def test_init_domain_pack_cli_requires_name_and_schema_without_interactive(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert payload['error']['category'] == 'domain_pack_init'
    assert 'requires --name' in payload['error']['message']


def test_init_domain_pack_cli_can_scaffold_profile_and_validate_pair(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'paired_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-profile',
            '--profile-name',
            'sandbox',
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['created']['profile_path'] is not None
    assert payload['readiness']['resolved']['profile_name'] == 'sandbox'

    profile_path = Path(payload['created']['profile_path'])
    assert profile_path.exists()
    profile_payload = json.loads(profile_path.read_text(encoding='utf-8'))
    assert profile_payload['profile_id'] == 'sandbox'
    assert profile_payload['environment'] == 'sandbox'
    assert profile_payload['runtime']['emit_lineage'] is True


def test_init_domain_pack_cli_reports_existing_profile_error(tmp_path) -> None:
    profiles_root = tmp_path / 'profiles'
    _write_file(
        profiles_root / 'sandbox.profile.json',
        '{"profile_id": "sandbox", "environment": "sandbox", "runtime": {"emit_lineage": true}}',
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'paired_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-profile',
            '--profile-name',
            'sandbox',
            '--profiles-root',
            str(profiles_root),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert payload['error']['category'] == 'domain_pack_init'
    assert 'Profile already exists' in payload['error']['message']


def test_init_domain_pack_cli_can_scaffold_profile_tiers(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'tiered_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profile-tiering',
            '--profile',
            'staging',
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert payload['readiness']['resolved']['profile_name'] == 'staging'
    assert len(payload['created']['profile_paths']) == 3

    created_profiles = {Path(path).name: json.loads(Path(path).read_text(encoding='utf-8')) for path in payload['created']['profile_paths']}
    assert sorted(created_profiles) == ['dev.profile.json', 'prod.profile.json', 'staging.profile.json']
    assert created_profiles['dev.profile.json']['runtime']['debug_logging'] is True
    assert 'debug_logging' not in created_profiles['staging.profile.json']['runtime']


def test_init_domain_pack_cli_rejects_conflicting_profile_scaffold_modes(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'tiered_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-profile',
            '--profile-tiering',
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'Specify only one of --with-profile or --profile-tiering' in payload['error']['message']


def test_init_domain_pack_cli_can_scaffold_workspace_folders(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: REPO_ROOT)

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'workspace_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'workspace',
            '--with-workspace',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert len(payload['created']['workspace_paths']) == 3

    expected_paths = [
        REPO_ROOT / 'documents/workspace_domain',
        REPO_ROOT / 'processed/workspace_domain',
        REPO_ROOT / 'consolidated/workspace_domain',
    ]
    for expected_path in expected_paths:
        assert expected_path.exists()
        assert expected_path.is_dir()


def test_init_domain_pack_cli_workspace_scaffold_is_idempotent(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: REPO_ROOT)

    for existing_path in (
        REPO_ROOT / 'documents/workspace_domain',
        REPO_ROOT / 'processed/workspace_domain',
        REPO_ROOT / 'consolidated/workspace_domain',
    ):
        existing_path.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'workspace_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'workspace',
            '--with-workspace',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert len(payload['created']['workspace_paths']) == 3


def test_init_domain_pack_cli_can_scaffold_sample_assets(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: tmp_path)
    monkeypatch.setattr(
        'streamline_extract.cli.utils_commands.build_runtime_readiness_report',
        lambda **_: {
            'status': 'ready',
            'resolved': {
                'pack_name': 'sample_domain',
                'profile_name': 'sample',
                'artifact_id': 'artifact://tests/sample-assets',
                'schema_path': str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
                'schema_file_path': str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
                'main_data_array': 'rate_schedules',
                'identifier_fields': ['utility_info.utility_name', 'utility_info.state'],
                'pack_path': str(tmp_path / 'domain_packs/sample_domain/pack.yaml'),
                'profile_path': str(tmp_path / 'profiles/sample.profile.json'),
                'enabled_module_ids': ['value_semantics_classifier'],
            },
            'checks': [],
        },
    )

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'sample_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'sample',
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--with-sample-assets',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert len(payload['created']['sample_asset_paths']) == 3

    readme_path = tmp_path / 'documents/sample_domain/README.md'
    manifest_path = tmp_path / 'documents/sample_domain/sample_manifest.csv'
    assert readme_path.exists()
    assert manifest_path.exists()

    readme_text = readme_path.read_text(encoding='utf-8')
    manifest_text = manifest_path.read_text(encoding='utf-8')
    assert '# sample_domain Source Documents' in readme_text
    assert 'Document Type: Utility Tariff' in readme_text
    assert 'Supported extensions:' in readme_text
    assert 'config/sample_domain/page_ranges.csv' in readme_text
    assert '--max-context 1400000' not in readme_text
    assert 'file_name,notes' in manifest_text
    assert 'utility_tariff.pdf' in manifest_text

    assert payload['next_steps'][0]['title'] == 'Review scaffolded sample assets'
    assert 'sample_manifest.csv' in payload['next_steps'][0]['detail']


def test_init_domain_pack_cli_sample_assets_require_force_to_overwrite(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: tmp_path)
    _write_file(tmp_path / 'documents/sample_domain/README.md', 'existing sample asset readme\n')

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'sample_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'sample',
            '--with-sample-assets',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'Scaffold file already exists' in payload['error']['message']


def test_init_domain_pack_cli_can_scaffold_config_files(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'config_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'config',
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['status'] == 'ready'
    assert len(payload['created']['config_paths']) == 4

    readme_path = tmp_path / 'config/config_domain/README.md'
    csv_path = tmp_path / 'config/config_domain/page_ranges.csv'
    run_config_path = tmp_path / 'config/config_domain/run.yaml'
    assert readme_path.exists()
    assert csv_path.exists()
    assert run_config_path.exists()
    assert '# config_domain Configuration' in readme_path.read_text(encoding='utf-8')
    assert 'Document Type: Utility Tariff' in readme_path.read_text(encoding='utf-8')
    assert 'Domain: Energy - Utility Tariffs' in readme_path.read_text(encoding='utf-8')
    assert 'run.yaml: Starter runtime config for acquire/process/consolidate commands' in readme_path.read_text(encoding='utf-8')
    assert 'documents/config_domain/' in readme_path.read_text(encoding='utf-8')
    assert 'file_path,start_page,end_page' in csv_path.read_text(encoding='utf-8')
    assert 'utility_tariff.pdf' in csv_path.read_text(encoding='utf-8')
    assert 'processing:' in run_config_path.read_text(encoding='utf-8')
    assert 'consolidation:' in run_config_path.read_text(encoding='utf-8')
    assert 'schemas/personal/electricity_tariff_schema.json' in run_config_path.read_text(encoding='utf-8')
    assert '--max-context 1400000' not in readme_path.read_text(encoding='utf-8')
    assert '--enable-qa-qc' not in readme_path.read_text(encoding='utf-8')
    assert 'processed/config_domain/qa_qc' not in readme_path.read_text(encoding='utf-8')
    assert '--qaqc-lane qualitative' not in readme_path.read_text(encoding='utf-8')

    next_steps = payload['next_steps']
    step_titles = [step['title'] for step in next_steps]
    assert step_titles == [
        'Add source documents',
        'Validate runtime seam',
        'Process documents',
        'Consolidate extracted records',
    ]
    assert next_steps[0]['detail'] == 'Place source files under documents/config_domain/ before the first run.'
    assert next_steps[1]['command'].startswith('pixi run streamline-extract validate-runtime --pack ')
    assert '--profile' in next_steps[1]['command']
    assert next_steps[2]['command'] == (
        'pixi run streamline-extract process documents/config_domain/ '
        '--schema schemas/personal/electricity_tariff_schema.json '
        f'--profile {payload["created"]["profile_path"]} '
        f'--pages-csv {(tmp_path / "config/config_domain/page_ranges.csv").as_posix()}'
    )
    assert next_steps[3]['command'] == (
        'pixi run streamline-extract consolidate processed/config_domain '
        '--schema schemas/personal/electricity_tariff_schema.json'
    )


def test_init_domain_pack_cli_scaffolds_domain_aware_config_content(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'geo_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'geo',
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    readme_path = tmp_path / 'config/geo_domain/README.md'
    csv_path = tmp_path / 'config/geo_domain/page_ranges.csv'
    run_config_path = tmp_path / 'config/geo_domain/run.yaml'
    assert 'Document Type: Geothermal Ordinance' in readme_path.read_text(encoding='utf-8')
    assert 'Domain: Energy - Geothermal Regulations' in readme_path.read_text(encoding='utf-8')
    assert 'schemas/personal/geothermal_ordinance_schema.json' in readme_path.read_text(encoding='utf-8')
    assert 'geothermal_ordinance.pdf' in csv_path.read_text(encoding='utf-8')
    assert 'schemas/personal/geothermal_ordinance_schema.json' in run_config_path.read_text(encoding='utf-8')
    assert 'Optional QA/QC workflow:' not in readme_path.read_text(encoding='utf-8')
    assert 'processed/geo_domain/qa_qc' not in readme_path.read_text(encoding='utf-8')
    assert '--qaqc-lane qualitative' not in readme_path.read_text(encoding='utf-8')


def test_init_domain_pack_cli_scaffolds_solar_domain_aware_config_content(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'solar_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    readme_path = tmp_path / 'config/solar_domain/README.md'
    csv_path = tmp_path / 'config/solar_domain/page_ranges.csv'
    run_config_path = tmp_path / 'config/solar_domain/run.yaml'
    assert 'Document Type: Solar Ordinance' in readme_path.read_text(encoding='utf-8')
    assert 'Domain: Energy - Solar Regulations' in readme_path.read_text(encoding='utf-8')
    assert 'schemas/personal/solar_ordinance_schema.json' in readme_path.read_text(encoding='utf-8')
    assert 'solar_ordinance.pdf' in csv_path.read_text(encoding='utf-8')
    assert 'schemas/personal/solar_ordinance_schema.json' in run_config_path.read_text(encoding='utf-8')
    assert '--qaqc-lane qualitative' not in readme_path.read_text(encoding='utf-8')


def test_init_domain_pack_cli_prefills_page_ranges_from_existing_documents(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    repo_root = tmp_path
    documents_dir = repo_root / 'documents' / 'existing_docs_domain'
    documents_dir.mkdir(parents=True, exist_ok=True)
    _write_file(documents_dir / 'alpha.pdf', '')
    _write_file(documents_dir / 'beta.docx', '')
    _write_file(documents_dir / 'notes.md', '')

    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        'streamline_extract.cli.utils_commands.build_runtime_readiness_report',
        lambda **_: {
            'status': 'ready',
            'resolved': {
                'pack_name': 'existing_docs_domain',
                'profile_name': 'default',
                'artifact_id': 'artifact://tests/existing-docs-domain',
                'schema_path': str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
                'schema_file_path': str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
                'main_data_array': 'requirements',
                'identifier_fields': ['jurisdiction.state', 'jurisdiction.county'],
                'pack_path': str(tmp_path / 'domain_packs/existing_docs_domain/pack.yaml'),
                'profile_path': str(tmp_path / 'profiles/default.profile.json'),
                'enabled_module_ids': ['value_semantics_classifier'],
            },
            'checks': [],
        },
    )

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'existing_docs_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/solar_ordinance_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    csv_path = tmp_path / 'config/existing_docs_domain/page_ranges.csv'
    assert csv_path.read_text(encoding='utf-8') == (
        'file_path,start_page,end_page\n'
        'alpha.pdf,,\n'
        'beta.docx,,\n'
    )


def test_validate_schema_cli_reports_nested_metadata_fields_correctly(tmp_path) -> None:
    schema_path = tmp_path / 'nested_metadata_schema.json'
    _write_file(
        schema_path,
        json.dumps(
            {
                '$schema': 'http://json-schema.org/draft-07/schema#',
                '$metadata': {
                    'extraction': {
                        'main_data_array': 'items',
                        'identifier_fields': ['jurisdiction.state', 'jurisdiction.county'],
                    },
                    'consolidation': {
                        'deduplication': {
                            'key_fields': ['name'],
                        }
                    },
                },
                'type': 'object',
                'properties': {
                    'jurisdiction': {'type': 'object'},
                    'items': {'type': 'array'},
                },
            },
            indent=2,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ['validate-schema', str(schema_path)])

    assert result.exit_code == 0
    assert 'Identifier fields: jurisdiction.state, jurisdiction.county' in result.output
    assert 'Main data array: items' in result.output
    assert 'Deduplication key fields: name' in result.output
    assert "$metadata.extraction missing 'identifier_fields'" not in result.output
    assert "$metadata.extraction missing 'main_data_array'" not in result.output


def test_init_domain_pack_cli_text_output_includes_next_steps(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'guided_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
        ],
    )

    assert result.exit_code == 0
    assert 'Next Steps' in result.output
    assert '1. Add source documents' in result.output
    assert 'pixi run streamline-extract validate-runtime --pack' in result.output
    assert 'pixi run streamline-extract process documents/guided_domain/' in result.output
    assert '--schema schemas/personal/geothermal_ordinance_schema.json --profile default --pages-csv' in result.output
    assert str(tmp_path / 'config/guided_domain/page_ranges.csv') in result.output
    assert 'pixi run streamline-extract consolidate processed/guided_domain --schema schemas/personal/geothermal_ordinance_schema.json' in result.output
    assert 'Optional multi-model QA/QC run' not in result.output
    assert '--enable-qa-qc' not in result.output
    assert 'processed/guided_domain/qa_qc' not in result.output


def test_run_qaqc_extraction_prints_compare_command_with_selected_lane(tmp_path, monkeypatch, capsys) -> None:
    document_path = tmp_path / 'documents' / 'qa_doc.pdf'
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text('placeholder', encoding='utf-8')

    class _DummyConfig:
        llm_config = {'azure_endpoint': None, 'azure_api_version': None}

    class _DummyResult:
        def __init__(self, cost: float, processing_time: float, success: bool = True):
            self.cost = cost
            self.processing_time = processing_time
            self.success = success

    monkeypatch.setattr('streamline_extract.qa_qc.ModelDetector.get_qa_models', lambda: ['model-a', 'model-b'])
    monkeypatch.setattr('streamline_extract.qa_qc.ModelDetector.get_provider', lambda: 'openai')
    monkeypatch.setattr('streamline_extract.extraction.document_utils.extract_text_from_document', lambda *args, **kwargs: 'doc text')
    monkeypatch.setattr(
        'streamline_extract.qa_qc.run_multi_model_extraction',
        lambda **kwargs: {
            'model-a': _DummyResult(cost=0.1, processing_time=1.0),
            'model-b': _DummyResult(cost=0.2, processing_time=1.5),
        },
    )
    monkeypatch.setattr('streamline_extract.cli.commands.ask_confirm', lambda *args, **kwargs: True)

    _run_qa_qc_extraction(
        doc_files=[document_path],
        loaded_schema={'type': 'object'},
        schema_path=REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json',
        output_dir=tmp_path / 'processed',
        api_key='test-key',
        provider='openai',
        config=_DummyConfig(),
        max_context=400000,
        page_range_map={},
        verbosity='normal',
        runtime_artifact=None,
        run_id='run://test',
        qaqc_lane='qualitative',
    )

    captured = capsys.readouterr().out
    # The selected lane is shown in the configuration snapshot (rendered as a
    # key/value table by the shared design system) and echoed in the follow-up
    # compare command.
    assert 'QA/QC Lane' in captured and 'qualitative' in captured
    assert 'pixi run streamline-extract compare' in captured
    assert '--qaqc-lane qualitative' in captured


def test_init_domain_pack_cli_interactive_mode_prompts_for_missing_inputs(tmp_path) -> None:
    runner = CliRunner()
    output_root = tmp_path / 'domain_packs'
    profiles_root = tmp_path / 'profiles'
    config_root = tmp_path / 'config'

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--interactive',
        ],
        input=(
            'interactive_domain\n'
            f'{(REPO_ROOT / "schemas/personal/electricity_tariff_schema.json").as_posix()}\n'
            'n\n'
            'single\n'
            'sandbox\n'
            'n\n'
            'y\n'
            'n\n'
            'recommended\n'
            f'{output_root.as_posix()}\n'
            f'{profiles_root.as_posix()}\n'
            f'{config_root.as_posix()}\n'
        ),
    )

    assert result.exit_code == 0
    assert 'Interactive Domain Pack Setup' in result.output
    assert 'Input mode' in result.output
    assert 'interactive prompts resolved missing onboarding options' in result.output
    assert output_root.joinpath('interactive_domain/pack.yaml').exists()
    assert profiles_root.joinpath('sandbox.profile.json').exists()
    assert config_root.joinpath('interactive_domain/README.md').exists()
    assert '--max-context 1400000' in config_root.joinpath('interactive_domain/README.md').read_text(encoding='utf-8')
    assert str(output_root) in result.output
    assert str(profiles_root) in result.output


def test_init_domain_pack_cli_defaults_to_minimal_template_mode(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'default_minimal_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['created']['template_mode'] == 'minimal'
    assert [step['title'] for step in payload['next_steps']] == [
        'Add source documents',
        'Validate runtime seam',
        'Process documents',
        'Consolidate extracted records',
    ]

    readme_text = (tmp_path / 'config/default_minimal_domain/README.md').read_text(encoding='utf-8')
    assert 'Recommended flags:' not in readme_text
    assert 'Optional QA/QC workflow:' not in readme_text
    assert '--max-context 1400000' not in readme_text


def test_init_domain_pack_cli_interactive_mode_uses_default_roots_when_prompted(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: tmp_path)
    monkeypatch.setattr(
        'streamline_extract.cli.utils_commands.build_runtime_readiness_report',
        lambda **_: {
            'status': 'ready',
            'resolved': {
                'pack_name': 'default_roots_domain',
                'profile_name': 'default',
                'artifact_id': 'artifact://tests/default-roots',
                'schema_path': str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
                'schema_file_path': str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
                'main_data_array': 'requirements',
                'identifier_fields': ['jurisdiction.state', 'jurisdiction.county'],
                'pack_path': str(tmp_path / 'schemas/domain_packs/default_roots_domain/pack.yaml'),
                'profile_path': str(tmp_path / 'schemas/profiles/default.profile.json'),
                'enabled_module_ids': ['value_semantics_classifier'],
            },
            'checks': [],
        },
    )

    result = runner.invoke(
        cli,
        ['init-domain-pack', '--interactive'],
        input=(
            'default_roots_domain\n'
            f'{(REPO_ROOT / "schemas/personal/geothermal_ordinance_schema.json").as_posix()}\n'
            'n\n'
            'none\n'
            'n\n'
            'y\n'
            'n\n'
            '\n'
            '\n'
            '\n'
        ),
    )

    assert result.exit_code == 0
    assert (tmp_path / 'schemas/domain_packs/default_roots_domain/pack.yaml').exists()
    assert (tmp_path / 'config/default_roots_domain/README.md').exists()


def test_init_domain_pack_cli_rejects_interactive_json_mode(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--interactive',
            '--report-format',
            'json',
            '--output-root',
            str(tmp_path / 'domain_packs'),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert '--interactive only supports text output' in payload['error']['message']


def test_init_domain_pack_cli_interactive_mode_can_confirm_overwrite(tmp_path) -> None:
    runner = CliRunner()
    output_root = tmp_path / 'domain_packs'
    profiles_root = tmp_path / 'profiles'
    config_root = tmp_path / 'config'
    _write_file(output_root / 'overwrite_domain/pack.yaml', 'name: old\n')
    _write_file(profiles_root / 'sandbox.profile.json', '{"profile_id": "sandbox"}\n')
    _write_file(config_root / 'overwrite_domain/README.md', 'old readme\n')

    result = runner.invoke(
        cli,
        ['init-domain-pack', '--interactive'],
        input=(
            'overwrite_domain\n'
            f'{(REPO_ROOT / "schemas/personal/electricity_tariff_schema.json").as_posix()}\n'
            'n\n'
            'single\n'
            'sandbox\n'
            'n\n'
            'y\n'
            'n\n'
            'recommended\n'
            f'{output_root.as_posix()}\n'
            f'{profiles_root.as_posix()}\n'
            f'{config_root.as_posix()}\n'
            'y\n'
        ),
    )

    assert result.exit_code == 0
    assert 'Existing scaffold targets detected' in result.output
    assert 'Overwrite existing scaffold files?' in result.output
    assert 'schema_path: schemas/personal/electricity_tariff_schema.json' in output_root.joinpath('overwrite_domain/pack.yaml').read_text(encoding='utf-8')
    assert 'profile_id' in profiles_root.joinpath('sandbox.profile.json').read_text(encoding='utf-8')
    assert '# overwrite_domain Configuration' in config_root.joinpath('overwrite_domain/README.md').read_text(encoding='utf-8')


def test_init_domain_pack_cli_template_mode_minimal_trims_extra_guidance(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'minimal_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--with-profile',
            '--profile-name',
            'minimal',
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--template-mode',
            'minimal',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload['created']['template_mode'] == 'minimal'
    assert [step['title'] for step in payload['next_steps']] == [
        'Add source documents',
        'Validate runtime seam',
        'Process documents',
        'Consolidate extracted records',
    ]

    readme_text = (tmp_path / 'config/minimal_domain/README.md').read_text(encoding='utf-8')
    assert 'Recommended flags:' not in readme_text
    assert 'Optional QA/QC workflow:' not in readme_text
    assert '--max-context 1400000' not in readme_text
    assert '--enable-qa-qc' not in readme_text


def test_init_domain_pack_cli_interactive_mode_can_select_minimal_template(tmp_path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ['init-domain-pack', '--interactive'],
        input=(
            'interactive_minimal\n'
            f'{(REPO_ROOT / "schemas/personal/electricity_tariff_schema.json").as_posix()}\n'
            'n\n'
            'none\n'
            'n\n'
            'y\n'
            'n\n'
            'minimal\n'
            f'{(tmp_path / "domain_packs").as_posix()}\n'
            f'{(tmp_path / "config").as_posix()}\n'
        ),
    )

    assert result.exit_code == 0
    assert 'Template Mode' in result.output
    assert 'minimal' in result.output
    readme_text = (tmp_path / 'config/interactive_minimal/README.md').read_text(encoding='utf-8')
    assert 'Recommended flags:' not in readme_text
    assert 'Optional QA/QC workflow:' not in readme_text


def test_init_domain_pack_cli_interactive_mode_can_cancel_overwrite(tmp_path) -> None:
    runner = CliRunner()
    output_root = tmp_path / 'domain_packs'
    _write_file(output_root / 'overwrite_domain/pack.yaml', 'name: old\n')

    result = runner.invoke(
        cli,
        ['init-domain-pack', '--interactive'],
        input=(
            'overwrite_domain\n'
            f'{(REPO_ROOT / "schemas/personal/electricity_tariff_schema.json").as_posix()}\n'
            'n\n'
            'none\n'
            'n\n'
            'n\n'
            'n\n'
            'recommended\n'
            f'{output_root.as_posix()}\n'
            'n\n'
        ),
    )

    assert result.exit_code == 1
    assert 'Interactive onboarding cancelled because scaffold targets already exist' in result.output
    assert output_root.joinpath('overwrite_domain/pack.yaml').read_text(encoding='utf-8') == 'name: old\n'


def test_init_domain_pack_cli_interactive_mode_can_enable_sample_assets(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    output_root = tmp_path / 'domain_packs'
    config_root = tmp_path / 'config'
    monkeypatch.setattr('streamline_extract.cli.utils_commands._cli_repo_root', lambda: tmp_path)
    monkeypatch.setattr(
        'streamline_extract.cli.utils_commands.build_runtime_readiness_report',
        lambda **_: {
            'status': 'ready',
            'resolved': {
                'pack_name': 'interactive_assets',
                'profile_name': 'default',
                'artifact_id': 'artifact://tests/interactive-assets',
                'schema_path': str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
                'schema_file_path': str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
                'main_data_array': 'rate_schedules',
                'identifier_fields': ['utility_info.utility_name', 'utility_info.state'],
                'pack_path': str(output_root / 'interactive_assets/pack.yaml'),
                'profile_path': str(tmp_path / 'schemas/profiles/default.profile.json'),
                'enabled_module_ids': ['value_semantics_classifier'],
            },
            'checks': [],
        },
    )

    result = runner.invoke(
        cli,
        ['init-domain-pack', '--interactive'],
        input=(
            'interactive_assets\n'
            f'{(REPO_ROOT / "schemas/personal/electricity_tariff_schema.json").as_posix()}\n'
            'n\n'
            'none\n'
            'n\n'
            'y\n'
            'y\n'
            'recommended\n'
            f'{output_root.as_posix()}\n'
            f'{config_root.as_posix()}\n'
        ),
    )

    assert result.exit_code == 0
    assert 'Sample asset scaffold created' in result.output
    assert 'Sample Assets' in result.output
    assert (tmp_path / 'documents/interactive_assets/README.md').exists()
    assert (tmp_path / 'documents/interactive_assets/sample_manifest.csv').exists()
    assert (tmp_path / 'config/interactive_assets/page_ranges.csv').exists()
    assert (tmp_path / 'config/interactive_assets/run.yaml').exists()


def test_init_domain_pack_cli_config_scaffold_requires_force_to_overwrite(tmp_path) -> None:
    config_dir = tmp_path / 'config/config_domain'
    config_dir.mkdir(parents=True, exist_ok=True)
    _write_file(config_dir / 'README.md', 'existing readme\n')

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'init-domain-pack',
            '--name',
            'config_domain',
            '--schema',
            str(REPO_ROOT / 'schemas/personal/electricity_tariff_schema.json'),
            '--output-root',
            str(tmp_path / 'domain_packs'),
            '--profiles-root',
            str(tmp_path / 'profiles'),
            '--with-profile',
            '--profile-name',
            'config',
            '--with-config',
            '--config-root',
            str(tmp_path / 'config'),
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload['status'] == 'error'
    assert 'Scaffold file already exists' in payload['error']['message']


def test_build_dedup_preview_report_includes_duplicate_groups() -> None:
    report = _build_dedup_preview_report(
        schema_info={'type': 'Test Schema', 'main_array_key': 'items'},
        dedup_preview={
            'duplicates_removed': 2,
            'key_fields': ['name'],
            'compare_columns': ['Name'],
            'warnings': [],
            'suspicious_groups_count': 1,
            'suspicious_groups_by_severity': {'high': 1, 'medium': 0, 'low': 0},
            'suspicious_groups': [
                {
                    'keep_index': 0,
                    'drop_indices': [1, 2],
                    'severity': 'high',
                    'conflicting_columns': ['Value'],
                    'conflicts': {'Value': ['10', '12']},
                }
            ],
            'duplicate_groups': [
                {
                    'keep_index': 0,
                    'drop_indices': [1, 2],
                    'merged_count': 2,
                    'note': 'Merged 2 duplicates',
                    'sample_values': {'Name': 'Charge A'},
                    'suspicious': True,
                    'severity': 'high',
                    'conflicting_columns': ['Value'],
                    'conflicts': {'Value': ['10', '12']},
                }
            ],
        },
        rows_before_dedup=5,
    )

    assert report == {
        'schema_type': 'Test Schema',
        'main_array_key': 'items',
        'rows_before_dedup': 5,
        'rows_after_dedup': 3,
        'duplicates_removed': 2,
        'key_fields': ['name'],
        'compare_columns': ['Name'],
        'warnings': [],
        'suspicious_groups_count': 1,
        'suspicious_groups_by_severity': {'high': 1, 'medium': 0, 'low': 0},
        'suspicious_groups': [
            {
                'keep_index': 0,
                'drop_indices': [1, 2],
                'severity': 'high',
                'conflicting_columns': ['Value'],
                'conflicts': {'Value': ['10', '12']},
            }
        ],
        'duplicate_groups': [
            {
                'keep_index': 0,
                'drop_indices': [1, 2],
                'merged_count': 2,
                'note': 'Merged 2 duplicates',
                'sample_values': {'Name': 'Charge A'},
                'suspicious': True,
                'severity': 'high',
                'conflicting_columns': ['Value'],
                'conflicts': {'Value': ['10', '12']},
            }
        ],
    }


def test_should_fail_on_suspicious_respects_thresholds() -> None:
    preview_report = {
        'suspicious_groups_count': 2,
        'suspicious_groups_by_severity': {'high': 1, 'medium': 1, 'low': 0},
    }

    assert _should_fail_on_suspicious(preview_report, 'none') is False
    assert _should_fail_on_suspicious(preview_report, 'high') is True
    assert _should_fail_on_suspicious(preview_report, 'medium') is True
    assert _should_fail_on_suspicious(preview_report, 'low') is True


def test_resolve_consolidation_output_formats_honors_runtime_excel_override() -> None:
    assert _resolve_consolidation_output_formats(
        {
            'consolidation': {
                'output': {
                    'default_format': 'excel',
                }
            }
        }
    ) == ['excel']


def test_resolve_consolidation_output_formats_falls_back_for_invalid_value() -> None:
    assert _resolve_consolidation_output_formats(
        {
            'consolidation': {
                'output': {
                    'default_format': 'spreadsheet',
                }
            }
        }
    ) == ['csv', 'excel']


def test_consolidate_cli_uses_runtime_pack_default_format_for_geothermal(tmp_path) -> None:
    extracted_dir = tmp_path / 'processed/geothermal'
    output_dir = tmp_path / 'consolidated/geothermal'
    _write_json(
        extracted_dir / 'doc1.json',
        {
            'record_id': 'record-1',
            'contract_version': '1.0.0',
            'document': {
                'source_document_id': 'geo-1',
                'source_path': 'documents/geothermal/doc1.pdf',
                'source_filename': 'doc1.pdf',
            },
            'lineage': {
                'run_id': 'run://geo123',
                'artifact_id': 'artifact://runtime/geo123',
                'profile_id': 'default',
                'model': 'gpt-5',
                'provider': 'azure',
                'extracted_at': '2026-03-25T00:00:00Z',
            },
            'payload': {
                'document_applicability': {
                    'applies_to_geothermal_electricity': True,
                    'extraction_approach': 'Extracted geothermal sections',
                    'relevant_sections': 'Section 1',
                },
                'jurisdiction': {
                    'state': 'Colorado',
                    'county': 'Example County',
                    'ordinance_code': 'ORD-1',
                },
                'requirements': [
                    {
                        'category': 'Setback',
                        'specific_subject': 'from property line',
                        'applies_to': 'Geothermal power plant',
                        'value': 100,
                        'value_type': 'fixed_number',
                        'unit': 'feet',
                        'condition': None,
                        'details': 'Minimum setback is 100 feet.',
                        'section': '1.1',
                        'notes': None,
                    }
                ],
            },
            'quality': {'warnings': [], 'errors': []},
            'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'consolidate',
            str(extracted_dir),
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--output',
            str(output_dir),
            '--quiet',
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / 'geothermal.xlsx').exists()
    assert not (output_dir / 'geothermal.csv').exists()


def test_consolidate_cli_keeps_dual_output_without_runtime_override(tmp_path) -> None:
    schema_path = tmp_path / 'schema.json'
    _write_json(
        schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Test',
                'version': '1.0.0',
                'extraction': {
                    'main_data_array': 'items',
                    'context_objects': ['metadata'],
                    'identifier_fields': ['metadata.id'],
                    'document_type': 'Test Document',
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['name'],
                        'ignore_fields': [],
                    },
                    'output': {
                        'default_format': 'excel',
                    },
                },
            },
            'type': 'object',
            'properties': {
                'metadata': {'type': 'object'},
                'items': {'type': 'array'},
            },
        },
    )

    extracted_dir = tmp_path / 'processed/contracts'
    output_dir = tmp_path / 'consolidated/contracts'
    _write_json(
        extracted_dir / 'doc1.json',
        {
            'record_id': 'record-2',
            'contract_version': '1.0.0',
            'document': {
                'source_document_id': 'doc-2',
                'source_path': 'documents/contracts/doc2.pdf',
                'source_filename': 'doc2.pdf',
            },
            'lineage': {
                'run_id': 'run://contract123',
                'artifact_id': 'artifact://runtime/contract123',
                'profile_id': 'default',
                'model': 'gpt-5',
                'provider': 'azure',
                'extracted_at': '2026-03-25T00:00:00Z',
            },
            'payload': {
                'metadata': {
                    'id': 'C-1',
                    'jurisdiction': 'Example Borough',
                },
                'items': [
                    {
                        'name': 'Charge A',
                        'value': 10,
                    }
                ],
            },
            'quality': {'warnings': [], 'errors': []},
            'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'consolidate',
            str(extracted_dir),
            '--schema',
            str(schema_path),
            '--output',
            str(output_dir),
            '--quiet',
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / 'contracts.csv').exists()
    assert (output_dir / 'contracts.xlsx').exists()


def test_consolidate_cli_dry_run_previews_deduplication_without_writing_outputs(tmp_path) -> None:
    schema_path = tmp_path / 'schema.json'
    _write_json(
        schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Test',
                'version': '1.0.0',
                'extraction': {
                    'main_data_array': 'items',
                    'context_objects': ['metadata'],
                    'identifier_fields': ['metadata.id'],
                    'document_type': 'Test Document',
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['name'],
                        'ignore_fields': ['notes'],
                    }
                },
            },
            'type': 'object',
            'properties': {
                'metadata': {'type': 'object'},
                'items': {'type': 'array'},
            },
        },
    )

    extracted_dir = tmp_path / 'processed/contracts'
    output_dir = tmp_path / 'consolidated/contracts'
    shared_payload = {
        'metadata': {'id': 'C-1', 'jurisdiction': 'Example Borough'},
        'items': [
            {
                'name': 'Charge A',
                'value': 10,
                'notes': 'first copy',
            }
        ],
    }
    _write_json(
        extracted_dir / 'doc1.json',
        {
            'record_id': 'record-2',
            'contract_version': '1.0.0',
            'document': {
                'source_document_id': 'doc-2',
                'source_path': 'documents/contracts/doc2.pdf',
                'source_filename': 'doc2.pdf',
            },
            'lineage': {
                'run_id': 'run://contract123',
                'artifact_id': 'artifact://runtime/contract123',
                'profile_id': 'default',
                'model': 'gpt-5',
                'provider': 'azure',
                'extracted_at': '2026-03-25T00:00:00Z',
            },
            'payload': shared_payload,
            'quality': {'warnings': [], 'errors': []},
            'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
        },
    )
    _write_json(
        extracted_dir / 'doc2.json',
        {
            'record_id': 'record-3',
            'contract_version': '1.0.0',
            'document': {
                'source_document_id': 'doc-3',
                'source_path': 'documents/contracts/doc3.pdf',
                'source_filename': 'doc3.pdf',
            },
            'lineage': {
                'run_id': 'run://contract124',
                'artifact_id': 'artifact://runtime/contract124',
                'profile_id': 'default',
                'model': 'gpt-5',
                'provider': 'azure',
                'extracted_at': '2026-03-25T00:00:00Z',
            },
            'payload': shared_payload,
            'quality': {'warnings': [], 'errors': []},
            'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'consolidate',
            str(extracted_dir),
            '--schema',
            str(schema_path),
            '--output',
            str(output_dir),
            '--dry-run',
        ],
    )

    assert result.exit_code == 0
    assert 'Deduplication Preview' in result.output
    assert 'Duplicates Removed' in result.output
    assert 'Dry run complete - no CSV/Excel files were written' in result.output
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_consolidate_cli_dry_run_json_report_is_machine_readable(tmp_path) -> None:
    schema_path = tmp_path / 'schema.json'
    _write_json(
        schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Test',
                'version': '1.0.0',
                'extraction': {
                    'main_data_array': 'items',
                    'context_objects': ['metadata'],
                    'identifier_fields': ['metadata.id'],
                    'document_type': 'Test Document',
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['name'],
                        'ignore_fields': ['notes'],
                    }
                },
            },
            'type': 'object',
            'properties': {
                'metadata': {'type': 'object'},
                'items': {'type': 'array'},
            },
        },
    )

    extracted_dir = tmp_path / 'processed/contracts'
    output_dir = tmp_path / 'consolidated/contracts'
    shared_payload = {
        'metadata': {'id': 'C-1', 'jurisdiction': 'Example Borough'},
        'items': [
            {
                'name': 'Charge A',
                'value': 10,
                'notes': 'first copy',
            }
        ],
    }
    conflicting_payload = {
        'metadata': {'id': 'C-2', 'jurisdiction': 'Example Borough'},
        'items': [
            {
                'name': 'Charge A',
                'value': 12,
                'notes': 'second copy',
            }
        ],
    }
    for filename, record_id in [('doc1.json', 'record-2'), ('doc2.json', 'record-3')]:
        _write_json(
            extracted_dir / filename,
            {
                'record_id': record_id,
                'contract_version': '1.0.0',
                'document': {
                    'source_document_id': record_id,
                    'source_path': f'documents/contracts/{filename.replace(".json", ".pdf")}',
                    'source_filename': filename.replace('.json', '.pdf'),
                },
                'lineage': {
                    'run_id': f'run://{record_id}',
                    'artifact_id': f'artifact://runtime/{record_id}',
                    'profile_id': 'default',
                    'model': 'gpt-5',
                    'provider': 'azure',
                    'extracted_at': '2026-03-25T00:00:00Z',
                },
                'payload': shared_payload if filename == 'doc1.json' else conflicting_payload,
                'quality': {'warnings': [], 'errors': []},
                'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
            },
        )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'consolidate',
            str(extracted_dir),
            '--schema',
            str(schema_path),
            '--output',
            str(output_dir),
            '--dry-run',
            '--report-format',
            'json',
        ],
    )

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report['schema_type'] == 'Test Document'
    assert report['main_array_key'] == 'items'
    assert report['rows_before_dedup'] == 2
    assert report['rows_after_dedup'] == 1
    assert report['duplicates_removed'] == 1
    assert report['suspicious_groups_count'] == 1
    assert report['suspicious_groups_by_severity'] == {'high': 1, 'medium': 0, 'low': 0}
    assert report['key_fields'] == ['name']
    assert report['compare_columns'] == ['Name']
    assert len(report['duplicate_groups']) == 1
    assert report['duplicate_groups'][0]['suspicious'] is True
    assert report['duplicate_groups'][0]['severity'] == 'high'
    assert report['duplicate_groups'][0]['conflicting_columns'] == ['Value']
    assert report['warnings']
    assert report['fail_on_suspicious'] == 'none'
    assert report['would_fail_on_suspicious'] is False
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_consolidate_cli_dry_run_json_report_can_fail_on_suspicious_threshold(tmp_path) -> None:
    schema_path = tmp_path / 'schema.json'
    _write_json(
        schema_path,
        {
            '$schema': 'http://json-schema.org/draft-07/schema#',
            '$metadata': {
                'domain': 'Test',
                'version': '1.0.0',
                'extraction': {
                    'main_data_array': 'items',
                    'context_objects': ['metadata'],
                    'identifier_fields': ['metadata.id'],
                    'document_type': 'Test Document',
                },
                'consolidation': {
                    'deduplication': {
                        'key_fields': ['name'],
                        'ignore_fields': ['notes'],
                    }
                },
            },
            'type': 'object',
            'properties': {
                'metadata': {'type': 'object'},
                'items': {'type': 'array'},
            },
        },
    )

    extracted_dir = tmp_path / 'processed/contracts'
    output_dir = tmp_path / 'consolidated/contracts'
    payloads = [
        {
            'metadata': {'id': 'C-1', 'jurisdiction': 'Example Borough'},
            'items': [{'name': 'Charge A', 'value': 10, 'notes': 'first copy'}],
        },
        {
            'metadata': {'id': 'C-2', 'jurisdiction': 'Example Borough'},
            'items': [{'name': 'Charge A', 'value': 12, 'notes': 'second copy'}],
        },
    ]
    for index, payload in enumerate(payloads, start=1):
        _write_json(
            extracted_dir / f'doc{index}.json',
            {
                'record_id': f'record-{index}',
                'contract_version': '1.0.0',
                'document': {
                    'source_document_id': f'doc-{index}',
                    'source_path': f'documents/contracts/doc{index}.pdf',
                    'source_filename': f'doc{index}.pdf',
                },
                'lineage': {
                    'run_id': f'run://record-{index}',
                    'artifact_id': f'artifact://runtime/record-{index}',
                    'profile_id': 'default',
                    'model': 'gpt-5',
                    'provider': 'azure',
                    'extracted_at': '2026-03-25T00:00:00Z',
                },
                'payload': payload,
                'quality': {'warnings': [], 'errors': []},
                'processing_metrics': {'duration_seconds': 1.0, 'cost_usd': 0.01},
            },
        )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'consolidate',
            str(extracted_dir),
            '--schema',
            str(schema_path),
            '--output',
            str(output_dir),
            '--dry-run',
            '--report-format',
            'json',
            '--fail-on-suspicious',
            'high',
        ],
    )

    assert result.exit_code == 2
    report = json.loads(result.output)
    assert report['fail_on_suspicious'] == 'high'
    assert report['would_fail_on_suspicious'] is True
    assert not output_dir.exists() or not any(output_dir.iterdir())


@pytest.mark.parametrize(
    ('schema_relative_path', 'expected_match_fields'),
    [
        (
            'schemas/personal/geothermal_ordinance_schema.json',
            ['category', 'facility_type', 'specific_subject'],
        ),
    ],
)
def test_geothermal_runtime_qaqc_config_does_not_require_schema_block(
    schema_relative_path: str,
    expected_match_fields: list[str],
) -> None:
    schema_path = REPO_ROOT / schema_relative_path
    schema_metadata = SchemaMetadata(schema_path)

    with pytest.raises(SchemaMetadataError, match='qa_qc.record_matching.key_fields'):
        schema_metadata.get_qa_qc_match_fields()

    runtime_artifact = _resolve_runtime_artifact(
        category=None,
        schema_path=schema_path,
        repo_root=REPO_ROOT,
    )

    assert runtime_artifact is not None

    config = resolve_qaqc_runtime_config(
        schema_metadata,
        runtime_artifact=runtime_artifact,
    )

    assert config['source'] == 'runtime_artifact'
    assert config['lane_name'] == 'quantitative'
    assert config['match_fields'] == expected_match_fields
    assert config['compare_fields'] == ['value', 'unit']


def test_geothermal_runtime_qaqc_config_can_select_qualitative_lane() -> None:
    schema_path = REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'
    schema_metadata = SchemaMetadata(schema_path)

    runtime_artifact = _resolve_runtime_artifact(
        category=None,
        schema_path=schema_path,
        repo_root=REPO_ROOT,
    )

    assert runtime_artifact is not None

    config = resolve_qaqc_runtime_config(
        schema_metadata,
        runtime_artifact=runtime_artifact,
        preferred_lane='qualitative',
    )

    assert config['source'] == 'runtime_artifact'
    assert config['lane_name'] == 'qualitative'
    assert config['mode'] == 'qualitative'
    assert config['comparison_approach'] == 'text_review'
    assert config['match_fields'] == ['category', 'facility_type', 'specific_subject']
    assert config['compare_fields'] == ['details']


def test_compare_cli_can_run_with_qualitative_lane(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    qa_qc_root = tmp_path / 'qa_qc' / 'Test County'
    qa_qc_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        qa_qc_root / 'gpt-4o.json',
        {
            'requirements': [
                {
                    'category': 'Permit required',
                    'facility_type': 'General',
                    'specific_subject': 'Commercial Use',
                    'details': 'Permit required before operations begin',
                }
            ]
        },
    )
    _write_json(
        qa_qc_root / 'gpt-4.1.json',
        {
            'requirements': [
                {
                    'category': 'Permit required',
                    'facility_type': 'General',
                    'specific_subject': 'Commercial Use',
                    'details': 'Permit required before drilling begins',
                }
            ]
        },
    )
    monkeypatch.setattr(
        'streamline_extract.qa_qc.report_generator.ReportGenerator.generate_report',
        lambda self, result, doc_dir: (doc_dir / 'comparison_report.xlsx', doc_dir / 'comparison_report.csv'),
    )

    result = runner.invoke(
        cli,
        [
            'compare',
            str(tmp_path / 'qa_qc'),
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--qaqc-lane',
            'qualitative',
        ],
    )

    assert result.exit_code == 0
    assert 'Requested QA/QC Lane' in result.output
    assert 'qualitative' in result.output
    assert 'Comparison Approach' in result.output
    assert 'text_review' in result.output
    assert 'Qualitative advisory gate' in result.output
    assert 'FAIL' in result.output


def test_compare_cli_ignores_comparison_summary_artifact(tmp_path, monkeypatch) -> None:
    runner = CliRunner()
    qa_qc_root = tmp_path / 'qa_qc' / 'Test County'
    qa_qc_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        qa_qc_root / 'gpt-4o.json',
        {
            'requirements': [
                {
                    'category': 'Permit required',
                    'facility_type': 'General',
                    'specific_subject': 'Commercial Use',
                    'details': 'Permit required before operations begin',
                }
            ]
        },
    )
    _write_json(
        qa_qc_root / 'gpt-4.1.json',
        {
            'requirements': [
                {
                    'category': 'Permit required',
                    'facility_type': 'General',
                    'specific_subject': 'Commercial Use',
                    'details': 'Permit required before drilling begins',
                }
            ]
        },
    )
    _write_json(
        qa_qc_root / 'comparison_summary.json',
        {
            'document_name': 'Test County',
            'summary': {
                'qualitative_advisory_gate': {
                    'status': 'pass',
                }
            },
        },
    )
    monkeypatch.setattr(
        'streamline_extract.qa_qc.report_generator.ReportGenerator.generate_report',
        lambda self, result, doc_dir: (doc_dir / 'comparison_report.xlsx', doc_dir / 'comparison_report.csv'),
    )

    result = runner.invoke(
        cli,
        [
            'compare',
            str(tmp_path / 'qa_qc'),
            '--schema',
            str(REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json'),
            '--qaqc-lane',
            'qualitative',
        ],
    )

    assert result.exit_code == 0
    assert 'comparison_summary' not in result.output
    assert 'Needs review: 1 field(s)' in result.output


def test_extract_and_save_result_includes_lineage_fields(tmp_path) -> None:
    result = SimpleNamespace(
        data={
            'metadata': {'jurisdiction': 'Example County'},
            'items': [{'name': 'a'}, {'name': 'b'}],
        },
        cost=0.123,
        processing_time=4.5,
        completeness_score=0.95,
        validation_notes=['ok'],
        input_tokens=111,
        output_tokens=222,
    )

    runtime_artifact = {
        'artifact_id': 'artifact://runtime/abc123def4567890',
        'contract_versions': {
            'extraction_record': '1.0.0',
            'modules_catalog': '1.0.0',
        },
        'lineage': {
            'artifact_id': 'artifact://runtime/abc123def4567890',
            'profile_id': 'default',
            'pack_name': 'tariffs',
            'pack_version': '1.0.0',
            'compiled_at': '2026-03-24T12:00:00Z',
        },
    }

    count = _extract_and_save_result(
        doc_path=Path('example.pdf'),
        result=result,
        output_dir=tmp_path,
        category='tariffs',
        model='gpt-5',
        qa_qc_enabled=False,
        runtime_artifact=runtime_artifact,
        run_id='run://deterministic1234',
        provider='azure',
        schema_id='https://example.org/schema/tariffs',
        identifier_fields=['jurisdiction'],
    )

    assert count == 2

    output_path = tmp_path / 'example.json'
    saved = json.loads(output_path.read_text(encoding='utf-8'))
    assert saved['contract_version'] == '1.0.0'
    assert saved['record_id'] == 'record://deterministic1234/example'
    assert saved['document']['source_filename'] == 'example.pdf'
    # Identifier is schema-driven: the caller-supplied identifier field
    # ('jurisdiction') selects the source_document_id — no domain term is
    # hardcoded in _extract_and_save_result.
    assert saved['document']['source_document_id'] == 'Example County'
    # Token metrics now flow through from the extraction result.
    assert saved['processing_metrics']['input_tokens'] == 111
    assert saved['processing_metrics']['output_tokens'] == 222
    assert saved['lineage']['artifact_id'] == runtime_artifact['artifact_id']
    assert saved['lineage']['profile_id'] == 'default'
    assert saved['lineage']['run_id'] == 'run://deterministic1234'
    assert saved['lineage']['provider'] == 'azure'
    assert saved['lineage']['schema_id'] == 'https://example.org/schema/tariffs'
    assert saved['payload']['items'][0]['name'] == 'a'
    assert saved['quality']['overall_confidence'] == 0.95
    assert saved['quality']['errors'] == []
    assert saved['processing_metrics']['cost_usd'] == 0.123


def test_extract_and_save_result_persists_structured_processing_errors(tmp_path) -> None:
    result = SimpleNamespace(
        data={'items': [{'name': 'a'}]},
        cost=0.123,
        processing_time=4.5,
        completeness_score=0.4,
        validation_notes=['partial extraction'],
        processing_errors=[
            {
                'stage': 'process',
                'category': 'document_processing',
                'code': 'document_extraction_failed',
                'message': 'OCR text quality degraded',
                'retryable': False,
                'source': {
                    'document_path': 'example.pdf',
                    'model': 'gpt-5',
                    'provider': 'azure',
                },
            }
        ],
    )

    _extract_and_save_result(
        doc_path=Path('example.pdf'),
        result=result,
        output_dir=tmp_path,
        category='tariffs',
        model='gpt-5',
        qa_qc_enabled=False,
        runtime_artifact=None,
        run_id='run://deterministic1234',
        provider='azure',
        schema_id='https://example.org/schema/tariffs',
    )

    saved = json.loads((tmp_path / 'example.json').read_text(encoding='utf-8'))
    assert saved['quality']['errors'][0]['category'] == 'document_processing'
    assert saved['quality']['errors'][0]['code'] == 'document_extraction_failed'


def test_generate_run_id_is_deterministic_for_same_inputs() -> None:
    run_id_1 = _generate_run_id(
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        provider='azure',
        model='gpt-5',
        enable_qa_qc=False,
        doc_files=[Path('documents/tariffs/a.pdf'), Path('documents/tariffs/b.pdf')],
        artifact_id='artifact://runtime/abc123def4567890',
    )
    run_id_2 = _generate_run_id(
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        provider='azure',
        model='gpt-5',
        enable_qa_qc=False,
        doc_files=[Path('documents/tariffs/b.pdf'), Path('documents/tariffs/a.pdf')],
        artifact_id='artifact://runtime/abc123def4567890',
    )

    assert run_id_1 == run_id_2
    assert run_id_1.startswith('run://')
