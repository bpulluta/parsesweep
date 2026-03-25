"""Tests for Phase 2 lineage wiring in process output."""

import json
from pathlib import Path
from types import SimpleNamespace

from streamline_extract.cli.commands import (
    _extract_and_save_result,
    _generate_run_id,
    _resolve_runtime_artifact,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_resolve_runtime_artifact_returns_none_without_pack_or_profile_dirs(tmp_path) -> None:
    resolved = _resolve_runtime_artifact(
        category='tariffs',
        schema_path=Path('schemas/personal/electricity_tariff_schema.json'),
        repo_root=tmp_path,
    )

    assert resolved is None


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
    )

    assert count == 2

    output_path = tmp_path / 'example.json'
    saved = json.loads(output_path.read_text(encoding='utf-8'))
    assert saved['contract_version'] == '1.0.0'
    assert saved['record_id'] == 'record://deterministic1234/example'
    assert saved['document']['source_filename'] == 'example.pdf'
    assert saved['document']['source_document_id'] == 'Example County'
    assert saved['lineage']['artifact_id'] == runtime_artifact['artifact_id']
    assert saved['lineage']['profile_id'] == 'default'
    assert saved['lineage']['run_id'] == 'run://deterministic1234'
    assert saved['lineage']['provider'] == 'azure'
    assert saved['lineage']['schema_id'] == 'https://example.org/schema/tariffs'
    assert saved['payload']['items'][0]['name'] == 'a'
    assert saved['quality']['overall_confidence'] == 0.95
    assert saved['processing_metrics']['cost_usd'] == 0.123


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
