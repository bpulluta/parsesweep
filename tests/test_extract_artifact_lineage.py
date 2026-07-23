"""Tests for extraction and compilation CLI lineage."""

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from psweep.cli.commands import (
    _build_dedup_preview_report,
    _context_budget_suggestions_for_process,
    _extract_and_save_result,
    _generate_run_id,
    _run_qa_qc_extraction,
    _resolve_compilation_output_formats,
    _resolve_runtime_artifact,
    _resolve_schema_ref,
    _should_fail_on_suspicious,
)
from psweep.cli.main import cli
from psweep.utils.schema_metadata import SchemaMetadata


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


def test_resolve_compilation_output_formats_defaults_to_both() -> None:
    assert _resolve_compilation_output_formats() == ['csv', 'excel']


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
                'compilation': {
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
    assert metadata['compilation']['deduplication'] == {
        'key_fields': ['category', 'citation'],
        'ignore_fields': ['notes'],
    }
    assert 'normalization' not in metadata['extraction']
    assert 'output' not in metadata
    assert 'validation' not in metadata
    assert 'strategy' not in metadata['compilation']['deduplication']
    assert starter_schema['title'] == 'Starter Permit Starter Schema'
    assert 'examples' not in json.dumps(starter_schema)
    assert 'enum' not in json.dumps(starter_schema)
    assert 'default' not in json.dumps(starter_schema)


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
                'compilation': {
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
    assert starter_schema['$metadata']['compilation']['deduplication']['key_fields'] == ['feature']
    assert 'ignore_fields' not in starter_schema['$metadata']['compilation']['deduplication']


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
                    'compilation': {
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
    result = runner.invoke(cli, ['check-schema', str(schema_path)])

    assert result.exit_code == 0
    assert 'Identifier fields: jurisdiction.state, jurisdiction.county' in result.output
    assert 'Main data array: items' in result.output
    assert 'Deduplication key fields: name' in result.output
    assert "$metadata.extraction missing 'identifier_fields'" not in result.output
    assert "$metadata.extraction missing 'main_data_array'" not in result.output


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

    monkeypatch.setattr('psweep.qa_qc.ModelDetector.get_qa_models', lambda: ['model-a', 'model-b'])
    monkeypatch.setattr('psweep.qa_qc.ModelDetector.get_provider', lambda: 'openai')
    monkeypatch.setattr('psweep.extraction.document_utils.extract_text_from_document', lambda *args, **kwargs: 'doc text')
    monkeypatch.setattr(
        'psweep.qa_qc.run_multi_model_extraction',
        lambda **kwargs: {
            'model-a': _DummyResult(cost=0.1, processing_time=1.0),
            'model-b': _DummyResult(cost=0.2, processing_time=1.5),
        },
    )
    monkeypatch.setattr('psweep.cli.commands.ask_confirm', lambda *args, **kwargs: True)

    _run_qa_qc_extraction(
        doc_files=[document_path],
        loaded_schema={'type': 'object'},
        schema_path=REPO_ROOT / 'schemas/personal/geothermal_ordinance_schema.json',
        output_dir=tmp_path / 'extracted',
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
    assert 'pixi run psweep compare' in captured
    assert '--qaqc-lane qualitative' in captured


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


def test_resolve_compilation_output_formats_honors_runtime_excel_override() -> None:
    assert _resolve_compilation_output_formats(
        {
            'compilation': {
                'output': {
                    'default_format': 'excel',
                }
            }
        }
    ) == ['excel']


def test_resolve_compilation_output_formats_falls_back_for_invalid_value() -> None:
    assert _resolve_compilation_output_formats(
        {
            'compilation': {
                'output': {
                    'default_format': 'spreadsheet',
                }
            }
        }
    ) == ['csv', 'excel']


def test_compile_cli_keeps_dual_output_without_runtime_override(tmp_path) -> None:
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
                'compilation': {
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

    extracted_dir = tmp_path / 'extracted/contracts'
    output_dir = tmp_path / 'compiled/contracts'
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
            'compile',
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


def test_compile_cli_dry_run_previews_deduplication_without_writing_outputs(tmp_path) -> None:
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
                'compilation': {
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

    extracted_dir = tmp_path / 'extracted/contracts'
    output_dir = tmp_path / 'compiled/contracts'
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
            'compile',
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


def test_compile_cli_dry_run_json_report_is_machine_readable(tmp_path) -> None:
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
                'compilation': {
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

    extracted_dir = tmp_path / 'extracted/contracts'
    output_dir = tmp_path / 'compiled/contracts'
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
            'compile',
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


def test_compile_cli_dry_run_json_report_can_fail_on_suspicious_threshold(tmp_path) -> None:
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
                'compilation': {
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

    extracted_dir = tmp_path / 'extracted/contracts'
    output_dir = tmp_path / 'compiled/contracts'
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
            'compile',
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
                'stage': 'extract',
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
