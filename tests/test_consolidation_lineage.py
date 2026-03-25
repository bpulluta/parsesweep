"""Tests for run_id lineage propagation into consolidated outputs."""

import json
from pathlib import Path

from streamline_extract.consolidation.consolidator import Consolidator
from streamline_extract.utils.schema_metadata import SchemaMetadata


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")


def _write_schema(path: Path) -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "Test",
            "version": "1.0.0",
            "extraction": {
                "main_data_array": "items",
                "context_objects": ["metadata"],
                "identifier_fields": ["metadata.id"],
                "document_type": "Test Document",
            },
            "consolidation": {
                "deduplication": {
                    "key_fields": ["name"],
                    "ignore_fields": ["notes"],
                }
            },
        },
        "type": "object",
        "properties": {
            "metadata": {"type": "object"},
            "items": {"type": "array"},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def test_consolidation_includes_run_id_from_extraction_record_payload(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    extracted_dir = tmp_path / "processed/tariffs"
    _write_json(
        extracted_dir / "doc1.json",
        {
            "record_id": "record-1",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "doc-1",
                "source_path": "documents/tariffs/doc1.pdf",
                "source_filename": "doc1.pdf",
            },
            "lineage": {
                "run_id": "run://deterministic1111",
                "artifact_id": "artifact://runtime/abc123",
                "profile_id": "default",
                "model": "gpt-5",
                "provider": "azure",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-1", "jurisdiction": "Example City"},
                "items": [{"name": "Charge A", "value": 10}],
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    consolidator = Consolidator(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = consolidator.consolidate_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic1111"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/abc123"


def test_consolidation_includes_run_id_from_qaqc_extraction_record(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    extracted_dir = tmp_path / "processed/qa_qc/test_doc"
    _write_json(
        extracted_dir / "gpt-4o.json",
        {
            "record_id": "record-qaqc-1",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "T-2",
                "source_path": "documents/qa_qc/test_doc.pdf",
                "source_filename": "test_doc.pdf",
            },
            "lineage": {
                "run_id": "run://deterministic2222",
                "artifact_id": "artifact://runtime/def456",
                "profile_id": "default",
                "model": "gpt-4o",
                "provider": "openai",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-2", "jurisdiction": "Example County"},
                "items": [{"name": "Charge B", "value": 20}],
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    consolidator = Consolidator(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = consolidator.consolidate_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic2222"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/def456"


def test_consolidation_supports_extraction_record_payload_lineage(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    extracted_dir = tmp_path / "processed/contracts"
    _write_json(
        extracted_dir / "record-1.json",
        {
            "record_id": "record-1",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "doc-1",
                "source_path": "documents/contracts/doc-1.pdf",
                "source_filename": "doc-1.pdf",
            },
            "lineage": {
                "artifact_id": "artifact://runtime/ghi789",
                "profile_id": "default",
                "run_id": "run://deterministic3333",
                "model": "gpt-5",
                "provider": "azure",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-3", "jurisdiction": "Example Borough"},
                "items": [{"name": "Charge C", "value": 30}],
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    consolidator = Consolidator(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = consolidator.consolidate_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert "Run_Id" not in df.columns
    assert "Artifact_Id" not in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic3333"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/ghi789"
    assert df.iloc[0]["Jurisdiction"] == "Example Borough"


def test_consolidation_skips_non_record_metadata_files(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    extracted_dir = tmp_path / "processed/qa_qc/test_doc"
    _write_json(
        extracted_dir / "metadata.json",
        {
            "document": "test_doc",
            "models": ["gpt-4o", "gpt-4.1"],
            "status": "completed",
        },
    )
    _write_json(
        extracted_dir / "gpt-4o.json",
        {
            "record_id": "record-qaqc-2",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "T-4",
                "source_path": "documents/qa_qc/test_doc.pdf",
                "source_filename": "test_doc.pdf",
            },
            "lineage": {
                "artifact_id": "artifact://runtime/jkl012",
                "profile_id": "default",
                "run_id": "run://deterministic4444",
                "model": "gpt-4o",
                "provider": "openai",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-4", "jurisdiction": "Example Parish"},
                "items": [{"name": "Charge D", "value": 40}],
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    consolidator = Consolidator(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = consolidator.consolidate_from_directory(extracted_dir)

    assert len(df) == 1
    assert df.iloc[0]["Run Id"] == "run://deterministic4444"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/jkl012"
    assert df.iloc[0]["Jurisdiction"] == "Example Parish"
