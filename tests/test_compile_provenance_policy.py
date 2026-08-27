from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli

from _helpers import write_json as _write_json


def _write_min_schema(path: Path) -> None:
    _write_json(
        path,
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "domain": "Demo",
                "version": "1.0.0",
                "extraction": {
                    "main_data_array": "items",
                    "context_objects": ["metadata"],
                    "identifier_fields": ["metadata.id"],
                    "document_type": "Demo Document",
                },
                "identity": {
                    "deduplication": {
                        "key_fields": ["name"],
                        "ignore_fields": [],
                    }
                },
            },
            "type": "object",
            "properties": {
                "metadata": {"type": "object"},
                "items": {"type": "array"},
            },
        },
    )


def _write_bad_record(path: Path) -> None:
    _write_json(
        path,
        {
            "record_id": "record://bad",
            "contract_version": "1.0.0",
            "document": {
                "source_document_id": "bad",
                "source_path": "documents/demo/bad.pdf",
                "source_filename": "bad.pdf",
            },
            "lineage": {
                "run_id": "run://bad",
                "artifact_id": "artifact://runtime/unresolved",
                "profile_id": "default",
                "schema_id": None,
                "model": "gpt-5.6-terra",
                "provider": "openai",
                "extracted_at": "2026-01-01T00:00:00Z",
            },
            "payload": {
                "metadata": {"id": "bad"},
                "items": [{"name": "x"}],
            },
        },
    )


def test_compile_dry_run_json_fails_when_provenance_policy_is_fail(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    extracted_dir = tmp_path / "extracted" / "demo"
    _write_min_schema(schema_path)
    _write_bad_record(extracted_dir / "bad.json")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "compile",
            str(extracted_dir),
            "--schema",
            str(schema_path),
            "--dry-run",
            "--report-format",
            "json",
            "--provenance-policy",
            "fail",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["provenance_policy"] == "fail"
    assert payload["would_fail_on_provenance"] is True
    assert any(w.startswith("missing_schema_id:") for w in payload["warnings"])
    assert any(w.startswith("unresolved_artifact_id:") for w in payload["warnings"])


def test_compile_dry_run_json_warn_policy_reports_without_failing(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    extracted_dir = tmp_path / "extracted" / "demo"
    _write_min_schema(schema_path)
    _write_bad_record(extracted_dir / "bad.json")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "compile",
            str(extracted_dir),
            "--schema",
            str(schema_path),
            "--dry-run",
            "--report-format",
            "json",
            "--provenance-policy",
            "warn",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["provenance_policy"] == "warn"
    assert payload["would_fail_on_provenance"] is False
    assert any(w.startswith("missing_schema_id:") for w in payload["warnings"])
    assert any(w.startswith("unresolved_artifact_id:") for w in payload["warnings"])
