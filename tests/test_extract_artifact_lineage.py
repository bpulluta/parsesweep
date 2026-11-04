"""Tests for extraction and compilation CLI lineage."""

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from _helpers import write_json as _write_json
from psweep.cli.commands_compile import (
    _build_dedup_preview_report,
    _should_fail_on_suspicious,
)
from psweep.extraction.schema_utils import load_schema
from psweep.extraction.record_writer import write_extraction_record
from psweep.cli.main import cli
from psweep.utils.schema_metadata import SchemaMetadata


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_schema_loader_accepts_direct_json_contract(tmp_path) -> None:
    """Extraction uses the domain schema directly, without pack unwrapping."""
    schema = tmp_path / 'schema.json'
    _write_json(schema, {'type': 'object'})
    assert load_schema(schema) == {"type": "object"}


def test_resolve_compilation_output_formats_defaults_to_both(schema_path) -> None:
    assert SchemaMetadata.for_compilation(schema_path, {}).get_output_formats() == ['csv', 'excel']


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
                    'context_objects': ['jurisdiction'],
                    'display_name_template': '{jurisdiction.county}',
                    'document_type': 'Reference Permit',
                    'normalization': {'trim_strings': True},
                },
                'identity': {
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
    output_path = tmp_path / 'config/starter_domain/schema.json'
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

    starter_schema = json.loads(output_path.read_text(encoding='utf-8'))
    metadata = starter_schema['$metadata']
    assert metadata['domain'] == 'Starter Domain'
    assert metadata['version'] == '0.1.0'
    assert metadata['extraction']['document_type'] == 'Starter Permit'
    assert metadata['identity']['deduplication'] == {
        'key_fields': ['category', 'citation'],
        'ignore_fields': ['notes'],
    }
    assert 'normalization' not in metadata['extraction']
    assert 'output' not in metadata
    assert 'validation' not in metadata
    assert 'strategy' not in metadata['identity']['deduplication']
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
                    'context_objects': ['jurisdiction'],
                    'document_type': 'Reference Ordinance',
                },
                'identity': {
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
    output_path = tmp_path / 'config/trimmed/schema.json'
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
    assert starter_schema['$metadata']['identity']['deduplication']['key_fields'] == ['feature']
    assert 'ignore_fields' not in starter_schema['$metadata']['identity']['deduplication']


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
                    },
                    'identity': {
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
    assert 'Main data array: items' in result.output
    assert 'Deduplication key fields: name' in result.output
    assert "$metadata.extraction missing 'main_data_array'" not in result.output


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


def test_resolve_compilation_output_formats_honors_runtime_excel_override(schema_path) -> None:
    assert SchemaMetadata.for_compilation(schema_path, {
        'compilation_output': {'default_format': 'excel'},
    }).get_output_formats() == ['excel']


def test_resolve_compilation_output_formats_rejects_invalid_value(schema_path) -> None:
    from psweep.exceptions import SchemaMetadataError

    with pytest.raises(SchemaMetadataError, match='default_format'):
        SchemaMetadata.for_compilation(schema_path, {
            'compilation_output': {'default_format': 'spreadsheet'},
        })


@pytest.mark.parametrize("schema_format", [None, "excel"])
def test_compile_cli_respects_schema_format_and_dual_default(tmp_path, schema_format) -> None:
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
                    'document_type': 'Test Document',
                },
                'identity': {
                    'deduplication': {
                        'key_fields': ['name'],
                        'ignore_fields': [],
                    },
                },
                'compilation': {
                    'output': {
                        **({'default_format': schema_format} if schema_format else {}),
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
    assert (output_dir / 'contracts.csv').exists() is (schema_format is None)
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
                    'document_type': 'Test Document',
                },
                'identity': {
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
                    'document_type': 'Test Document',
                },
                'identity': {
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
    assert report['compare_columns'] == ['name']
    assert len(report['duplicate_groups']) == 1
    assert report['duplicate_groups'][0]['suspicious'] is True
    assert report['duplicate_groups'][0]['severity'] == 'high'
    assert report['duplicate_groups'][0]['conflicting_columns'] == ['id', 'value']
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
                    'document_type': 'Test Document',
                },
                'identity': {
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


@pytest.fixture
def writer_metadata(tmp_path):
    """Provide schema-owned counts for record-writer tests."""
    path = tmp_path / "record_domain.schema.json"
    _write_json(path, {
        "$metadata": {"extraction": {
            "main_data_array": "items",
        }},
        "type": "object",
        "properties": {
            "metadata": {"type": "object"}, "items": {"type": "array"},
        },
    })
    return SchemaMetadata(path)


def test_write_extraction_record_includes_lineage_fields(
    tmp_path, writer_metadata,
) -> None:
    """Canonical writing preserves runtime-artifact and token lineage."""
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
            'pack_name': 'tariffs',
            'pack_version': '1.0.0',
            'compiled_at': '2026-03-24T12:00:00Z',
        },
    }

    document = tmp_path / "example.pdf"
    document.write_bytes(b"synthetic source")
    count = write_extraction_record(
        tmp_path / "example.json",
        doc_path=document,
        source_document_id="doc.txt",
        result=result,
        model='gpt-5',
        schema_metadata=writer_metadata,
        runtime_artifact=runtime_artifact,
        run_id='run://deterministic1234',
        provider='azure',
        schema_id='https://example.org/schema/tariffs',
    )

    assert count == 2

    output_path = tmp_path / 'example.json'
    saved = json.loads(output_path.read_text(encoding='utf-8'))
    assert saved['contract_version'] == '1.1.0'
    assert saved["record_id"].startswith("record://deterministic1234/")
    assert len(saved["record_id"].rsplit("/", 1)[1]) == 64
    assert saved['document']['source_filename'] == 'example.pdf'
    assert saved['document']['source_document_id'] == 'doc.txt'
    # Token metrics now flow through from the extraction result.
    assert saved['processing_metrics']['input_tokens'] == 111
    assert saved['processing_metrics']['output_tokens'] == 222
    assert saved['lineage']['artifact_id'] == runtime_artifact['artifact_id']
    assert saved['lineage']['run_id'] == 'run://deterministic1234'
    assert saved['lineage']['provider'] == 'azure'
    assert saved['lineage']['schema_id'] == 'https://example.org/schema/tariffs'
    assert saved['payload']['items'][0]['name'] == 'a'
    assert saved['quality']['completeness'] == 0.95
    assert saved['quality']['errors'] == []
    assert saved['processing_metrics']['cost_usd'] == 0.123


def test_write_extraction_record_persists_structured_processing_errors(
    tmp_path, writer_metadata,
) -> None:
    """Structured processing errors survive atomic record publication."""
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

    document = tmp_path / "example.pdf"
    document.write_bytes(b"synthetic source")
    write_extraction_record(
        tmp_path / "example.json",
        doc_path=document,
        source_document_id="doc.txt",
        result=result,
        model='gpt-5',
        schema_metadata=writer_metadata,
        runtime_artifact=None,
        run_id='run://deterministic1234',
        provider='azure',
        schema_id='https://example.org/schema/tariffs',
    )

    saved = json.loads((tmp_path / 'example.json').read_text(encoding='utf-8'))
    assert saved['quality']['errors'][0]['category'] == 'document_processing'
    assert saved['quality']['errors'][0]['code'] == 'document_extraction_failed'
    assert saved['lineage']['artifact_id'] == 'artifact://runtime/deterministic1234'
