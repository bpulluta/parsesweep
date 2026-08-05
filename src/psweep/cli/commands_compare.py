"""Shared QA/QC comparison report generation helpers used by validate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import contextlib

from psweep.cli.ui import console, create_extraction_progress, print_error


def load_runtime_qaqc_config(
    *,
    schema_path: Path,
    config_path: Optional[str],
):
    """Load schema/runtime QA/QC config for comparison."""
    from psweep.config import load_runtime_config_file
    from psweep.extraction.llm_factory import resolve_llm_kwargs
    from psweep.qa_qc.utils import resolve_qaqc_runtime_config
    from psweep.utils.config import get_config
    from psweep.utils.schema_metadata import SchemaMetadata

    schema_metadata = SchemaMetadata(schema_path)
    runtime_artifact = None
    runtime_qaqc = None
    config_data = {}
    if config_path:
        config_data = load_runtime_config_file(Path(config_path))
        config_qaqc = config_data.get("qaqc")
        if isinstance(config_qaqc, dict):
            runtime_qaqc = config_qaqc
    qa_qc_config = resolve_qaqc_runtime_config(
        schema_metadata,
        runtime_artifact=runtime_artifact,
        runtime_qaqc=runtime_qaqc,
    )
    judge_cfg = qa_qc_config.get("judge") or {}
    if judge_cfg.get("enabled") and judge_cfg.get("model"):
        llm_kwargs = resolve_llm_kwargs(
            judge_cfg.get("model"),
            models=config_data.get("models"),
            llm_config=get_config().llm_config,
        )
        judge_timeout = judge_cfg.get("timeout_seconds")
        if judge_timeout is not None:
            if isinstance(judge_timeout, bool) or not isinstance(
                judge_timeout, int
            ) or judge_timeout <= 0:
                raise ValueError(
                    "qaqc.judge.timeout_seconds must be a positive integer"
                )
        qa_qc_config["judge_runtime"] = {
            "model": llm_kwargs.get("model"),
            "provider": llm_kwargs.get("provider"),
            "api_key": llm_kwargs.get("api_key"),
            "base_url": llm_kwargs.get("base_url"),
            "azure_endpoint": llm_kwargs.get("azure_endpoint"),
            "azure_api_version": llm_kwargs.get("azure_api_version"),
            "timeout": judge_timeout,
        }
    return schema_metadata, runtime_artifact, qa_qc_config


def generate_comparison_reports(
    *,
    qa_qc_path_obj: Path,
    schema_metadata,
    qa_qc_config: dict,
    runtime_artifact,
    view,
    report_limit: int | None = None,
    discovery_checkpoint_path: Optional[Path] = None,
    extraction_base_dir: Optional[Path] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> int:
    """Generate comparison reports from previously extracted QA/QC JSON sidecars."""
    from psweep.qa_qc import ComparisonEngine, ReportGenerator

    engine = ComparisonEngine(schema_metadata, qa_qc_config=qa_qc_config)
    report_gen = ReportGenerator(schema=schema)
    report_cfg = qa_qc_config.get("report") or {}
    include_csv = bool(report_cfg.get("include_csv", False))
    include_missing_in_queue = bool(
        report_cfg.get("include_missing_in_queue", True)
    )
    include_low_signal_presence_in_queue = bool(
        report_cfg.get("include_low_signal_presence_in_queue", False)
    )

    doc_dirs = []
    json_files_in_path = list(qa_qc_path_obj.glob("*.json"))
    ignored_qaqc_json_files = {"metadata.json", "comparison_summary.json", "judge_cache.json"}

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
                "Run the validate stage first",
                "Check the path is correct",
            ],
        )
        return 1

    results = []
    view.phase("Loading QA/QC extractions")

    _progress = view.make_progress()
    _progress_ctx = _progress if _progress is not None else contextlib.nullcontext()
    _task = None
    _writing_phase_announced = False

    if report_limit and report_limit > 0:
        doc_dirs = doc_dirs[:report_limit]

    judge_runtime = qa_qc_config.get("judge_runtime") or {}
    judge_enabled = bool((qa_qc_config.get("judge") or {}).get("enabled"))
    judge_model = judge_runtime.get("model") if judge_enabled else None

    with _progress_ctx:
        if _progress is not None:
            _task = _progress.add_task("Comparing", total=len(doc_dirs), phase="")

        for doc_dir in doc_dirs:
            if _progress is not None:
                _progress.update(
                    _task,
                    description=f"Comparing {doc_dir.name[:50]}",
                    phase=(
                        f"→ compare + judge ({judge_model})"
                        if judge_model
                        else "→ compare"
                    ),
                )

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
                    _progress.update(_task, phase="", advance=1)
                continue

            try:
                engine.set_judge_cache_path(doc_dir / "judge_cache.json")
                result = engine.compare_outputs(model_files, doc_dir.name)
                run_metadata = None
                metadata_path = doc_dir / "metadata.json"
                if metadata_path.exists():
                    try:
                        run_metadata = json.loads(
                            metadata_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        run_metadata = None

                if not _writing_phase_announced and not view.is_quiet:
                    view.phase("Generating comparison reports")
                    _writing_phase_announced = True

                if _progress is not None:
                    _progress.update(
                        _task,
                        description=f"Writing report for {doc_dir.name[:50]}",
                        phase="→ report",
                    )

                report_gen.generate_report(
                    result,
                    doc_dir,
                    run_metadata=run_metadata,
                    include_csv=include_csv,
                    include_missing_in_queue=include_missing_in_queue,
                    include_low_signal_presence_in_queue=include_low_signal_presence_in_queue,
                    discovery_checkpoint_path=discovery_checkpoint_path,
                    extraction_dir=extraction_base_dir / doc_dir.name if extraction_base_dir else None,
                )

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
                        "judge": result.summary.get("judge") or {},
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
                    judge = result.summary.get("judge") or {}
                    if judge.get("enabled"):
                        view.detail(
                            "Judge: "
                            f"{judge.get('resolved_matches', 0)} matches, "
                            f"{judge.get('attempted_calls', 0)} calls, "
                            f"${float(judge.get('total_cost_usd', 0.0)):.4f}"
                        )

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
                _progress.update(_task, phase="", advance=1)

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
            judge_runs = [r.get("judge") or {} for r in successful if r.get("judge")]
            if judge_runs:
                summary_stats["Judge Calls"] = str(
                    sum(int(j.get("attempted_calls", 0)) for j in judge_runs)
                )
                summary_stats["Judge Cost"] = (
                    f"${sum(float(j.get('total_cost_usd', 0.0)) for j in judge_runs):.4f}"
                )

            view.summary(summary_stats, title="Comparison Summary")
            view.outputs(
                {
                    "Reports": str(qa_qc_path_obj),
                    "Files": "comparison_report.xlsx, comparison_summary.json"
                    + (", comparison_report.csv" if include_csv else ""),
                }
            )
        else:
            view.warning("No documents were successfully compared")
            return 1
    else:
        successful = len([record for record in results if record.get("success")])
        console.print(f"{successful} documents compared")
        if successful == 0:
            return 1

    return 0
