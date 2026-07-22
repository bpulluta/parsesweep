"""Contract validation tests for core modernization schemas."""

import json
from pathlib import Path

from jsonschema import Draft7Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_RECORD_CONTRACT_PATH = REPO_ROOT / "schemas/core/extraction_record.schema.json"
MODULES_CATALOG_CONTRACT_PATH = REPO_ROOT / "schemas/core/modules_catalog.schema.json"


def _load_contract_schema() -> dict:
    with EXTRACTION_RECORD_CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_modules_catalog_schema() -> dict:
    with MODULES_CATALOG_CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sample_record() -> dict:
    return {
        "record_id": "record-0001",
        "contract_version": "1.0.0",
        "document": {
            "source_document_id": "doc-0001",
            "source_path": "documents/tariffs/electric-tariff.pdf",
            "source_filename": "electric-tariff.pdf",
            "source_sha256": None,
            "mime_type": "application/pdf",
            "page_span": "1-5",
        },
        "lineage": {
            "artifact_id": "artifact://contracts/core/v1",
            "profile_id": "default",
            "run_id": "run-123",
            "model": "compassop-gpt-4.1-mini",
            "provider": "azure",
            "schema_id": "schemas/personal/electricity_tariff_schema.json",
            "extracted_at": "2026-03-24T12:00:00Z",
        },
        "payload": {"items": []},
        "quality": {
            "overall_confidence": 0.92,
            "warnings": [],
            "errors": [],
        },
        "processing_metrics": {
            "duration_seconds": 3.2,
            "input_tokens": 1200,
            "output_tokens": 300,
            "cost_usd": 0.012,
        },
    }


def _sample_modules_catalog() -> dict:
    return {
        "catalog_id": "catalog-0001",
        "contract_version": "1.0.0",
        "catalog_metadata": {
            "artifact_id": "artifact://contracts/core/v1",
            "profile_id": "default",
            "generated_at": "2026-03-24T12:00:00Z",
            "source": "modernization-seed",
        },
        "modules": [
            {
                "module_id": "classifier-001",
                "name": "value-semantics-classifier",
                "version": "1.0.0",
                "kind": "classifier",
                "enabled": True,
                "entrypoint": "psweep.modules.classifier:run",
                "config": {"strict": True},
                "notes": None,
            }
        ],
    }


def test_contract_file_exists() -> None:
    assert EXTRACTION_RECORD_CONTRACT_PATH.exists(), "Core extraction record contract schema must exist"


def test_modules_catalog_contract_file_exists() -> None:
    assert MODULES_CATALOG_CONTRACT_PATH.exists(), "Core modules catalog contract schema must exist"


def test_contract_has_required_metadata_fields() -> None:
    schema = _load_contract_schema()
    metadata = schema["$metadata"]

    assert metadata["version"] == "1.0.0"
    assert metadata["extraction"]["main_data_array"] == "records"
    assert metadata["extraction"]["identifier_fields"]


def test_contract_id_contains_version() -> None:
    schema = _load_contract_schema()

    assert schema["$metadata"]["version"] in schema["$id"]


def test_contract_schema_is_valid_jsonschema() -> None:
    schema = _load_contract_schema()

    Draft7Validator.check_schema(schema)


def test_contract_accepts_valid_sample_record() -> None:
    schema = _load_contract_schema()
    sample = _sample_record()

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(sample), key=lambda item: list(item.path))

    assert not errors, "Expected sample extraction record to satisfy contract schema"


def test_contract_accepts_structured_quality_errors() -> None:
    schema = _load_contract_schema()
    sample = _sample_record()
    sample["quality"]["errors"] = [
        {
            "stage": "process",
            "category": "document_processing",
            "code": "document_extraction_failed",
            "message": "OCR text quality degraded",
            "retryable": False,
            "source": {
                "document_path": "documents/tariffs/electric-tariff.pdf",
                "model": "compassop-gpt-4.1-mini",
                "provider": "azure",
            },
        }
    ]

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(sample), key=lambda item: list(item.path))

    assert not errors, "Expected sample extraction record with structured errors to satisfy contract schema"


def test_modules_catalog_contract_has_required_metadata_fields() -> None:
    schema = _load_modules_catalog_schema()
    metadata = schema["$metadata"]

    assert metadata["version"] == "1.0.0"
    assert metadata["extraction"]["main_data_array"] == "modules"
    assert metadata["extraction"]["identifier_fields"]


def test_modules_catalog_contract_id_contains_version() -> None:
    schema = _load_modules_catalog_schema()

    assert schema["$metadata"]["version"] in schema["$id"]


def test_modules_catalog_contract_schema_is_valid_jsonschema() -> None:
    schema = _load_modules_catalog_schema()

    Draft7Validator.check_schema(schema)


def test_modules_catalog_contract_accepts_valid_sample() -> None:
    schema = _load_modules_catalog_schema()
    sample = _sample_modules_catalog()

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(sample), key=lambda item: list(item.path))

    assert not errors, "Expected sample modules catalog to satisfy contract schema"
