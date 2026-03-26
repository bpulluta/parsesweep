"""Tests for run-manifest benchmark profile aggregation and gate evaluation."""

import json
from pathlib import Path

from click.testing import CliRunner

from streamline_extract.benchmarking.performance import (
    collect_benchmark_metrics,
    compare_benchmark_to_baseline,
    evaluate_benchmark_gates,
    load_benchmark_snapshot,
)
from streamline_extract.cli.main import cli


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")


def test_collect_benchmark_metrics_aggregates_run_and_record_metrics(tmp_path) -> None:
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "processed/tariffs/doc-b.json",
        {
            "quality": {"errors": [{"category": "document_processing"}]},
            "processing_metrics": {"duration_seconds": 7.0, "cost_usd": 0.30},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 2,
                "successful": 2,
                "failed": 0,
            },
            "errors": {
                "total_errors": 1,
                "by_category": {"document_processing": 1},
            },
            "outputs": {
                "records": [
                    "processed/tariffs/doc-a.json",
                    "processed/tariffs/doc-b.json",
                ]
            },
        },
    )

    metrics = collect_benchmark_metrics(tmp_path / "processed", repo_root=tmp_path)

    assert metrics["manifest_count"] == 1
    assert metrics["total_documents"] == 2
    assert metrics["successful_documents"] == 2
    assert metrics["failed_documents"] == 0
    assert metrics["total_run_duration_seconds"] == 60.0
    assert metrics["average_document_duration_seconds"] == 6.0
    assert metrics["median_document_duration_seconds"] == 6.0
    assert metrics["max_document_duration_seconds"] == 7.0
    assert metrics["average_document_cost_usd"] == 0.2
    assert metrics["median_document_cost_usd"] == 0.2
    assert metrics["throughput_documents_per_minute"] == 2.0
    assert metrics["total_errors"] == 1
    assert metrics["error_categories"] == {"document_processing": 1}
    assert metrics["average_record_errors"] == 0.5
    assert metrics["extraction_parity"] is None


def test_collect_benchmark_metrics_computes_extraction_parity(tmp_path) -> None:
    _write_json(
        tmp_path / "schemas/test_schema.json",
        {
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["document_id"],
                },
                "consolidation": {
                    "deduplication": {
                        "key_fields": ["name", "value"],
                        "ignore_fields": [],
                    }
                },
            },
            "type": "object",
        },
    )
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "data": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                    {"name": "offpeak", "value": 2},
                ],
            },
            "lineage": {"schema_id": "schemas/test_schema.json"},
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "expected/tariffs/doc-a.json",
        {
            "data": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                    {"name": "offpeak", "value": 9},
                ],
            },
            "lineage": {"schema_id": "schemas/test_schema.json"},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    metrics = collect_benchmark_metrics(
        tmp_path / "processed",
        repo_root=tmp_path,
        extraction_baseline_dir=tmp_path / "expected",
    )

    assert metrics["expected_items"] == 2
    assert metrics["actual_items"] == 2
    assert metrics["correct_items"] == 1
    assert metrics["scored_records"] == 1
    assert metrics["extraction_parity"] == 50.0


def test_collect_benchmark_metrics_computes_extraction_parity_from_payload_records(tmp_path) -> None:
    schema_path = tmp_path / "schemas/test_schema.json"
    _write_json(
        schema_path,
        {
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["document_id"],
                },
                "consolidation": {
                    "deduplication": {
                        "key_fields": ["name", "value"],
                        "ignore_fields": [],
                    }
                },
            },
            "type": "object",
        },
    )
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "payload": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                ],
            },
            "lineage": {"schema_id": str(schema_path)},
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "expected/tariffs/doc-a.json",
        {
            "payload": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                ],
            },
            "lineage": {"schema_id": str(schema_path)},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    metrics = collect_benchmark_metrics(
        tmp_path / "processed",
        repo_root=tmp_path,
        extraction_baseline_dir=tmp_path / "expected",
    )

    assert metrics["correct_items"] == 1
    assert metrics["expected_items"] == 1
    assert metrics["extraction_parity"] == 100.0


