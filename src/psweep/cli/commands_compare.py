"""`compare` command extracted from the legacy CLI monolith."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

import contextlib

from psweep.cli.ui import console, create_extraction_progress, print_error


@click.command()
@click.argument("qa_qc_path", type=click.Path(exists=True))
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain run config file to load QA/QC lanes/defaults.",
)
@click.option(
    "--schema",
    "-s",
    type=click.Path(exists=True),
    required=True,
    help="Path to QA/QC schema file (REQUIRED)",
)
@click.option(
    "--qaqc-lane",
    type=str,
    default=None,
    help="Optional runtime QA/QC lane name to compare, including disabled evaluation lanes such as qualitative",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
def compare(
    qa_qc_path: str,
    config_path: Optional[str],
    schema: str,
    qaqc_lane: Optional[str],
    quiet: bool,
    verbose: bool,
) -> None:
    """
    Generate comparison reports from existing QA/QC extractions.

    This command compares outputs from multiple models that were previously
    extracted with --enable-qa-qc, without re-running the expensive extractions.

    \b
    EXAMPLES:
        # Generate comparison reports for all documents
        psweep compare extracted/qa_qc_test/qa_qc --schema schemas/personal/geothermal_ordinance_schema.json

        # Compare specific document folder
        psweep compare "extracted/qa_qc_test/qa_qc/Chaffee County Colorado" --schema schemas/personal/geothermal_ordinance_schema.json

    \b
    OUTPUT (per document):
        • comparison_report.xlsx - Color-coded Excel with agreement analysis
        • comparison_report.csv - Plain CSV for data analysis

    \b
    WORKFLOW:
        1. Run extraction with QA/QC: psweep extract docs/ --schema schema.json --enable-qa-qc
        2. Generate/update reports: psweep compare extracted/docs/qa_qc --schema schema.json
    """
    from psweep.cli.commands import (
        _format_runtime_artifact_summary,
        begin_run,
    )
    from psweep.config import load_runtime_config_file
    from psweep.qa_qc import ComparisonEngine, ReportGenerator
    from psweep.qa_qc.utils import resolve_qaqc_runtime_config
    from psweep.utils.schema_metadata import SchemaMetadata

    qa_qc_path_obj = Path(qa_qc_path)
    schema_path = Path(schema)

    view = begin_run("compare", quiet=quiet, verbose=verbose)

    try:
        schema_metadata = SchemaMetadata(schema_path)
        runtime_artifact = None
        runtime_qaqc = None
        if config_path:
            config_data = load_runtime_config_file(Path(config_path))
            config_qaqc = config_data.get("qaqc")
            if isinstance(config_qaqc, dict):
                runtime_qaqc = config_qaqc
        qa_qc_config = resolve_qaqc_runtime_config(
            schema_metadata,
            runtime_artifact=runtime_artifact,
            runtime_qaqc=runtime_qaqc,
            preferred_lane=qaqc_lane,
        )
    except Exception as exc:
        view.error("Failed to load schema", str(exc))
        sys.exit(1)

    view.header("QA/QC COMPARISON REPORT")
    view.config(
        {
            "Input": str(qa_qc_path_obj),
            "Schema": str(schema_path),
            "Runtime": _format_runtime_artifact_summary(runtime_artifact),
            "QA/QC Config": qa_qc_config["source"],
            "Requested QA/QC Lane": qaqc_lane or "(default resolution)",
            "QA/QC Lane": qa_qc_config.get("lane_name") or "schema fallback",
            "Comparison Approach": qa_qc_config.get("comparison_approach")
            or "numeric_only",
            "Match Fields": ", ".join(qa_qc_config["match_fields"]),
            "Compare Fields": ", ".join(qa_qc_config["compare_fields"]),
        }
    )

    engine = ComparisonEngine(schema_metadata, qa_qc_config=qa_qc_config)
    report_gen = ReportGenerator()

    doc_dirs = []
    json_files_in_path = list(qa_qc_path_obj.glob("*.json"))
    ignored_qaqc_json_files = {"metadata.json", "comparison_summary.json"}

    if json_files_in_path and any(
        f.name not in ignored_qaqc_json_files for f in json_files_in_path
    ):
        doc_dirs = [qa_qc_path_obj]
    else:
        doc_dirs = [d for d in sorted(qa_qc_path_obj.iterdir()) if d.is_dir()]

    if not doc_dirs:
        print_error(
            "No QA/QC outputs found",
            f"No document directories found in {qa_qc_path_obj}",
            [
                "Run extraction with --enable-qa-qc first",
                "Check the path is correct",
            ],
        )
        sys.exit(1)

    results = []
    view.phase("Loading QA/QC extractions")

    _progress = create_extraction_progress() if not view.is_quiet else None
    _progress_ctx = _progress if _progress is not None else contextlib.nullcontext()
    _task = None
    _writing_phase_announced = False

    with _progress_ctx:
        if _progress is not None:
            _task = _progress.add_task("Comparing", total=len(doc_dirs))

        for doc_dir in doc_dirs:
            if _progress is not None:
                _progress.update(_task, description=f"Comparing {doc_dir.name[:50]}")

            model_files = {}
            for file_path in doc_dir.glob("*.json"):
                if file_path.name in ignored_qaqc_json_files:
                    continue
                model_files[file_path.stem] = file_path

            if len(model_files) < 2:
                view.status(
                    "warning",
                    f"Skipping {doc_dir.name}: needs at least 2 model outputs",
                )
                if _progress is not None:
                    _progress.advance(_task)
                continue

            try:
                result = engine.compare_outputs(model_files, doc_dir.name)

                if not _writing_phase_announced and not view.is_quiet:
                    view.phase("Generating comparison reports")
                    _writing_phase_announced = True

                if _progress is not None:
                    _progress.update(
                        _task, description=f"Writing report for {doc_dir.name[:50]}"
                    )

                report_gen.generate_report(result, doc_dir)

                results.append(
                    {
                        "name": doc_dir.name,
                        "success": True,
                        "models": result.models,
                        "items_per_model": result.summary.get("items_per_model", {}),
                        "full_agreement_pct": result.summary.get("full_agreement_pct", 0),
                        "needs_review_count": result.summary.get("needs_review_count", 0),
                        "total_comparisons": result.summary.get("total_comparisons", 0),
                        "qualitative_gate": result.summary.get(
                            "qualitative_advisory_gate"
                        ),
                    }
                )

                if not view.is_quiet:
                    agreement_pct = result.summary.get("full_agreement_pct", 0)
                    needs_review = result.summary.get("needs_review_count", 0)
                    total = result.summary.get("total_comparisons", 0)

                    level = (
                        "success"
                        if agreement_pct >= 80
                        else "warning"
                        if agreement_pct >= 50
                        else "error"
                    )
                    view.status(
                        level,
                        doc_dir.name,
                        f"{agreement_pct:.1f}% agreement",
                    )
                    view.detail(
                        f"Agreement: {agreement_pct:.1f}% "
                        f"({total - needs_review}/{total} fields)"
                    )
                    view.detail(f"Needs review: {needs_review} field(s)")
                    qualitative_gate = result.summary.get("qualitative_advisory_gate") or {}
                    if qualitative_gate:
                        gate_status = str(
                            qualitative_gate.get("status", "not_applicable")
                        ).upper()
                        aligned_pct = float(qualitative_gate.get("aligned_pct", 0.0))
                        missing_pct = float(
                            qualitative_gate.get("missing_item_pct", 0.0)
                        )
                        excluded_scope_variants = int(
                            qualitative_gate.get("excluded_scope_variants", 0) or 0
                        )
                        view.detail(
                            f"Qualitative advisory gate: {gate_status} "
                            f"({aligned_pct:.1f}% aligned, {missing_pct:.1f}% missing items)"
                        )
                        if excluded_scope_variants:
                            view.detail(
                                f"Excluded scope variants: {excluded_scope_variants} "
                                "auxiliary row(s)"
                            )
                    qualitative_breakdown = (
                        result.summary.get("qualitative_mismatch_breakdown") or {}
                    )
                    missing_categories = (
                        qualitative_breakdown.get("missing_item_by_category") or []
                    )
                    scope_variant_categories = (
                        qualitative_breakdown.get("scope_variant_by_category") or []
                    )
                    text_categories = (
                        qualitative_breakdown.get("text_difference_by_category") or []
                    )
                    if missing_categories:
                        summary_text = ", ".join(
                            f"{entry.get('label')} ({entry.get('count')})"
                            for entry in missing_categories[:3]
                        )
                        view.detail(f"Top missing-item categories: {summary_text}")
                    if scope_variant_categories:
                        summary_text = ", ".join(
                            f"{entry.get('label')} ({entry.get('count')})"
                            for entry in scope_variant_categories[:3]
                        )
                        view.detail(f"Top scope-variant categories: {summary_text}")
                    if text_categories:
                        summary_text = ", ".join(
                            f"{entry.get('label')} ({entry.get('count')})"
                            for entry in text_categories[:3]
                        )
                        view.detail(f"Top text-difference categories: {summary_text}")

            except Exception as exc:
                results.append(
                    {
                        "name": doc_dir.name,
                        "success": False,
                        "error": str(exc),
                    }
                )
                view.status("error", doc_dir.name, str(exc)[:50])

            if _progress is not None:
                _progress.advance(_task)

    if not view.is_quiet:
        successful = [record for record in results if record.get("success")]
        failed = [record for record in results if not record.get("success")]

        if successful:
            avg_agreement = sum(
                record["full_agreement_pct"] for record in successful
            ) / len(successful)
            total_reviews = sum(record["needs_review_count"] for record in successful)

            summary_stats = {
                "Documents Compared": str(len(successful)),
                "Average Agreement": f"{avg_agreement:.1f}%",
                "Total Fields Needing Review": str(total_reviews),
            }

            qualitative_gates = [
                record.get("qualitative_gate")
                for record in successful
                if record.get("qualitative_gate")
            ]
            if qualitative_gates:
                gate_counts = {}
                for gate in qualitative_gates:
                    gate_status = str(gate.get("status", "not_applicable")).upper()
                    gate_counts[gate_status] = gate_counts.get(gate_status, 0) + 1
                summary_stats["Qualitative Gates"] = ", ".join(
                    f"{status}: {count}"
                    for status, count in sorted(gate_counts.items())
                )

            if failed:
                summary_stats["Failed"] = str(len(failed))

            view.summary(summary_stats, title="Comparison Summary")
            view.outputs(
                {
                    "Reports": str(qa_qc_path_obj),
                    "Files": "comparison_report.xlsx, comparison_report.csv",
                }
            )
        else:
            view.warning("No documents were successfully compared")
    else:
        successful = len([record for record in results if record.get("success")])
        console.print(f"{successful} documents compared")
