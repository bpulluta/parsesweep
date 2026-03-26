"""Tests for deterministic process run manifest generation."""

import json
from pathlib import Path

from streamline_extract.cli.commands import _build_run_manifest, _write_run_manifest


def test_build_run_manifest_sorts_documents_and_output_records() -> None:
    runtime_artifact = {
        "artifact_id": "artifact://runtime/abc123def4567890",
        "lineage": {"profile_id": "default"},
    }

    manifest = _build_run_manifest(
        run_id="run://deterministic1234",
        mode="single_model",
        schema_path=Path("schemas/personal/electricity_tariff_schema.json"),
        provider="azure",
        model="gpt-5",
        runtime_artifact=runtime_artifact,
        doc_files=[Path("documents/tariffs/b.pdf"), Path("documents/tariffs/a.pdf")],
        successful_output_paths=[
            Path("processed/tariffs/b.json"),
            Path("processed/tariffs/a.json"),
        ],
        started_at="2026-03-25T10:00:00Z",
        finished_at="2026-03-25T10:00:03Z",
        total_processed=2,
        successful_count=2,
        failed_count=0,
        failed_results=[],
    )

    assert manifest["manifest_version"] == "1.0.0"
    assert manifest["run_id"] == "run://deterministic1234"
    assert manifest["lineage"]["artifact_id"] == "artifact://runtime/abc123def4567890"
    assert manifest["lineage"]["profile_id"] == "default"
    assert manifest["lineage"]["provider"] == "azure"
    assert manifest["lineage"]["model"] == "gpt-5"
    assert manifest["documents"] == [
        "documents/tariffs/a.pdf",
        "documents/tariffs/b.pdf",
    ]
    assert manifest["outputs"]["records"] == [
        "processed/tariffs/a.json",
        "processed/tariffs/b.json",
    ]
    assert manifest["status"]["result"] == "success"
    assert manifest["errors"]["total_errors"] == 0
    assert manifest["errors"]["by_category"] == {}


def test_build_run_manifest_summarizes_failed_error_records() -> None:
    manifest = _build_run_manifest(
        run_id="run://deterministic1234",
        mode="single_model",
        schema_path=Path("schemas/personal/electricity_tariff_schema.json"),
        provider="azure",
        model="gpt-5",
        runtime_artifact=None,
        doc_files=[Path("documents/tariffs/a.pdf")],
        successful_output_paths=[],
        started_at="2026-03-25T10:00:00Z",
        finished_at="2026-03-25T10:00:03Z",
        total_processed=1,
        successful_count=0,
        failed_count=1,
        failed_results=[
            {
                "file": "a.pdf",
                "success": False,
                "errors": [
                    {
                        "stage": "process",
                        "category": "document_processing",
                        "code": "document_extraction_failed",
                        "message": "Failed to extract from PDF",
                        "retryable": False,
                        "source": {
                            "document_path": "documents/tariffs/a.pdf",
                            "model": "gpt-5",
                            "provider": "azure",
                        },
                    }
                ],
            }
        ],
    )

    assert manifest["status"]["result"] == "partial_failure"
    assert manifest["errors"]["total_errors"] == 1
    assert manifest["errors"]["by_category"] == {"document_processing": 1}
    assert manifest["errors"]["by_code"] == {"document_extraction_failed": 1}


def test_write_run_manifest_persists_expected_file(tmp_path) -> None:
    manifest = {
        "manifest_version": "1.0.0",
        "run_id": "run://deterministic1234",
        "lineage": {"artifact_id": "artifact://runtime/example"},
    }

    manifest_path = _write_run_manifest(
        output_dir=tmp_path,
        run_id="run://deterministic1234",
        manifest=manifest,
    )

    assert manifest_path == tmp_path / "run_manifests" / "deterministic1234.manifest.json"
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["run_id"] == "run://deterministic1234"
    assert saved["manifest_version"] == "1.0.0"
