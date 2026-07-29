"""`compile` command extracted from the legacy CLI monolith."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from psweep.cli.ui import (
    Verbosity,
    console,
    get_verbosity,
    print_error,
    print_info,
    print_success,
    print_warning,
    with_status,
)
from psweep.compilation.data_compiler import DataCompiler
from psweep.config import RuntimeConfigError


def _build_dedup_preview_report(
    *,
    schema_info: Dict[str, Any],
    dedup_preview: Dict[str, Any],
    rows_before_dedup: int,
) -> Dict[str, Any]:
    """Build a stable dry-run report payload for machine-readable output."""
    return {
        "schema_type": schema_info["type"],
        "main_array_key": schema_info["main_array_key"],
        "rows_before_dedup": rows_before_dedup,
        "rows_after_dedup": rows_before_dedup - dedup_preview["duplicates_removed"],
        "duplicates_removed": dedup_preview["duplicates_removed"],
        "key_fields": dedup_preview["key_fields"],
        "compare_columns": dedup_preview["compare_columns"],
        "warnings": dedup_preview.get("warnings") or [],
        "suspicious_groups_count": dedup_preview.get("suspicious_groups_count", 0),
        "suspicious_groups_by_severity": dedup_preview.get(
            "suspicious_groups_by_severity"
        )
        or {
            "high": 0,
            "medium": 0,
            "low": 0,
        },
        "suspicious_groups": dedup_preview.get("suspicious_groups") or [],
        "duplicate_groups": dedup_preview["duplicate_groups"],
    }


def _should_fail_on_suspicious(
    preview_report: Dict[str, Any],
    threshold: str,
) -> bool:
    """Return whether suspicious-group counts meet or exceed the requested threshold."""
    if threshold == "none":
        return False

    severity_counts = preview_report.get("suspicious_groups_by_severity") or {}
    if threshold == "high":
        return severity_counts.get("high", 0) > 0
    if threshold == "medium":
        return (
            severity_counts.get("high", 0) > 0
            or severity_counts.get("medium", 0) > 0
        )
    if threshold == "low":
        return preview_report.get("suspicious_groups_count", 0) > 0

    return False


def _build_pipeline_accounting(
    *,
    domain: str,
    extraction_dir: Path,
    output_dir: Path,
    compilation_stats: Dict[str, Any],
    discovery_input_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Stitch together discovery + extraction + compilation into a unified accounting."""
    accounting: Dict[str, Any] = {
        "domain": domain,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stages": {},
        "totals": {"cost_usd": 0.0, "elapsed_seconds": 0.0, "llm_calls": 0, "tokens": 0},
    }

    if discovery_input_dir:
        disc_path = Path(discovery_input_dir)
        manifest_candidate: Optional[Path] = None

        for parent in [disc_path, disc_path.parent, disc_path.parent.parent]:
            candidate = parent / "manifest.json"
            if candidate.exists():
                manifest_candidate = candidate
                break

        if manifest_candidate is None:
            runs_dir = disc_path.parent / "runs"
            if runs_dir.is_dir():
                run_dirs = sorted(
                    [directory for directory in runs_dir.iterdir() if directory.is_dir()],
                    reverse=True,
                )
                for run_dir in run_dirs:
                    candidate = run_dir / "manifest.json"
                    if candidate.exists():
                        manifest_candidate = candidate
                        break

        if manifest_candidate is not None:
            try:
                disc = json.loads(manifest_candidate.read_text(encoding="utf-8"))
                timing = disc.get("timing", {})
                stage_summaries = disc.get("stage_summaries", {})
                seeker = stage_summaries.get("seeker", {})
                review = stage_summaries.get("document_review", {})
                review_costs = review.get("costs", {})
                downloads = stage_summaries.get("downloads", {})

                disc_cost = review_costs.get("total_cost_usd", 0.0)
                disc_elapsed = timing.get("elapsed_seconds", 0.0)
                disc_tokens = review_costs.get("total_input_tokens", 0) + review_costs.get(
                    "total_output_tokens", 0
                )
                disc_calls = review_costs.get("model_calls", 0)

                accounting["stages"]["discovery"] = {
                    "status": "completed",
                    "manifest_path": str(manifest_candidate),
                    "targets_total": seeker.get("input_targets"),
                    "targets_successful": seeker.get("successful_targets"),
                    "targets_failed": seeker.get("failed_targets"),
                    "docs_downloaded": downloads.get("downloaded_files"),
                    "docs_selected": review.get("selected_count"),
                    "docs_rejected": review.get("rejected_count"),
                    "cost_usd": disc_cost,
                    "elapsed_seconds": disc_elapsed,
                    "llm_calls": disc_calls,
                    "tokens": disc_tokens,
                }
                accounting["totals"]["cost_usd"] += float(disc_cost or 0.0)
                accounting["totals"]["elapsed_seconds"] += float(disc_elapsed or 0.0)
                accounting["totals"]["llm_calls"] += int(disc_calls or 0)
                accounting["totals"]["tokens"] += int(disc_tokens or 0)
            except Exception:
                pass

    extraction_records = list(extraction_dir.glob("*.json"))
    if extraction_records:
        doc_total = 0
        doc_success = 0
        ext_cost = 0.0
        ext_elapsed = 0.0
        ext_tokens = 0
        ext_calls = 0
        provider = model = None
        run_id = schema_id = artifact_id = None
        failed_examples: List[Dict[str, Any]] = []
        error_totals: Dict[str, int] = {}
        merged_fields_union: set[str] = set()
        input_chars_total = 0
        files_with_input_chars = 0

        for record_path in extraction_records:
            try:
                rec = json.loads(record_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue

            metrics = rec.get("processing_metrics") or {}
            lineage = rec.get("lineage") or {}
            quality = rec.get("quality") or {}
            document = rec.get("document") or {}

            if provider is None:
                provider = lineage.get("provider")
            if model is None:
                model = lineage.get("model")
            if run_id is None:
                run_id = lineage.get("run_id")
            if schema_id is None:
                schema_id = lineage.get("schema_id")
            if artifact_id is None:
                artifact_id = lineage.get("artifact_id")

            input_chars = metrics.get("input_chars")
            if isinstance(input_chars, int):
                input_chars_total += input_chars
                files_with_input_chars += 1

            doc_total += 1
            ext_cost += float(metrics.get("cost_usd", 0.0) or 0.0)
            ext_elapsed += float(metrics.get("duration_seconds", 0.0) or 0.0)
            ext_tokens += int(metrics.get("input_tokens") or 0) + int(
                metrics.get("output_tokens") or 0
            )
            ext_calls += 1

            q_errors = quality.get("errors") or []
            if q_errors:
                for err in q_errors:
                    code = (
                        err.get("code")
                        if isinstance(err, dict)
                        else None
                    ) or "unspecified_error"
                    error_totals[code] = error_totals.get(code, 0) + 1
                if len(failed_examples) < 10:
                    failed_examples.append(
                        {
                            "record": str(record_path),
                            "source_document_id": document.get("source_document_id"),
                            "error_codes": list(
                                {
                                    (
                                        e.get("code")
                                        if isinstance(e, dict)
                                        else "unspecified_error"
                                    )
                                    for e in q_errors
                                }
                            ),
                        }
                    )
            else:
                doc_success += 1

            notes = rec.get("notes")
            if isinstance(notes, str):
                marker = "Merged duplicate from source row(s) with differing values in: "
                if marker in notes:
                    merged_part = notes.split(marker, 1)[1].split(";", 1)[0]
                    for field_name in [
                        field.strip() for field in merged_part.split(",") if field.strip()
                    ]:
                        merged_fields_union.add(field_name)

        stage_payload: Dict[str, Any] = {
            "status": "completed",
            "records_dir": str(extraction_dir),
            "documents_total": doc_total,
            "documents_successful": doc_success,
            "documents_failed": max(doc_total - doc_success, 0),
            "provider": provider,
            "model": model,
            "run_id": run_id,
            "schema_id": schema_id,
            "artifact_id": artifact_id,
            "cost_usd": ext_cost,
            "elapsed_seconds": ext_elapsed,
            "llm_calls": ext_calls,
            "tokens": ext_tokens,
        }
        if error_totals:
            stage_payload["error_summary"] = error_totals
        if failed_examples:
            stage_payload["failed_examples"] = failed_examples
        if merged_fields_union:
            stage_payload["merged_conflict_fields"] = sorted(merged_fields_union)
        if files_with_input_chars:
            stage_payload["avg_input_chars"] = input_chars_total / files_with_input_chars

        accounting["stages"]["extraction"] = stage_payload
        accounting["totals"]["cost_usd"] += ext_cost
        accounting["totals"]["elapsed_seconds"] += ext_elapsed
        accounting["totals"]["llm_calls"] += ext_calls
        accounting["totals"]["tokens"] += ext_tokens

    accounting["stages"]["compilation"] = {
        "status": "completed",
        "output_dir": str(output_dir),
        **compilation_stats,
    }
    return accounting


def _resolve_compilation_output_formats(
    metadata_overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Resolve which compilation outputs to emit."""
    output_config = ((metadata_overrides or {}).get("compilation") or {}).get("output") or {}
    requested_format = output_config.get("default_format")

    if requested_format is None:
        return ["csv", "excel"]

    normalized_format = str(requested_format).strip().lower()
    if normalized_format == "excel":
        return ["excel"]
    if normalized_format == "csv":
        return ["csv"]
    if normalized_format in {"both", "all"}:
        return ["csv", "excel"]

    logging.getLogger(__name__).warning(
        "Unsupported compilation output format '%s'; falling back to csv+excel",
        requested_format,
    )
    return ["csv", "excel"]


@click.command()
@click.argument("extracted_dir", type=click.Path(exists=True), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain config file (RECOMMENDED — includes schema, page targeting, dedup)",
)
@click.option(
    "--show-effective-config",
    is_flag=True,
    help="Print resolved command inputs with source attribution and continue",
)
@click.option(
    "--validate-config",
    "validate_config_only",
    is_flag=True,
    help="Validate resolved command inputs and exit without compiling",
)
@click.option(
    "--config-strict",
    is_flag=True,
    help="Fail on unknown keys in runtime config sections",
)
@click.option(
    "--schema",
    "-s",
    type=click.Path(exists=True),
    required=False,
    help="Schema file (for quick testing without a config YAML)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output directory (auto-detected if not specified)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview compilation and deduplication without writing output files",
)
@click.option(
    "--report-format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Dry-run preview output format",
)
@click.option(
    "--fail-on-suspicious",
    type=click.Choice(["none", "high", "medium", "low"], case_sensitive=False),
    default="none",
    show_default=True,
    help="With --dry-run, exit non-zero when suspicious duplicate groups meet this severity threshold",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output with statistics")
@click.option("--debug", is_flag=True, help="Debug mode with full logs")
def compile(
    extracted_dir: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    schema: Optional[str],
    output: Optional[str],
    dry_run: bool,
    report_format: str,
    fail_on_suspicious: str,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """
    Compile extracted JSON files into clean Excel/CSV output.

    Works with ANY schema type - automatically detects structure and creates
    clean, readable output with intelligent deduplication.
    """
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("compile", quiet=quiet, verbose=verbose, debug=debug)

    cli_overrides = _explicit_cli_overrides(
        [
            "extracted_dir",
            "schema",
            "output",
            "dry_run",
            "report_format",
            "fail_on_suspicious",
        ]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="compile",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    view.warnings(warnings)

    if show_effective_config and not get_verbosity().is_quiet:
        _print_effective_config("compile", resolved_inputs)
        console.print()

    if validate_config_only:
        if get_verbosity().is_quiet:
            click.echo(
                json.dumps(
                    {
                        "command": "compile",
                        "status": "valid",
                        "resolved": {
                            key: value
                            for key, value in resolved_inputs.items()
                            if not key.startswith("_")
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print_success("Runtime config validation passed for compile command")
        return

    extracted_dir = resolved_inputs["extracted_dir"]
    schema = resolved_inputs["schema"]
    output = resolved_inputs.get("output", output)
    dry_run = resolved_inputs.get("dry_run", dry_run)
    report_format = resolved_inputs.get("report_format", report_format)
    fail_on_suspicious = resolved_inputs.get(
        "fail_on_suspicious", fail_on_suspicious
    )
    synthesis_cfg = resolved_inputs.get("synthesis")
    synthesis_active = isinstance(synthesis_cfg, dict) and bool(
        synthesis_cfg.get("enabled")
    )

    input_dir = Path(extracted_dir)
    if not input_dir.exists():
        print_error("Input directory not found", input_dir.as_posix())
        sys.exit(1)

    emit_json_report = dry_run and report_format.lower() == "json"

    if fail_on_suspicious != "none" and not dry_run:
        print_error(
            "Invalid option combination",
            "--fail-on-suspicious only applies with --dry-run",
        )
        sys.exit(1)

    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if "extracted" in parts:
            index = parts.index("extracted")
            parts[index] = "compiled"
            output_dir = Path(*parts)
        else:
            output_dir = Path.cwd() / "compiled" / input_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)

    if not emit_json_report:
        view.header("COMPILATION")

    matched_schema = Path(schema)
    if not matched_schema.exists():
        print_error("Schema not found", matched_schema.as_posix())
        sys.exit(1)

    try:
        from psweep.utils.schema_metadata import SchemaMetadata

        metadata_overrides: Dict[str, Any] = {}
        compilation_overrides: Dict[str, Any] = {}

        config_dedup = resolved_inputs.get("deduplication")
        if isinstance(config_dedup, dict):
            compilation_overrides["deduplication"] = config_dedup

        config_output = resolved_inputs.get("compilation_output")
        if isinstance(config_output, dict):
            compilation_overrides["output"] = config_output

        config_norm = resolved_inputs.get("normalization")
        if isinstance(config_norm, dict):
            compilation_overrides["normalization"] = config_norm

        if compilation_overrides:
            metadata_overrides["compilation"] = compilation_overrides

        schema_metadata = SchemaMetadata(
            matched_schema,
            metadata_overrides=metadata_overrides or None,
        )

        if not emit_json_report and not view.is_quiet:
            try:
                schema_display = str(matched_schema.relative_to(Path.cwd()))
            except ValueError:
                schema_display = str(matched_schema)
            config_info = {
                "Input": str(input_dir),
                "Output": str(output_dir),
                "Schema": schema_display,
            }
            if metadata_overrides:
                config_info["Overrides"] = "config-owned compilation settings active"
            view.config(config_info)
    except Exception as exc:
        print_error(
            "Schema validation failed",
            f"Schema {matched_schema.name} is missing required $metadata section: {exc}\n"
            "ParseSweep v2.0+ requires schemas with $metadata.\n"
            "See schemas/SCHEMA_BEST_PRACTICES.md for examples.",
        )
        return

    if not emit_json_report:
        view.phase("Analyzing schema structure")
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=view.verbosity.shows_detail,
        debug=view.verbosity is Verbosity.DEBUG,
    )

    try:
        if synthesis_active:
            from psweep.compilation.synthesizer import Synthesizer
            from psweep.extraction.llm_factory import build_llm_client

            synth_client = build_llm_client(
                synthesis_cfg.get("model"),
                models=resolved_inputs.get("models"),
            )
            if not emit_json_report:
                view.phase(
                    "Synthesizing one record per entity "
                    f"(model={synth_client.raw_model})"
                )
            synthesizer = Synthesizer(
                schema_metadata=schema_metadata,
                config=synthesis_cfg,
                llm_client=synth_client,
                verbose=view.verbosity.shows_detail,
            )
            quiet = emit_json_report or view.is_quiet
            with with_status(
                f"Synthesizing with {synth_client.raw_model}...", quiet=quiet
            ):
                df = synthesizer.synthesize_from_directory(input_dir)
            if not emit_json_report:
                total_rows = synthesizer.llm_calls + synthesizer.deterministic_rows
                view.status(
                    "info",
                    f"Synthesis: {synthesizer.llm_calls} LLM reconciliation "
                    f"call(s), {synthesizer.deterministic_rows} resolved "
                    f"deterministically (no API) of {total_rows} entities",
                )
            schema_info = {
                "type": f"Synthesized: {schema_metadata.get_main_data_array()}",
                "main_array_key": (synthesis_cfg.get("group_by") or ["entity"])[0],
            }
        else:
            quiet = emit_json_report or view.is_quiet
            if not quiet:
                _json_files = list(input_dir.glob("*.json"))
                view.phase(
                    f"Found {len(_json_files)} extracted JSON file(s) in {input_dir.name}"
                )
            with with_status("Loading extracted JSON files...", quiet=quiet):
                df, schema_info = compiler.compile_from_directory(
                    input_dir,
                    apply_deduplication=not dry_run,
                )

        if df.empty:
            print_warning("No data found to compile")
            return

        if not emit_json_report:
            view.success(f"Schema detected: {schema_info['type']}")
            view.status("info", f"Main entity: {schema_info['main_array_key']}")

        if dry_run and synthesis_active:
            print_info(
                f"Dry run: synthesized {len(df)} entity row(s); no files written."
            )
            return

        if dry_run:
            dedup_preview = compiler.deduplicator.preview_deduplication(df)
            preview_report = _build_dedup_preview_report(
                schema_info=schema_info,
                dedup_preview=dedup_preview,
                rows_before_dedup=len(df),
            )
            preview_report["fail_on_suspicious"] = fail_on_suspicious
            preview_report["would_fail_on_suspicious"] = _should_fail_on_suspicious(
                preview_report,
                fail_on_suspicious,
            )

            if emit_json_report or view.is_quiet:
                click.echo(json.dumps(preview_report, indent=2, sort_keys=True))
            else:
                view.phase("Previewing deduplication")
                preview_stats = {
                    "Schema Type": preview_report["schema_type"],
                    "Rows Before Dedup": str(preview_report["rows_before_dedup"]),
                    "Rows After Dedup": str(preview_report["rows_after_dedup"]),
                    "Duplicates Removed": str(preview_report["duplicates_removed"]),
                    "Suspicious Groups": str(preview_report["suspicious_groups_count"]),
                    "Severity Mix": ", ".join(
                        f"{severity}={count}"
                        for severity, count in preview_report[
                            "suspicious_groups_by_severity"
                        ].items()
                        if count > 0
                    )
                    or "none",
                    "Fail Threshold": preview_report["fail_on_suspicious"],
                    "Key Fields": ", ".join(preview_report["key_fields"]) or "none",
                    "Compare Columns": ", ".join(preview_report["compare_columns"])
                    or "none",
                }
                view.summary(preview_stats, title="Deduplication Preview")

                for warning in preview_report["warnings"]:
                    view.warning(warning)

                for index, group in enumerate(
                    preview_report["duplicate_groups"][:5],
                    start=1,
                ):
                    sample_values = (
                        ", ".join(
                            f"{field}={value}"
                            for field, value in group["sample_values"].items()
                            if value not in (None, "")
                        )
                        or "no populated key values"
                    )
                    group_label = f"Group {index}"
                    if group.get("suspicious"):
                        group_label += f" (suspicious:{group.get('severity', 'low')})"
                    view.status(
                        "warning" if group.get("suspicious") else "info",
                        f"{group_label}: keep row {group['keep_index']}, "
                        f"drop {group['drop_indices']}",
                        sample_values,
                    )
                    view.status("info", group["note"])
                    if group.get("conflicting_columns"):
                        view.status(
                            "warning",
                            f"Conflicts: {', '.join(group['conflicting_columns'])}",
                        )

                if len(preview_report["duplicate_groups"]) > 5:
                    view.status(
                        "info",
                        f"... {len(preview_report['duplicate_groups']) - 5} "
                        "more duplicate group(s) omitted",
                    )

                view.info("Dry run complete - no CSV/Excel files were written")

            if preview_report["would_fail_on_suspicious"]:
                if not emit_json_report and not view.is_quiet:
                    view.error(
                        "Suspicious deduplication threshold exceeded",
                        "Dry-run found suspicious groups at or above "
                        f"'{fail_on_suspicious}' severity",
                    )
                sys.exit(2)
            return

        view.phase("Creating outputs")
        base_name = input_dir.name.replace("_", "-")
        output_formats = _resolve_compilation_output_formats(metadata_overrides or None)
        emitted_paths: List[Path] = []

        if "csv" in output_formats:
            csv_path = output_dir / f"{base_name}.csv"
            compiler.save_csv(df, csv_path)
            emitted_paths.append(csv_path)
            csv_size_mb = csv_path.stat().st_size / (1024 * 1024)

            if view.verbosity.shows_detail:
                view.success(
                    f"CSV saved: {csv_path.name} ({csv_size_mb:.2f} MB, {len(df)} rows)"
                )
            else:
                view.success(f"CSV saved ({len(df)} rows)")

        if "excel" in output_formats:
            excel_path = output_dir / f"{base_name}.xlsx"
            compiler.save_excel(df, excel_path)
            emitted_paths.append(excel_path)
            excel_size_mb = excel_path.stat().st_size / (1024 * 1024)

            if view.verbosity.shows_detail:
                view.success(
                    f"Excel saved: {excel_path.name} ({excel_size_mb:.2f} MB, {len(df)} rows)"
                )
            else:
                view.success("Excel saved (clean formatting, auto-sized columns)")

        if not get_verbosity().is_quiet:
            summary_stats = {
                "Schema Type": schema_info["type"],
                "Records": str(len(df)),
                "Columns": str(len(df.columns)),
                "Outputs": ", ".join(output_formats),
            }
            if not synthesis_active and compiler.duplicates_removed > 0:
                summary_stats["Duplicates Removed"] = str(compiler.duplicates_removed)
            if synthesis_active:
                summary_stats["Model"] = synth_client.raw_model

            if schema_info.get("category_field"):
                category_display = (
                    "".join(
                        [
                            " " + char if char.isupper() else char
                            for char in schema_info.get("category_field", "")
                        ]
                    )
                    .strip()
                    .title()
                )
                if category_display and category_display in df.columns:
                    top_categories = df[category_display].value_counts().head(5)
                    if not top_categories.empty:
                        top_cat_str = ", ".join(
                            [
                                f"{cat} ({count})"
                                for cat, count in list(top_categories.items())[:3]
                            ]
                        )
                        summary_stats[f"Top {category_display}s"] = top_cat_str

            view.summary(summary_stats, title="Compilation Summary")
            view.outputs(
                {
                    (path.suffix.lstrip(".").upper() or "File"): str(path.absolute())
                    for path in emitted_paths
                }
            )
            view.next_steps(["Open the CSV/Excel to review the compiled dataset"])

            try:
                accounting = _build_pipeline_accounting(
                    domain=resolved_inputs.get("domain", input_dir.name),
                    extraction_dir=input_dir,
                    output_dir=output_dir,
                    compilation_stats={
                        "records": len(df),
                        "columns": len(df.columns),
                        "duplicates_removed": compiler.duplicates_removed,
                        "output_format": summary_stats.get("Outputs", ""),
                    },
                    discovery_input_dir=resolved_inputs.get("_extraction_input_dir"),
                )
                acct_path = output_dir / "run_accounting.json"
                acct_path.write_text(
                    json.dumps(accounting, indent=2) + "\n",
                    encoding="utf-8",
                )
                view.outputs({"Accounting": str(acct_path.absolute())})
            except Exception:
                pass
        else:
            for emitted_path in emitted_paths:
                console.print(str(emitted_path.absolute()))

    except Exception as exc:
        view.error("Compilation failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)
