"""Tests for run_id lineage propagation into compiled outputs."""

import json
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from psweep.compilation.data_compiler import DataCompiler
from psweep.compilation.deduplicator import Deduplicator
from psweep.utils.schema_metadata import SchemaMetadata


from _helpers import write_json as _write_json


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
            "identity": {
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


def test_compilation_includes_run_id_from_extraction_record_payload(tmp_path) -> None:
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
    compiler = DataCompiler(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = compiler.compile_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic1111"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/abc123"


def test_compilation_includes_run_id_from_qaqc_extraction_record(tmp_path) -> None:
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
    compiler = DataCompiler(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = compiler.compile_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic2222"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/def456"


def test_compilation_supports_extraction_record_payload_lineage(tmp_path) -> None:
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
            "quality": {
                "warnings": [],
                "errors": [
                    {
                        "stage": "extract",
                        "category": "document_processing",
                        "code": "document_extraction_failed",
                        "message": "OCR text quality degraded",
                        "retryable": False,
                        "source": {
                            "document_path": "documents/contracts/doc-1.pdf",
                            "model": "gpt-5",
                            "provider": "azure",
                        },
                    }
                ],
            },
            "payload": {
                "metadata": {"id": "T-3", "jurisdiction": "Example Borough"},
                "items": [{"name": "Charge C", "value": 30}],
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    compiler = DataCompiler(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = compiler.compile_from_directory(extracted_dir)

    assert not df.empty
    assert "Run Id" in df.columns
    assert "Artifact Id" in df.columns
    assert "Run_Id" not in df.columns
    assert "Artifact_Id" not in df.columns
    assert df.iloc[0]["Run Id"] == "run://deterministic3333"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/ghi789"
    assert df.iloc[0]["Error Count"] == 1
    assert df.iloc[0]["Error Categories"] == "document_processing"
    assert "OCR text quality degraded" in df.iloc[0]["Error Messages"]
    assert df.iloc[0]["Jurisdiction"] == "Example Borough"


def test_compilation_skips_non_record_metadata_files(tmp_path) -> None:
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
    compiler = DataCompiler(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = compiler.compile_from_directory(extracted_dir)

    assert len(df) == 1
    assert df.iloc[0]["Run Id"] == "run://deterministic4444"
    assert df.iloc[0]["Artifact Id"] == "artifact://runtime/jkl012"
    assert df.iloc[0]["Jurisdiction"] == "Example Parish"


def test_compilation_exclude_fields_can_come_from_runtime_overrides(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    extracted_dir = tmp_path / "processed/contracts"
    _write_json(
        extracted_dir / "record-2.json",
        {
            "record_id": "record-2",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "doc-2",
                "source_path": "documents/contracts/doc-2.pdf",
                "source_filename": "doc-2.pdf",
            },
            "lineage": {
                "artifact_id": "artifact://runtime/mno345",
                "profile_id": "default",
                "run_id": "run://deterministic5555",
                "model": "gpt-5",
                "provider": "azure",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-5", "jurisdiction": "Example District", "internal_note": "drop me"},
                "items": [{"name": "Charge E", "value": 50}],
            },
        },
    )

    schema_metadata = SchemaMetadata(
        schema_path,
        metadata_overrides={
            "compilation": {
                "output": {
                    "exclude_fields": ["internal_note"],
                }
            }
        },
    )
    compiler = DataCompiler(schema_metadata=schema_metadata, verbose=False, debug=False)
    df, _ = compiler.compile_from_directory(extracted_dir)

    assert not df.empty
    assert "Internal Note" not in df.columns
    assert df.iloc[0]["Jurisdiction"] == "Example District"


def test_compilation_output_overrides_apply_to_saved_exports(tmp_path) -> None:
    """Runtime output overrides should shape saved CSV and Excel exports."""
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    schema_metadata = SchemaMetadata(
        schema_path,
        metadata_overrides={
            "compilation": {
                "output": {
                    "column_renames": {
                        "Jurisdiction": "county",
                        "Run Id": "run_id",
                        "Name": "charge_name",
                        "Value": "amount",
                    },
                    "column_order": [
                        "county",
                        "run_id",
                        "charge_name",
                        "amount",
                    ],
                    "freeze_columns": 3,
                    "auto_width": False,
                }
            }
        },
    )
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=False,
        debug=False,
    )

    export_frame = pd.DataFrame(
        [
            {
                "Jurisdiction": "Example District",
                "Run Id": "run://deterministic6666",
                "Name": "Charge F",
                "Value": 60,
                "Artifact Id": "artifact://runtime/pqr678",
            }
        ]
    )

    csv_path = tmp_path / "output.csv"
    excel_path = tmp_path / "output.xlsx"
    compiler.save_csv(export_frame, csv_path)
    compiler.save_excel(export_frame, excel_path)

    csv_headers = (
        csv_path.read_text(encoding="utf-8").splitlines()[0].split(",")
    )
    assert csv_headers == [
        "county",
        "run_id",
        "charge_name",
        "amount",
        "Artifact Id",
    ]

    workbook = load_workbook(excel_path)
    worksheet = workbook["Data"]
    excel_headers = [cell.value for cell in worksheet[1]]
    assert excel_headers == [
        "county",
        "run_id",
        "charge_name",
        "amount",
        "Artifact Id",
    ]
    assert worksheet.freeze_panes == "D2"


def test_preview_deduplication_flags_conflicting_non_key_values_as_suspicious(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)

    schema_metadata = SchemaMetadata(schema_path)
    deduplicator = Deduplicator(schema_metadata=schema_metadata)
    df = pd.DataFrame(
        [
            {
                "Jurisdiction": "Example Borough",
                "Id": "doc-1",
                "Name": "Charge A",
                "Value": 10,
                "Run Id": "run://1",
                "Notes": "first copy",
            },
            {
                "Jurisdiction": "Example Borough",
                "Id": "doc-2",
                "Name": "Charge A",
                "Value": 12,
                "Run Id": "run://2",
                "Notes": "second copy",
            },
        ]
    )

    preview = deduplicator.preview_deduplication(df)

    assert preview["duplicates_removed"] == 1
    assert preview["suspicious_groups_count"] == 1
    assert preview["suspicious_groups_by_severity"] == {"high": 1, "medium": 0, "low": 0}
    assert len(preview["warnings"]) == 1
    assert preview["duplicate_groups"][0]["suspicious"] is True
    assert preview["duplicate_groups"][0]["severity"] == "high"
    assert preview["duplicate_groups"][0]["conflicting_columns"] == ["Value"]
    assert preview["suspicious_groups"][0]["severity"] == "high"
    assert preview["suspicious_groups"][0]["conflicting_columns"] == ["Value"]


def test_preview_deduplication_uses_schema_numeric_fields_for_high_severity(tmp_path) -> None:
    schema_path = tmp_path / "schema.json"
    _write_json(
        schema_path,
        {
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
                "identity": {
                    "deduplication": {
                        "key_fields": ["name"],
                        "ignore_fields": ["notes"],
                    }
                },
            },
            "type": "object",
            "properties": {
                "metadata": {"type": "object"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "distance_measure": {"type": "number"},
                        },
                    },
                },
            },
        },
    )

    schema_metadata = SchemaMetadata(schema_path)
    deduplicator = Deduplicator(schema_metadata=schema_metadata)
    df = pd.DataFrame(
        [
            {
                "Jurisdiction": "Example Borough",
                "Id": "doc-1",
                "Name": "Charge A",
                "Distance Measure": 10,
                "Run Id": "run://1",
                "Notes": "first copy",
            },
            {
                "Jurisdiction": "Example Borough",
                "Id": "doc-2",
                "Name": "Charge A",
                "Distance Measure": 12,
                "Run Id": "run://2",
                "Notes": "second copy",
            },
        ]
    )

    preview = deduplicator.preview_deduplication(df)

    assert preview["duplicates_removed"] == 1
    assert preview["duplicate_groups"][0]["conflicting_columns"] == ["Distance Measure"]
    assert preview["duplicate_groups"][0]["severity"] == "high"
    assert preview["suspicious_groups_by_severity"] == {"high": 1, "medium": 0, "low": 0}


def _write_record_with_state(path: Path, state: str) -> None:
    _write_json(
        path,
        {
            "record_id": "r1",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "d1",
                "source_path": "documents/x/d1.pdf",
                "source_filename": "d1.pdf",
            },
            "lineage": {
                "run_id": "run://s",
                "artifact_id": "artifact://s",
                "profile_id": "default",
                "model": "gpt-5",
                "provider": "azure",
                "extracted_at": "2026-03-24T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "T-1", "state": state},
                "items": [{"name": "Charge A", "value": 10}],
            },
        },
    )


def test_state_normalization_default_abbreviates(tmp_path) -> None:
    """By default (no config) a 'State' column is normalized to its abbrev."""
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)
    extracted_dir = tmp_path / "processed/tariffs"
    _write_record_with_state(extracted_dir / "doc1.json", "Utah")

    compiler = DataCompiler(
        schema_metadata=SchemaMetadata(schema_path), verbose=False, debug=False
    )
    df, _ = compiler.compile_from_directory(extracted_dir)
    assert df.iloc[0]["State"] == "UT"


def test_state_normalization_can_be_disabled_via_config(tmp_path) -> None:
    """A non-US/non-jurisdiction domain opts out; raw values are preserved."""
    schema_path = tmp_path / "schema.json"
    _write_schema(schema_path)
    extracted_dir = tmp_path / "processed/tariffs"
    _write_record_with_state(extracted_dir / "doc1.json", "Utah")

    schema_metadata = SchemaMetadata(
        schema_path,
        metadata_overrides={
            "compilation": {"normalization": {"state_column": None}}
        },
    )
    compiler = DataCompiler(
        schema_metadata=schema_metadata, verbose=False, debug=False
    )
    df, _ = compiler.compile_from_directory(extracted_dir)
    assert df.iloc[0]["State"] == "Utah"
