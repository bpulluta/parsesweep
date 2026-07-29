"""Benchmark command extracted from the legacy monolith.

This module is intentionally focused on benchmark profiling and gate evaluation
so it can be migrated independently from the rest of the CLI command surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click

from psweep.benchmarking import (
    collect_benchmark_metrics,
    compare_benchmark_to_baseline,
    evaluate_benchmark_gates,
    load_benchmark_snapshot,
    write_benchmark_snapshot,
)
from psweep.cli.ui import console

_BENCHMARK_PROFILE_PATH_FIELDS = {
    "path",
    "extraction_baseline_dir",
    "qaqc_baseline_dir",
    "compilation_baseline_dir",
    "compilation_schema",
    "baseline_snapshot",
    "write_snapshot",
}


def _load_benchmark_gate_profile(profile_path: Path) -> Dict[str, Any]:
    """Load a benchmark gate profile and resolve relative paths against its directory."""
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.UsageError(
            f"Invalid benchmark gate profile JSON: {profile_path}: {exc}"
        ) from exc

    if not isinstance(profile, dict):
        raise click.UsageError(
            f"Benchmark gate profile must be a JSON object: {profile_path}"
        )

    resolved: Dict[str, Any] = {}
    for key, value in profile.items():
        if key in _BENCHMARK_PROFILE_PATH_FIELDS and value is not None:
            resolved[key] = (
                Path(value)
                if Path(value).is_absolute()
                else (profile_path.parent / value)
            )
        else:
            resolved[key] = value
    return resolved


def _coalesce_benchmark_option(
    cli_value: Any, profile: Dict[str, Any], key: str
) -> Any:
    """Prefer explicit CLI value, then fallback to gate profile."""
    return cli_value if cli_value is not None else profile.get(key)


@click.command(name="benchmark")
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option(
    "--gate-profile",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JSON file containing benchmark input paths and gate thresholds for reproducible release evaluation.",
)
@click.option(
    "--extraction-baseline-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing expected extraction JSON records for parity scoring. Files should mirror benchmark output relative paths or record filenames.",
)
@click.option(
    "--qaqc-baseline-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing expected QA/QC comparison_report.csv files for signal-quality scoring. Files should mirror benchmark document-folder relative paths.",
)
@click.option(
    "--compilation-baseline-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing expected compiled CSV outputs for row-correctness scoring. Files should mirror benchmark CSV relative paths or filenames.",
)
@click.option(
    "--compilation-schema",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Schema used to generate compiled outputs. Required for compilation correctness scoring.",
)
@click.option(
    "--baseline-snapshot",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a saved benchmark snapshot used for median throughput/cost delta comparison.",
)
@click.option(
    "--write-snapshot",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the current benchmark metrics to a snapshot JSON file.",
)
@click.option(
    "--snapshot-label",
    type=str,
    default=None,
    help="Optional label to store in a written benchmark snapshot.",
)
@click.option(
    "--min-extraction-parity",
    type=float,
    default=None,
    help="Minimum required extraction parity percentage (0-100) when expected extraction records are provided.",
)
@click.option(
    "--min-qaqc-signal-quality",
    type=float,
    default=None,
    help="Minimum required QA/QC signal quality percentage (0-100) when expected comparison reports are provided.",
)
@click.option(
    "--min-qaqc-qualitative-pass-rate",
    type=float,
    default=None,
    help="Minimum required percentage (0-100) of qualitative QA/QC comparison summaries whose advisory gate status is pass.",
)
@click.option(
    "--min-compilation-correctness",
    type=float,
    default=None,
    help="Minimum required compilation correctness percentage (0-100) when expected compiled CSVs are provided.",
)
@click.option(
    "--max-failure-rate",
    type=float,
    default=None,
    help="Maximum allowed failed-document rate (0-1).",
)
@click.option(
    "--max-average-seconds-per-document",
    type=float,
    default=None,
    help="Maximum allowed average processing seconds per document.",
)
@click.option(
    "--min-documents-per-minute",
    type=float,
    default=None,
    help="Minimum required successful document throughput.",
)
@click.option(
    "--max-total-errors",
    type=int,
    default=None,
    help="Maximum allowed total structured errors across manifests.",
)
@click.option(
    "--max-throughput-delta-percent",
    type=float,
    default=None,
    help="Maximum allowed median time-per-document increase versus the baseline snapshot.",
)
@click.option(
    "--max-cost-delta-percent",
    type=float,
    default=None,
    help="Maximum allowed median cost-per-document increase versus the baseline snapshot.",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
def benchmark(
    path: Optional[Path],
    gate_profile: Optional[Path],
    extraction_baseline_dir: Optional[Path],
    qaqc_baseline_dir: Optional[Path],
    compilation_baseline_dir: Optional[Path],
    compilation_schema: Optional[Path],
    baseline_snapshot: Optional[Path],
    write_snapshot: Optional[Path],
    snapshot_label: Optional[str],
    min_extraction_parity: Optional[float],
    min_qaqc_signal_quality: Optional[float],
    min_qaqc_qualitative_pass_rate: Optional[float],
    min_compilation_correctness: Optional[float],
    max_failure_rate: Optional[float],
    max_average_seconds_per_document: Optional[float],
    min_documents_per_minute: Optional[float],
    max_total_errors: Optional[int],
    max_throughput_delta_percent: Optional[float],
    max_cost_delta_percent: Optional[float],
    quiet: bool,
    verbose: bool,
) -> None:
    """Build a performance profile from run manifests and evaluate benchmark gates."""
    from psweep.cli.commands import begin_run

    profile_values = (
        _load_benchmark_gate_profile(gate_profile)
        if gate_profile is not None
        else {}
    )

    benchmark_path = _coalesce_benchmark_option(path, profile_values, "path")
    extraction_baseline_dir = _coalesce_benchmark_option(
        extraction_baseline_dir, profile_values, "extraction_baseline_dir"
    )
    qaqc_baseline_dir = _coalesce_benchmark_option(
        qaqc_baseline_dir, profile_values, "qaqc_baseline_dir"
    )
    compilation_baseline_dir = _coalesce_benchmark_option(
        compilation_baseline_dir, profile_values, "compilation_baseline_dir"
    )
    compilation_schema = _coalesce_benchmark_option(
        compilation_schema, profile_values, "compilation_schema"
    )
    baseline_snapshot = _coalesce_benchmark_option(
        baseline_snapshot, profile_values, "baseline_snapshot"
    )
    write_snapshot = _coalesce_benchmark_option(
        write_snapshot, profile_values, "write_snapshot"
    )
    snapshot_label = _coalesce_benchmark_option(
        snapshot_label, profile_values, "snapshot_label"
    )
    min_extraction_parity = _coalesce_benchmark_option(
        min_extraction_parity, profile_values, "min_extraction_parity"
    )
    min_qaqc_signal_quality = _coalesce_benchmark_option(
        min_qaqc_signal_quality, profile_values, "min_qaqc_signal_quality"
    )
    min_qaqc_qualitative_pass_rate = _coalesce_benchmark_option(
        min_qaqc_qualitative_pass_rate, profile_values, "min_qaqc_qualitative_pass_rate"
    )
    min_compilation_correctness = _coalesce_benchmark_option(
        min_compilation_correctness, profile_values, "min_compilation_correctness"
    )
    max_failure_rate = _coalesce_benchmark_option(
        max_failure_rate, profile_values, "max_failure_rate"
    )
    max_average_seconds_per_document = _coalesce_benchmark_option(
        max_average_seconds_per_document, profile_values, "max_average_seconds_per_document"
    )
    min_documents_per_minute = _coalesce_benchmark_option(
        min_documents_per_minute, profile_values, "min_documents_per_minute"
    )
    max_total_errors = _coalesce_benchmark_option(
        max_total_errors, profile_values, "max_total_errors"
    )
    max_throughput_delta_percent = _coalesce_benchmark_option(
        max_throughput_delta_percent, profile_values, "max_throughput_delta_percent"
    )
    max_cost_delta_percent = _coalesce_benchmark_option(
        max_cost_delta_percent, profile_values, "max_cost_delta_percent"
    )

    if benchmark_path is None:
        raise click.UsageError(
            "benchmark requires PATH or --gate-profile with a path entry"
        )
    benchmark_path = Path(benchmark_path)
    if not benchmark_path.exists():
        raise click.UsageError(f"Benchmark path not found: {benchmark_path}")

    if min_extraction_parity is not None and extraction_baseline_dir is None:
        raise click.UsageError(
            "--min-extraction-parity requires --extraction-baseline-dir"
        )
    if min_qaqc_signal_quality is not None and qaqc_baseline_dir is None:
        raise click.UsageError(
            "--min-qaqc-signal-quality requires --qaqc-baseline-dir"
        )
    if min_compilation_correctness is not None and compilation_baseline_dir is None:
        raise click.UsageError(
            "--min-compilation-correctness requires --compilation-baseline-dir"
        )
    if compilation_baseline_dir is not None and compilation_schema is None:
        raise click.UsageError(
            "--compilation-baseline-dir requires --compilation-schema"
        )

    metrics = collect_benchmark_metrics(
        benchmark_path,
        repo_root=Path.cwd(),
        extraction_baseline_dir=extraction_baseline_dir,
        qaqc_baseline_dir=qaqc_baseline_dir,
        compilation_baseline_dir=compilation_baseline_dir,
        compilation_schema_path=compilation_schema,
    )
    baseline_comparison = None
    if baseline_snapshot:
        baseline_comparison = compare_benchmark_to_baseline(
            metrics,
            load_benchmark_snapshot(Path(baseline_snapshot)),
        )

    snapshot_path = None
    if write_snapshot:
        snapshot_path = write_benchmark_snapshot(
            Path(write_snapshot),
            metrics=metrics,
            source_path=benchmark_path,
            label=snapshot_label,
        )

    gate_result = evaluate_benchmark_gates(
        metrics,
        min_extraction_parity=min_extraction_parity,
        min_qaqc_signal_quality=min_qaqc_signal_quality,
        min_qaqc_qualitative_pass_rate=min_qaqc_qualitative_pass_rate,
        min_compilation_correctness=min_compilation_correctness,
        max_failure_rate=max_failure_rate,
        max_average_seconds_per_document=max_average_seconds_per_document,
        min_documents_per_minute=min_documents_per_minute,
        max_total_errors=max_total_errors,
        max_throughput_delta_percent=max_throughput_delta_percent,
        max_cost_delta_percent=max_cost_delta_percent,
        baseline_comparison=baseline_comparison,
    )

    if quiet:
        console.print(
            json.dumps(
                {
                    "metrics": metrics,
                    "baseline_comparison": baseline_comparison,
                    "gates": gate_result,
                    "snapshot_path": None
                    if snapshot_path is None
                    else snapshot_path.as_posix(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        if gate_result["overall_passed"] is False:
            raise SystemExit(1)
        return

    view = begin_run("benchmark", quiet=quiet, verbose=verbose)

    view.header("PERFORMANCE BENCHMARK")
    config_info = {
        "Input": str(benchmark_path),
        "Run Manifests": str(metrics["manifest_count"]),
        "Documents": str(metrics["total_documents"]),
    }
    if gate_profile is not None:
        config_info["Gate Profile"] = str(gate_profile)
    view.config(config_info, title="Benchmark Input")

    summary_stats = {
        "Extraction Parity": f"{metrics['extraction_parity']:.2f}%"
        if metrics["extraction_parity"] is not None
        else "N/A",
        "QA/QC Signal Quality": f"{metrics['qaqc_signal_quality']:.2f}%"
        if metrics["qaqc_signal_quality"] is not None
        else "N/A",
        "QA/QC Qualitative Pass Rate": f"{metrics['qaqc_qualitative_pass_rate']:.2f}%"
        if metrics["qaqc_qualitative_pass_rate"] is not None
        else "N/A",
        "Compilation Correctness": f"{metrics['compilation_correctness']:.2f}%"
        if metrics["compilation_correctness"] is not None
        else "N/A",
        "Successful Documents": str(metrics["successful_documents"]),
        "Failed Documents": str(metrics["failed_documents"]),
        "Failure Rate": f"{(metrics['failure_rate'] or 0.0) * 100:.1f}%",
        "Total Run Time": f"{metrics['total_run_duration_seconds']:.1f}s",
        "Average Doc Time": f"{metrics['average_document_duration_seconds']:.2f}s"
        if metrics["average_document_duration_seconds"] is not None
        else "N/A",
        "Median Doc Time": f"{metrics['median_document_duration_seconds']:.2f}s"
        if metrics["median_document_duration_seconds"] is not None
        else "N/A",
        "Max Doc Time": f"{metrics['max_document_duration_seconds']:.2f}s"
        if metrics["max_document_duration_seconds"] is not None
        else "N/A",
        "Throughput": f"{metrics['throughput_documents_per_minute']:.2f} docs/min"
        if metrics["throughput_documents_per_minute"] is not None
        else "N/A",
        "Average Doc Cost": f"${metrics['average_document_cost_usd']:.4f}"
        if metrics["average_document_cost_usd"] is not None
        else "N/A",
        "Median Doc Cost": f"${metrics['median_document_cost_usd']:.4f}"
        if metrics["median_document_cost_usd"] is not None
        else "N/A",
        "Total Errors": str(metrics["total_errors"]),
    }
    view.summary(summary_stats, title="Performance Profile")

    if baseline_comparison is not None:
        baseline_stats = {
            "Baseline Label": baseline_comparison["baseline_label"] or "N/A",
            "Baseline Median Doc Time": f"{baseline_comparison['baseline_median_document_duration_seconds']:.2f}s"
            if baseline_comparison["baseline_median_document_duration_seconds"] is not None
            else "N/A",
            "Baseline Median Doc Cost": f"${baseline_comparison['baseline_median_document_cost_usd']:.4f}"
            if baseline_comparison["baseline_median_document_cost_usd"] is not None
            else "N/A",
            "Throughput Delta": f"{baseline_comparison['throughput_delta_percent']:+.2f}%"
            if baseline_comparison["throughput_delta_percent"] is not None
            else "N/A",
            "Cost Delta": f"{baseline_comparison['cost_delta_percent']:+.2f}%"
            if baseline_comparison["cost_delta_percent"] is not None
            else "N/A",
        }
        view.summary(baseline_stats, title="Baseline Comparison")

    if verbose and metrics["error_categories"]:
        view.summary(metrics["error_categories"], title="Error Categories")

    if verbose and metrics["qaqc_qualitative_gate_counts"]:
        view.summary(
            metrics["qaqc_qualitative_gate_counts"],
            title="QA/QC Qualitative Gates",
        )

    if snapshot_path is not None:
        view.status("info", f"Snapshot written to: {snapshot_path}")

    if gate_result["gates"]:
        view.section("Benchmark Gates")
        for gate_name, gate in gate_result["gates"].items():
            view.status(
                "success" if gate["passed"] else "error",
                gate_name,
                f"actual={gate['actual']}, threshold={gate['threshold']}",
            )
        if gate_result["overall_passed"]:
            view.success("Benchmark gates passed")
        else:
            view.warning("Benchmark gates failed")
            raise SystemExit(1)
    else:
        view.info("No thresholds supplied; reported metrics only")