def test_collect_benchmark_metrics_computes_qaqc_signal_quality(tmp_path) -> None:
    actual_report = tmp_path / "processed/qa_qc/test-doc/comparison_report.csv"
    actual_report.parent.mkdir(parents=True, exist_ok=True)
    actual_report.write_text(
        "Status,Requirement,Agreement,Notes\n"
        "AGREE,setback__property_line_ft,100%,\n"
        "ONLY gpt-5,time__reclamation_deadline_days,-,Not in: gpt-4.1\n",
        encoding="utf-8",
    )

    expected_report = tmp_path / "expected/qa_qc/test-doc/comparison_report.csv"
    expected_report.parent.mkdir(parents=True, exist_ok=True)
    expected_report.write_text(
        "Status,Requirement,Agreement,Notes\n"
        "AGREE,setback__property_line_ft,100%,\n"
        "DIFFER,time__reclamation_deadline_days,0%,Values differ\n",
        encoding="utf-8",
    )

    metrics = collect_benchmark_metrics(
        tmp_path / "processed/qa_qc",
        repo_root=tmp_path,
        qaqc_baseline_dir=tmp_path / "expected/qa_qc",
    )

    assert metrics["expected_qaqc_items"] == 2
    assert metrics["actual_qaqc_items"] == 2
    assert metrics["correct_qaqc_classifications"] == 1
    assert metrics["scored_qaqc_reports"] == 1
    assert metrics["qaqc_signal_quality"] == 50.0


def test_collect_benchmark_metrics_computes_consolidation_correctness(tmp_path) -> None:
    schema_path = tmp_path / "schemas/test_schema.json"
    _write_json(
        schema_path,
        {
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["metadata.id"],
                },
                "consolidation": {
                    "deduplication": {
                        "key_fields": ["name", "state"],
                        "ignore_fields": ["notes"],
                    }
                },
            },
            "type": "object",
        },
    )

    actual_csv = tmp_path / "actual/tariffs.csv"
    actual_csv.parent.mkdir(parents=True, exist_ok=True)
    actual_csv.write_text(
        "Name,State,Value,Notes\n"
        "Charge A,Utah,10,merged duplicate\n"
        "Charge C,CO,30,\n",
        encoding="utf-8",
    )

    expected_csv = tmp_path / "expected/tariffs.csv"
    expected_csv.parent.mkdir(parents=True, exist_ok=True)
    expected_csv.write_text(
        "Name,State,Value,Notes\n"
        "Charge A,UT,10,\n"
        "Charge B,CO,20,\n",
        encoding="utf-8",
    )

    metrics = collect_benchmark_metrics(
        tmp_path / "actual",
        repo_root=tmp_path,
        consolidation_baseline_dir=tmp_path / "expected",
        consolidation_schema_path=schema_path,
    )

    assert metrics["expected_rows"] == 2
    assert metrics["actual_rows"] == 2
    assert metrics["correct_rows"] == 1
    assert metrics["scored_consolidated_files"] == 1
    assert metrics["consolidation_correctness"] == 50.0


def test_compare_benchmark_to_baseline_returns_median_deltas() -> None:
    comparison = compare_benchmark_to_baseline(
        {
            "median_document_duration_seconds": 12.0,
            "median_document_cost_usd": 0.33,
        },
        {
            "label": "phase0-baseline",
            "source_path": "output/baseline",
            "captured_at": "2026-03-25T00:00:00Z",
            "metrics": {
                "median_document_duration_seconds": 10.0,
                "median_document_cost_usd": 0.30,
            },
        },
    )

    assert comparison["baseline_label"] == "phase0-baseline"
    assert comparison["throughput_delta_percent"] == 20.0
    assert comparison["cost_delta_percent"] == 10.0


def test_evaluate_benchmark_gates_returns_pass_fail_summary() -> None:
    metrics = {
        "extraction_parity": 98.0,
        "qaqc_signal_quality": 97.0,
        "consolidation_correctness": 99.6,
        "failure_rate": 0.05,
        "average_document_duration_seconds": 8.0,
        "throughput_documents_per_minute": 4.0,
        "total_errors": 1,
    }

    gate_result = evaluate_benchmark_gates(
        metrics,
        min_extraction_parity=97.0,
        min_qaqc_signal_quality=96.0,
        min_consolidation_correctness=99.5,
        max_failure_rate=0.10,
        max_average_seconds_per_document=10.0,
        min_documents_per_minute=2.0,
        max_total_errors=2,
    )

    assert gate_result["overall_passed"] is True
    assert all(gate["passed"] for gate in gate_result["gates"].values())


