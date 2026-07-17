"""Performance and quality profile aggregation for benchmark gates."""

from __future__ import annotations

import json
import csv
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional

from streamline_extract.utils.item_matcher import create_item_index
from streamline_extract.utils.item_matcher import map_key_fields_to_columns
from streamline_extract.utils.normalizers import normalize_state_column
from streamline_extract.utils.schema_metadata import SchemaMetadata

import pandas as pd


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _find_manifest_paths(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.manifest.json"))


def _resolve_record_path(
    record_ref: str, manifest_path: Path, repo_root: Path
) -> Path:
    record_path = Path(record_ref)
    if record_path.is_absolute():
        return record_path

    candidates = [repo_root / record_path]
    candidates.extend(parent / record_path for parent in manifest_path.parents)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return repo_root / record_path


def _resolve_expected_record_path(
    actual_record_path: Path, benchmark_path: Path, expected_dir: Path
) -> Path:
    try:
        relative_path = actual_record_path.relative_to(benchmark_path)
        candidate = expected_dir / relative_path
        if candidate.exists():
            return candidate
    except ValueError:
        pass

    fallback = expected_dir / actual_record_path.name
    if fallback.exists():
        return fallback

    return expected_dir / actual_record_path.name


def _find_qaqc_report_paths(path: Path) -> List[Path]:
    if path.is_file():
        return [path] if path.name == "comparison_report.csv" else []
    return sorted(path.rglob("comparison_report.csv"))


def _find_qaqc_summary_paths(path: Path) -> List[Path]:
    if path.is_file():
        return [path] if path.name == "comparison_summary.json" else []
    return sorted(path.rglob("comparison_summary.json"))


def _load_qaqc_qualitative_gate(
    summary_path: Path,
) -> Optional[Dict[str, Any]]:
    summary = _load_json(summary_path)
    gate = summary.get("summary", {}).get("qualitative_advisory_gate")
    return gate if isinstance(gate, dict) else None


def _resolve_expected_qaqc_report_path(
    actual_report_path: Path, benchmark_path: Path, expected_dir: Path
) -> Path:
    try:
        relative_path = actual_report_path.relative_to(benchmark_path)
        candidate = expected_dir / relative_path
        if candidate.exists():
            return candidate
    except ValueError:
        pass

    fallback = (
        expected_dir / actual_report_path.parent.name / actual_report_path.name
    )
    if fallback.exists():
        return fallback

    return fallback


def _load_qaqc_status_index(report_path: Path) -> Dict[str, str]:
    with report_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if (
            reader.fieldnames is None
            or "Requirement" not in reader.fieldnames
            or "Status" not in reader.fieldnames
        ):
            raise ValueError(
                f"QA/QC comparison report missing required columns Requirement/Status: {report_path}"
            )
        return {
            (row.get("Requirement") or "").strip(): (
                row.get("Status") or ""
            ).strip()
            for row in reader
            if (row.get("Requirement") or "").strip()
        }


def _find_consolidated_csv_paths(path: Path) -> List[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".csv" else []
    return sorted(
        candidate
        for candidate in path.rglob("*.csv")
        if candidate.name != "comparison_report.csv"
    )


def _resolve_expected_consolidated_path(
    actual_csv_path: Path, benchmark_path: Path, expected_dir: Path
) -> Path:
    try:
        relative_path = actual_csv_path.relative_to(benchmark_path)
        candidate = expected_dir / relative_path
        if candidate.exists():
            return candidate
    except ValueError:
        pass

    fallback = expected_dir / actual_csv_path.name
    if fallback.exists():
        return fallback

    return expected_dir / actual_csv_path.name


def _normalize_consolidated_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalize_state_column(normalized, "State")
    return normalized


def _build_consolidated_row_signatures(
    df: pd.DataFrame,
    *,
    schema_metadata: SchemaMetadata,
    compare_columns: List[str],
) -> List[tuple]:
    normalized_df = _normalize_consolidated_dataframe(df)
    if compare_columns:
        normalized_df = normalized_df[compare_columns].copy()

    key_fields = schema_metadata.get_deduplication_key_fields()
    mapped_key_columns = map_key_fields_to_columns(
        normalized_df,
        key_fields,
        warn_on_missing=False,
    )
    if not mapped_key_columns:
        raise ValueError(
            f"Schema {schema_metadata.schema_path} did not map any deduplication key fields to consolidated CSV columns"
        )

    normalized_df = normalized_df.fillna("")
    normalized_df = normalized_df.sort_values(
        by=mapped_key_columns
        + [col for col in compare_columns if col not in mapped_key_columns],
        kind="stable",
    )

    signatures = []
    for _, row in normalized_df.iterrows():
        signature = tuple(
            str(value).strip().lower() if isinstance(value, str) else value
            for value in row.tolist()
        )
        signatures.append(signature)
    return signatures


def _extract_record_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return payload

    payload = record.get("data")
    if isinstance(payload, dict):
        return payload
    return record


def _resolve_schema_path(
    record: Dict[str, Any], repo_root: Path
) -> Optional[Path]:
    schema_id = record.get("lineage", {}).get("schema_id")
    if not schema_id:
        return None

    schema_path = Path(schema_id)
    if schema_path.is_absolute():
        return schema_path
    return repo_root / schema_path


def collect_benchmark_metrics(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    extraction_baseline_dir: Optional[Path] = None,
    qaqc_baseline_dir: Optional[Path] = None,
    consolidation_baseline_dir: Optional[Path] = None,
    consolidation_schema_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Collect a deterministic performance and optional extraction-parity profile."""
    root = repo_root or Path.cwd()
    manifest_paths = _find_manifest_paths(path)
    benchmark_root = path if path.is_dir() else path.parent

    total_run_duration = 0.0
    total_documents = 0
    total_successful = 0
    total_failed = 0
    total_errors = 0
    error_categories: Dict[str, int] = {}
    record_durations: List[float] = []
    record_costs: List[float] = []
    record_error_counts: List[int] = []
    total_expected_items = 0
    total_actual_items = 0
    total_correct_items = 0
    scored_records = 0
    total_expected_qaqc_items = 0
    total_actual_qaqc_items = 0
    total_correct_qaqc_items = 0
    scored_qaqc_reports = 0
    qualitative_gate_counts: Dict[str, int] = {}
    scored_qaqc_qualitative_reports = 0
    total_expected_rows = 0
    total_actual_rows = 0
    total_correct_rows = 0
    scored_consolidated_files = 0

    for manifest_path in manifest_paths:
        manifest = _load_json(manifest_path)
        status = manifest.get("status", {})
        timing = manifest.get("timing", {})
        errors = manifest.get("errors", {})

        started_at = _parse_timestamp(timing.get("started_at"))
        finished_at = _parse_timestamp(timing.get("finished_at"))
        if started_at and finished_at:
            total_run_duration += max(
                (finished_at - started_at).total_seconds(), 0.0
            )

        total_documents += int(status.get("total_processed", 0) or 0)
        total_successful += int(status.get("successful", 0) or 0)
        total_failed += int(status.get("failed", 0) or 0)
        total_errors += int(errors.get("total_errors", 0) or 0)

        for category, count in sorted(
            (errors.get("by_category") or {}).items()
        ):
            error_categories[category] = error_categories.get(
                category, 0
            ) + int(count)

        for record_ref in manifest.get("outputs", {}).get("records", []):
            record_path = _resolve_record_path(record_ref, manifest_path, root)
            if not record_path.exists():
                continue

            record = _load_json(record_path)
            processing_metrics = record.get("processing_metrics", {})
            quality = record.get("quality", {})

            duration = processing_metrics.get("duration_seconds")
            if isinstance(duration, (int, float)):
                record_durations.append(float(duration))

            cost = processing_metrics.get("cost_usd")
            if isinstance(cost, (int, float)):
                record_costs.append(float(cost))

            errors_list = (
                quality.get("errors")
                if isinstance(quality.get("errors"), list)
                else []
            )
            record_error_counts.append(len(errors_list))

            if extraction_baseline_dir is None:
                continue

            expected_record_path = _resolve_expected_record_path(
                record_path, benchmark_root, extraction_baseline_dir
            )
            if not expected_record_path.exists():
                raise FileNotFoundError(
                    f"Expected extraction baseline record not found for {record_path.name}: {expected_record_path}"
                )

            expected_record = _load_json(expected_record_path)
            schema_path = _resolve_schema_path(
                record, root
            ) or _resolve_schema_path(expected_record, root)
            if schema_path is None or not schema_path.exists():
                raise FileNotFoundError(
                    f"Schema path required for extraction parity scoring was not found for {record_path.name}: {schema_path}"
                )

            metadata = SchemaMetadata(schema_path)
            key_fields = metadata.get_deduplication_key_fields()
            if not key_fields:
                raise ValueError(
                    f"Schema {schema_path} does not define consolidation.deduplication.key_fields required for parity scoring"
                )

            actual_payload = _extract_record_payload(record)
            expected_payload = _extract_record_payload(expected_record)
            actual_items = metadata.extract_main_data_array(actual_payload)
            expected_items = metadata.extract_main_data_array(expected_payload)

            actual_index = create_item_index(actual_items, key_fields)
            expected_index = create_item_index(expected_items, key_fields)

            total_actual_items += len(actual_index)
            total_expected_items += len(expected_index)
            total_correct_items += len(
                set(actual_index).intersection(expected_index)
            )
            scored_records += 1

    if qaqc_baseline_dir is not None:
        report_paths = _find_qaqc_report_paths(path)
        if not report_paths:
            raise FileNotFoundError(
                f"No QA/QC comparison_report.csv files found under {path}"
            )

        for report_path in report_paths:
            expected_report_path = _resolve_expected_qaqc_report_path(
                report_path, benchmark_root, qaqc_baseline_dir
            )
            if not expected_report_path.exists():
                raise FileNotFoundError(
                    f"Expected QA/QC baseline report not found for {report_path.parent.name}: {expected_report_path}"
                )

            actual_statuses = _load_qaqc_status_index(report_path)
            expected_statuses = _load_qaqc_status_index(expected_report_path)

            total_actual_qaqc_items += len(actual_statuses)
            total_expected_qaqc_items += len(expected_statuses)
            total_correct_qaqc_items += sum(
                1
                for requirement, expected_status in expected_statuses.items()
                if actual_statuses.get(requirement) == expected_status
            )
            scored_qaqc_reports += 1

    for summary_path in _find_qaqc_summary_paths(path):
        qualitative_gate = _load_qaqc_qualitative_gate(summary_path)
        if not qualitative_gate:
            continue

        gate_status = (
            str(qualitative_gate.get("status") or "not_applicable")
            .strip()
            .lower()
        )
        qualitative_gate_counts[gate_status] = (
            qualitative_gate_counts.get(gate_status, 0) + 1
        )
        scored_qaqc_qualitative_reports += 1

    if consolidation_baseline_dir is not None:
        if consolidation_schema_path is None:
            raise ValueError(
                "consolidation_schema_path is required when consolidation_baseline_dir is provided"
            )

        schema_metadata = SchemaMetadata(consolidation_schema_path)
        ignore_fields = {
            field.lower()
            for field in schema_metadata.get_deduplication_ignore_fields()
        }

        csv_paths = _find_consolidated_csv_paths(path)
        if not csv_paths:
            raise FileNotFoundError(
                f"No consolidated CSV files found under {path}"
            )

        for csv_path in csv_paths:
            expected_csv_path = _resolve_expected_consolidated_path(
                csv_path, benchmark_root, consolidation_baseline_dir
            )
            if not expected_csv_path.exists():
                raise FileNotFoundError(
                    f"Expected consolidated baseline CSV not found for {csv_path.name}: {expected_csv_path}"
                )

            actual_df = pd.read_csv(csv_path)
            expected_df = pd.read_csv(expected_csv_path)

            common_columns = [
                column
                for column in actual_df.columns
                if column in expected_df.columns
                and column.lower() not in ignore_fields
            ]
            if not common_columns:
                raise ValueError(
                    f"No shared comparable columns found between consolidated outputs for {csv_path.name}"
                )

            actual_signatures = _build_consolidated_row_signatures(
                actual_df,
                schema_metadata=schema_metadata,
                compare_columns=common_columns,
            )
            expected_signatures = _build_consolidated_row_signatures(
                expected_df,
                schema_metadata=schema_metadata,
                compare_columns=common_columns,
            )

            total_actual_rows += len(actual_signatures)
            total_expected_rows += len(expected_signatures)

            remaining_actual = list(actual_signatures)
            correct_rows = 0
            for expected_signature in expected_signatures:
                if expected_signature in remaining_actual:
                    remaining_actual.remove(expected_signature)
                    correct_rows += 1

            total_correct_rows += correct_rows
            scored_consolidated_files += 1

    average_doc_duration = (
        sum(record_durations) / len(record_durations)
        if record_durations
        else None
    )
    average_doc_cost = (
        sum(record_costs) / len(record_costs) if record_costs else None
    )
    median_doc_duration = (
        median(record_durations) if record_durations else None
    )
    median_doc_cost = median(record_costs) if record_costs else None
    max_doc_duration = max(record_durations) if record_durations else None
    throughput_docs_per_minute = None
    if total_run_duration > 0 and total_successful > 0:
        throughput_docs_per_minute = total_successful / (
            total_run_duration / 60.0
        )

    failure_rate = None
    if total_documents > 0:
        failure_rate = total_failed / total_documents

    average_record_errors = (
        sum(record_error_counts) / len(record_error_counts)
        if record_error_counts
        else 0.0
    )
    extraction_parity = None
    if extraction_baseline_dir is not None:
        extraction_parity = (
            0.0
            if total_expected_items == 0
            else (total_correct_items / total_expected_items) * 100.0
        )
    qaqc_signal_quality = None
    if qaqc_baseline_dir is not None:
        qaqc_signal_quality = (
            0.0
            if total_expected_qaqc_items == 0
            else (total_correct_qaqc_items / total_expected_qaqc_items) * 100.0
        )
    qaqc_qualitative_pass_rate = None
    if scored_qaqc_qualitative_reports > 0:
        qaqc_qualitative_pass_rate = (
            qualitative_gate_counts.get("pass", 0)
            / scored_qaqc_qualitative_reports
        ) * 100.0
    consolidation_correctness = None
    if consolidation_baseline_dir is not None:
        consolidation_correctness = (
            0.0
            if total_expected_rows == 0
            else (total_correct_rows / total_expected_rows) * 100.0
        )

    return {
        "manifest_count": len(manifest_paths),
        "total_documents": total_documents,
        "successful_documents": total_successful,
        "failed_documents": total_failed,
        "failure_rate": failure_rate,
        "total_run_duration_seconds": total_run_duration,
        "average_document_duration_seconds": average_doc_duration,
        "median_document_duration_seconds": median_doc_duration,
        "max_document_duration_seconds": max_doc_duration,
        "throughput_documents_per_minute": throughput_docs_per_minute,
        "average_document_cost_usd": average_doc_cost,
        "median_document_cost_usd": median_doc_cost,
        "total_errors": total_errors,
        "error_categories": dict(sorted(error_categories.items())),
        "average_record_errors": average_record_errors,
        "correct_items": total_correct_items,
        "expected_items": total_expected_items,
        "actual_items": total_actual_items,
        "scored_records": scored_records,
        "extraction_parity": extraction_parity,
        "correct_qaqc_classifications": total_correct_qaqc_items,
        "expected_qaqc_items": total_expected_qaqc_items,
        "actual_qaqc_items": total_actual_qaqc_items,
        "scored_qaqc_reports": scored_qaqc_reports,
        "qaqc_signal_quality": qaqc_signal_quality,
        "scored_qaqc_qualitative_reports": scored_qaqc_qualitative_reports,
        "qaqc_qualitative_gate_counts": dict(
            sorted(qualitative_gate_counts.items())
        ),
        "qaqc_qualitative_pass_rate": qaqc_qualitative_pass_rate,
        "correct_rows": total_correct_rows,
        "expected_rows": total_expected_rows,
        "actual_rows": total_actual_rows,
        "scored_consolidated_files": scored_consolidated_files,
        "consolidation_correctness": consolidation_correctness,
    }


def write_benchmark_snapshot(
    path: Path,
    *,
    metrics: Dict[str, Any],
    source_path: Path,
    label: Optional[str] = None,
) -> Path:
    """Persist benchmark metrics as a reusable snapshot for later gate comparisons."""
    snapshot = {
        "snapshot_version": "1.0.0",
        "captured_at": _utc_now_iso(),
        "label": label or source_path.name,
        "source_path": source_path.as_posix(),
        "metrics": metrics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def load_benchmark_snapshot(path: Path) -> Dict[str, Any]:
    """Load a previously captured benchmark snapshot."""
    return _load_json(path)


def compare_benchmark_to_baseline(
    metrics: Dict[str, Any],
    baseline_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute rubric-aligned benchmark deltas against a baseline snapshot."""
    baseline_metrics = baseline_snapshot.get("metrics", {})
    current_median_duration = metrics.get("median_document_duration_seconds")
    baseline_median_duration = baseline_metrics.get(
        "median_document_duration_seconds"
    )
    current_median_cost = metrics.get("median_document_cost_usd")
    baseline_median_cost = baseline_metrics.get("median_document_cost_usd")

    throughput_delta = None
    if (
        isinstance(current_median_duration, (int, float))
        and isinstance(baseline_median_duration, (int, float))
        and baseline_median_duration > 0
    ):
        throughput_delta = round(
            (
                (current_median_duration - baseline_median_duration)
                / baseline_median_duration
            )
            * 100.0,
            6,
        )

    cost_delta = None
    if (
        isinstance(current_median_cost, (int, float))
        and isinstance(baseline_median_cost, (int, float))
        and baseline_median_cost > 0
    ):
        cost_delta = round(
            (
                (current_median_cost - baseline_median_cost)
                / baseline_median_cost
            )
            * 100.0,
            6,
        )

    return {
        "baseline_label": baseline_snapshot.get("label"),
        "baseline_source_path": baseline_snapshot.get("source_path"),
        "baseline_captured_at": baseline_snapshot.get("captured_at"),
        "baseline_median_document_duration_seconds": baseline_median_duration,
        "baseline_median_document_cost_usd": baseline_median_cost,
        "throughput_delta_percent": throughput_delta,
        "cost_delta_percent": cost_delta,
    }


def evaluate_benchmark_gates(
    metrics: Dict[str, Any],
    *,
    min_extraction_parity: Optional[float] = None,
    min_qaqc_signal_quality: Optional[float] = None,
    min_qaqc_qualitative_pass_rate: Optional[float] = None,
    min_consolidation_correctness: Optional[float] = None,
    max_failure_rate: Optional[float] = None,
    max_average_seconds_per_document: Optional[float] = None,
    min_documents_per_minute: Optional[float] = None,
    max_total_errors: Optional[int] = None,
    max_throughput_delta_percent: Optional[float] = None,
    max_cost_delta_percent: Optional[float] = None,
    baseline_comparison: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate threshold-based benchmark gates against aggregated metrics."""
    gates: Dict[str, Dict[str, Any]] = {}

    if min_extraction_parity is not None:
        actual = metrics.get("extraction_parity")
        gates["min_extraction_parity"] = {
            "threshold": min_extraction_parity,
            "actual": actual,
            "passed": actual is not None and actual >= min_extraction_parity,
        }

    if min_qaqc_signal_quality is not None:
        actual = metrics.get("qaqc_signal_quality")
        gates["min_qaqc_signal_quality"] = {
            "threshold": min_qaqc_signal_quality,
            "actual": actual,
            "passed": actual is not None and actual >= min_qaqc_signal_quality,
        }

    if min_qaqc_qualitative_pass_rate is not None:
        actual = metrics.get("qaqc_qualitative_pass_rate")
        gates["min_qaqc_qualitative_pass_rate"] = {
            "threshold": min_qaqc_qualitative_pass_rate,
            "actual": actual,
            "passed": actual is not None
            and actual >= min_qaqc_qualitative_pass_rate,
        }

    if min_consolidation_correctness is not None:
        actual = metrics.get("consolidation_correctness")
        gates["min_consolidation_correctness"] = {
            "threshold": min_consolidation_correctness,
            "actual": actual,
            "passed": actual is not None
            and actual >= min_consolidation_correctness,
        }

    if max_failure_rate is not None:
        actual = metrics.get("failure_rate")
        gates["max_failure_rate"] = {
            "threshold": max_failure_rate,
            "actual": actual,
            "passed": actual is not None and actual <= max_failure_rate,
        }

    if max_average_seconds_per_document is not None:
        actual = metrics.get("average_document_duration_seconds")
        gates["max_average_seconds_per_document"] = {
            "threshold": max_average_seconds_per_document,
            "actual": actual,
            "passed": actual is not None
            and actual <= max_average_seconds_per_document,
        }

    if min_documents_per_minute is not None:
        actual = metrics.get("throughput_documents_per_minute")
        gates["min_documents_per_minute"] = {
            "threshold": min_documents_per_minute,
            "actual": actual,
            "passed": actual is not None
            and actual >= min_documents_per_minute,
        }

    if max_total_errors is not None:
        actual = metrics.get("total_errors")
        gates["max_total_errors"] = {
            "threshold": max_total_errors,
            "actual": actual,
            "passed": actual is not None and actual <= max_total_errors,
        }

    if max_throughput_delta_percent is not None:
        actual = (
            None
            if baseline_comparison is None
            else baseline_comparison.get("throughput_delta_percent")
        )
        gates["max_throughput_delta_percent"] = {
            "threshold": max_throughput_delta_percent,
            "actual": actual,
            "passed": actual is not None
            and actual <= max_throughput_delta_percent,
        }

    if max_cost_delta_percent is not None:
        actual = (
            None
            if baseline_comparison is None
            else baseline_comparison.get("cost_delta_percent")
        )
        gates["max_cost_delta_percent"] = {
            "threshold": max_cost_delta_percent,
            "actual": actual,
            "passed": actual is not None and actual <= max_cost_delta_percent,
        }

    overall_passed = None
    if gates:
        overall_passed = all(gate["passed"] for gate in gates.values())

    return {
        "overall_passed": overall_passed,
        "gates": gates,
    }