def test_evaluate_benchmark_gates_detects_failures() -> None:
    metrics = {
        "extraction_parity": 92.0,
        "qaqc_signal_quality": 90.0,
        "consolidation_correctness": 95.0,
        "failure_rate": 0.20,
        "average_document_duration_seconds": 12.0,
        "throughput_documents_per_minute": 1.0,
        "total_errors": 3,
    }

    gate_result = evaluate_benchmark_gates(
        metrics,
        min_extraction_parity=97.0,
        min_qaqc_signal_quality=96.0,
        min_consolidation_correctness=99.5,
        max_failure_rate=0.10,
        max_average_seconds_per_document=10.0,
        min_documents_per_minute=2.0,
        max_total_errors=2,
    )

    assert gate_result["overall_passed"] is False
    assert gate_result["gates"]["min_extraction_parity"]["passed"] is False
    assert gate_result["gates"]["min_qaqc_signal_quality"]["passed"] is False
    assert gate_result["gates"]["min_consolidation_correctness"]["passed"] is False
    assert gate_result["gates"]["max_failure_rate"]["passed"] is False
    assert gate_result["gates"]["max_average_seconds_per_document"]["passed"] is False
    assert gate_result["gates"]["min_documents_per_minute"]["passed"] is False
    assert gate_result["gates"]["max_total_errors"]["passed"] is False


def test_evaluate_benchmark_gates_detects_baseline_delta_failures() -> None:
    gate_result = evaluate_benchmark_gates(
        {},
        max_throughput_delta_percent=15.0,
        max_cost_delta_percent=10.0,
        baseline_comparison={
            "throughput_delta_percent": 20.0,
            "cost_delta_percent": 12.0,
        },
    )

    assert gate_result["overall_passed"] is False
    assert gate_result["gates"]["max_throughput_delta_percent"]["passed"] is False
    assert gate_result["gates"]["max_cost_delta_percent"]["passed"] is False


def test_benchmark_cli_returns_nonzero_when_gate_fails(tmp_path) -> None:
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 12.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "processed"),
            "--max-average-seconds-per-document",
            "10",
        ],
    )

    assert result.exit_code == 1
    assert "Benchmark gates failed" in result.output


def test_benchmark_cli_requires_baseline_dir_for_extraction_parity_gate(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path),
            "--min-extraction-parity",
            "97",
        ],
    )

    assert result.exit_code != 0
    assert "requires --extraction-baseline-dir" in result.output


def test_benchmark_cli_requires_baseline_dir_for_qaqc_gate(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path),
            "--min-qaqc-signal-quality",
            "96",
        ],
    )

    assert result.exit_code != 0
    assert "requires --qaqc-baseline-dir" in result.output


def test_benchmark_cli_requires_baseline_dir_for_consolidation_gate(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path),
            "--min-consolidation-correctness",
            "99.5",
        ],
    )

    assert result.exit_code != 0
    assert "requires --consolidation-baseline-dir" in result.output


def test_benchmark_cli_requires_schema_for_consolidation_baseline(tmp_path) -> None:
    runner = CliRunner()
    baseline_dir = tmp_path / "expected"
    baseline_dir.mkdir()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path),
            "--consolidation-baseline-dir",
            str(baseline_dir),
        ],
    )

    assert result.exit_code != 0
    assert "requires --consolidation-schema" in result.output


def test_benchmark_cli_quiet_mode_emits_json(tmp_path) -> None:
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["benchmark", str(tmp_path / "processed"), "--quiet"])

    assert result.exit_code == 0
    assert '"metrics"' in result.output
    assert '"manifest_count": 1' in result.output


def test_benchmark_cli_writes_and_reuses_snapshot(tmp_path) -> None:
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    snapshot_path = tmp_path / "modernization/tracking/baseline_snapshot.json"
    runner = CliRunner()

    write_result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "processed"),
            "--write-snapshot",
            str(snapshot_path),
            "--snapshot-label",
            "phase0-baseline",
            "--quiet",
        ],
    )

    assert write_result.exit_code == 0
    snapshot = load_benchmark_snapshot(snapshot_path)
    assert snapshot["label"] == "phase0-baseline"
    assert snapshot["metrics"]["median_document_duration_seconds"] == 5.0

    compare_result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "processed"),
            "--baseline-snapshot",
            str(snapshot_path),
            "--max-throughput-delta-percent",
            "15",
            "--max-cost-delta-percent",
            "10",
            "--quiet",
        ],
    )

    assert compare_result.exit_code == 0
    assert '"baseline_comparison"' in compare_result.output
    assert '"throughput_delta_percent": 0.0' in compare_result.output


def test_benchmark_cli_reports_extraction_parity_in_quiet_mode(tmp_path) -> None:
    schema_path = tmp_path / "schemas/test_schema.json"
    _write_json(
        schema_path,
        {
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["document_id"],
                },
                "consolidation": {
                    "deduplication": {
                        "key_fields": ["name", "value"],
                        "ignore_fields": [],
                    }
                },
            },
            "type": "object",
        },
    )
    _write_json(
        tmp_path / "processed/tariffs/doc-a.json",
        {
            "data": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                ],
            },
            "lineage": {"schema_id": str(schema_path)},
            "quality": {"errors": []},
            "processing_metrics": {"duration_seconds": 5.0, "cost_usd": 0.10},
        },
    )
    _write_json(
        tmp_path / "expected/tariffs/doc-a.json",
        {
            "data": {
                "document_id": "doc-a",
                "items": [
                    {"name": "peak", "value": 1},
                ],
            },
            "lineage": {"schema_id": str(schema_path)},
        },
    )
    _write_json(
        tmp_path / "processed/run_manifests/sample.manifest.json",
        {
            "timing": {
                "started_at": "2026-03-25T10:00:00Z",
                "finished_at": "2026-03-25T10:01:00Z",
            },
            "status": {
                "total_processed": 1,
                "successful": 1,
                "failed": 0,
            },
            "errors": {
                "total_errors": 0,
                "by_category": {},
            },
            "outputs": {
                "records": ["processed/tariffs/doc-a.json"]
            },
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "processed"),
            "--extraction-baseline-dir",
            str(tmp_path / "expected"),
            "--min-extraction-parity",
            "97",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert '"extraction_parity": 100.0' in result.output
    assert '"min_extraction_parity"' in result.output


def test_benchmark_cli_reports_qaqc_signal_quality_in_quiet_mode(tmp_path) -> None:
    actual_report = tmp_path / "processed/qa_qc/test-doc/comparison_report.csv"
    actual_report.parent.mkdir(parents=True, exist_ok=True)
    actual_report.write_text(
        "Status,Requirement,Agreement,Notes\n"
        "AGREE,setback__property_line_ft,100%,\n",
        encoding="utf-8",
    )

    expected_report = tmp_path / "expected/qa_qc/test-doc/comparison_report.csv"
    expected_report.parent.mkdir(parents=True, exist_ok=True)
    expected_report.write_text(
        "Status,Requirement,Agreement,Notes\n"
        "AGREE,setback__property_line_ft,100%,\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "processed/qa_qc"),
            "--qaqc-baseline-dir",
            str(tmp_path / "expected/qa_qc"),
            "--min-qaqc-signal-quality",
            "96",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert '"qaqc_signal_quality": 100.0' in result.output
    assert '"min_qaqc_signal_quality"' in result.output


def test_benchmark_cli_reports_consolidation_correctness_in_quiet_mode(tmp_path) -> None:
    schema_path = tmp_path / "schemas/test_schema.json"
    _write_json(
        schema_path,
        {
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["metadata.id"],
                },
                "consolidation": {
                    "deduplication": {
                        "key_fields": ["name", "state"],
                        "ignore_fields": ["notes"],
                    }
                },
            },
            "type": "object",
        },
    )

    actual_csv = tmp_path / "actual/tariffs.csv"
    actual_csv.parent.mkdir(parents=True, exist_ok=True)
    actual_csv.write_text(
        "Name,State,Value,Notes\n"
        "Charge A,UT,10,merged duplicate\n",
        encoding="utf-8",
    )

    expected_csv = tmp_path / "expected/tariffs.csv"
    expected_csv.parent.mkdir(parents=True, exist_ok=True)
    expected_csv.write_text(
        "Name,State,Value,Notes\n"
        "Charge A,UT,10,\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "benchmark",
            str(tmp_path / "actual"),
            "--consolidation-baseline-dir",
            str(tmp_path / "expected"),
            "--consolidation-schema",
            str(schema_path),
            "--min-consolidation-correctness",
            "99.5",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert '"consolidation_correctness": 100.0' in result.output
    assert '"min_consolidation_correctness"' in result.output