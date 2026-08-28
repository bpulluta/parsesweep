"""`compile` command."""

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
from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance,
    list_compile_record_files,
)
from psweep.config import RuntimeConfigError
from psweep.pipeline import resolve_compile_output_dir
from psweep.compilation.accounting import (
    _build_pipeline_accounting,
    _write_compilation_run_manifest,
)
from psweep.utils.config import get_config


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


def _should_fail_on_provenance(
    *,
    provenance_policy: str,
    provenance_warnings: List[str],
) -> bool:
    """Return whether provenance warnings should hard-fail compilation."""
    return provenance_policy == "fail" and bool(provenance_warnings)


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
    help="Domain run config file (RECOMMENDED — compile input, schema, and output paths)",
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
@click.option(
    "--provenance-policy",
    type=click.Choice(["warn", "fail"], case_sensitive=False),
    default="warn",
    show_default=True,
    help=(
        "How to handle compile input provenance warnings "
        "(missing schema_id, unresolved artifact_id, mixed lineage/orphan records)."
    ),
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help="Clear the compiled output directory (stale CSV/XLSX) before writing new results. Pair with 'extract --fresh' for a guaranteed clean pipeline.",
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
    provenance_policy: str,
    fresh: bool,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Compile extracted JSON files into clean Excel/CSV output.

    Reads per-document JSON files from EXTRACTED_DIR (or the configured
    input_dir), deduplicates records using the schema's identity rules, and
    writes a consolidated Excel and/or CSV file to the output directory.

    Use --fresh to clear the compiled output directory (stale CSV/XLSX) before
    writing new results. Pair with 'extract --fresh' for a guaranteed clean pipeline run.

    Run ``psweep compile --help`` for every option and its default.

    Examples
    --------
    ::

        psweep compile --config config/my_domain/run.yaml
        psweep compile extracted/my_domain/ --schema schemas/my_schema.json
        psweep compile extracted/ --schema s.json --dry-run --verbose
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
            "provenance_policy",
            "fresh",
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
    provenance_policy = str(
        resolved_inputs.get("provenance_policy", provenance_policy)
    ).strip().lower()
    synthesis_cfg = resolved_inputs.get("synthesis")
    synthesis_active = isinstance(synthesis_cfg, dict) and bool(
        synthesis_cfg.get("enabled")
    )

    input_dir = Path(extracted_dir)
    if not input_dir.exists():
        print_error("Input directory not found", input_dir.as_posix())
        sys.exit(1)

    if output:
        output_dir = Path(output)
    else:
        output_dir = resolve_compile_output_dir(input_dir)

    if fresh and not dry_run:
        import shutil

        # --fresh clears the compiled *output* directory (stale CSV/XLSX/accounting),
        # NOT the extracted JSON directory — that belongs to extract's domain.
        # Clear exactly the resolved output directory so --output is respected.
        if output_dir.exists():
            cleared_count = sum(1 for _ in output_dir.glob("*") if _.is_file())
            shutil.rmtree(output_dir)
            if cleared_count > 0 and not view.is_quiet:
                view.status(
                    "info",
                    f"--fresh: cleared {cleared_count} file(s) from {output_dir.as_posix()}",
                )

    emit_json_report = dry_run and report_format.lower() == "json"
    record_json_files = list_compile_record_files(input_dir)

    if fail_on_suspicious != "none" and not dry_run:
        print_error(
            "Invalid option combination",
            "--fail-on-suspicious only applies with --dry-run",
        )
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    input_provenance_warnings = analyze_compile_input_provenance(input_dir)
    would_fail_on_provenance = _should_fail_on_provenance(
        provenance_policy=provenance_policy,
        provenance_warnings=input_provenance_warnings,
    )

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

        config_output = resolved_inputs.get("compilation_output")
        if isinstance(config_output, dict):
            compilation_overrides["output"] = config_output

        config_norm = resolved_inputs.get("normalization")
        if isinstance(config_norm, dict):
            compilation_overrides["normalization"] = config_norm

        config_severity = resolved_inputs.get("severity_tokens")
        if isinstance(config_severity, dict):
            compilation_overrides["severity_tokens"] = config_severity

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
    synth_client = None
    synthesizer = None
    compile_started_at = datetime.now(timezone.utc)
    if would_fail_on_provenance and emit_json_report:
        click.echo(
            json.dumps(
                {
                    "warnings": input_provenance_warnings,
                    "provenance_policy": provenance_policy,
                    "would_fail_on_provenance": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        sys.exit(2)
    if would_fail_on_provenance:
        if not view.is_quiet:
            view.error(
                "Compile input provenance checks failed",
                "Provenance warnings are present and provenance_policy='fail'.",
            )
            view.warnings(input_provenance_warnings)
        sys.exit(2)

    try:
        if synthesis_active:
            from psweep.compilation.synthesizer import Synthesizer
            from psweep.extraction.llm_factory import build_llm_client

            synth_model = synthesis_cfg.get("model")
            env_model = get_config().llm_config.get("model")
            if not synth_model and not resolved_inputs.get("models") and not env_model:
                raise click.UsageError(
                    "No model configured for the compilation synthesis stage. "
                    "Add 'model: primary' under 'compilation.synthesis:' "
                    "in your run config, or set AZURE_OPENAI_MODEL in your .env file."
                )
            synth_client = build_llm_client(
                synth_model,
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
                    f"deterministically (no API) of {total_rows} synthesized row(s)",
                )
            schema_info = {
                "type": f"Synthesized: {schema_metadata.get_main_data_array()}",
                "main_array_key": (synthesis_cfg.get("group_by") or ["entity"])[0],
            }
        else:
            quiet = emit_json_report or view.is_quiet
            if not quiet:
                view.phase(
                    f"Found {len(record_json_files)} extracted JSON file(s) in {input_dir.name}"
                )
            with with_status("Loading extracted JSON files...", quiet=quiet):
                df, schema_info = compiler.compile_from_directory(
                    input_dir,
                    apply_deduplication=not dry_run,
                )

        if input_provenance_warnings and not emit_json_report:
            view.warnings(input_provenance_warnings)
        if df.empty:
            if input_provenance_warnings and emit_json_report:
                click.echo(
                    json.dumps(
                        {"warnings": input_provenance_warnings, "rows": 0},
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
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
            if input_provenance_warnings:
                preview_report["warnings"] = [
                    *(preview_report.get("warnings") or []),
                    *input_provenance_warnings,
                ]
            preview_report["fail_on_suspicious"] = fail_on_suspicious
            preview_report["would_fail_on_suspicious"] = _should_fail_on_suspicious(
                preview_report,
                fail_on_suspicious,
            )
            preview_report["provenance_policy"] = provenance_policy
            preview_report["would_fail_on_provenance"] = would_fail_on_provenance

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

            if (
                preview_report["would_fail_on_suspicious"]
                or preview_report["would_fail_on_provenance"]
            ):
                if not emit_json_report and not view.is_quiet:
                    if preview_report["would_fail_on_suspicious"]:
                        view.error(
                            "Suspicious deduplication threshold exceeded",
                            "Dry-run found suspicious groups at or above "
                            f"'{fail_on_suspicious}' severity",
                        )
                    if preview_report["would_fail_on_provenance"]:
                        view.error(
                            "Compile input provenance checks failed",
                            "Dry-run found provenance warnings while "
                            "provenance_policy='fail'",
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
                from psweep.utils.normalizers import camel_to_title

                category_display = camel_to_title(
                    schema_info.get("category_field", "")
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

        # ------------------------------------------------------------------
        # Run accounting — always written regardless of verbosity / quiet mode.
        # This is a structured record of costs, not presentation output.
        # ------------------------------------------------------------------
        _summary_stats_for_acct = {
            "records": len(df),
            "columns": len(df.columns),
            "duplicates_removed": compiler.duplicates_removed,
            "output_format": ", ".join(output_formats),
        }
        _write_compilation_run_manifest(
            output_dir=output_dir,
            schema_id=str(matched_schema),
            synthesis_active=synthesis_active,
            synthesis_model=(getattr(synth_client, "raw_model", None) if synth_client else None),
            synthesis_provider=(getattr(synth_client, "provider", None) if synth_client else None),
            synthesis_llm_calls=(int(getattr(synthesizer, "llm_calls", 0)) if synthesizer else 0),
            records=len(df),
            columns=len(df.columns),
            duplicates_removed=compiler.duplicates_removed,
            output_formats=output_formats,
            started_at=compile_started_at,
        )

        acct_path = output_dir / "run_accounting.json"
        try:
            accounting = _build_pipeline_accounting(
                domain=resolved_inputs.get("domain", input_dir.name),
                extraction_dir=input_dir,
                output_dir=output_dir,
                compilation_stats=_summary_stats_for_acct,
                discovery_input_dir=resolved_inputs.get("_extraction_input_dir"),
            )
            acct_path.write_text(
                json.dumps(accounting, indent=2) + "\n",
                encoding="utf-8",
            )
            if not get_verbosity().is_quiet:
                view.outputs({"Accounting": str(acct_path.absolute())})
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "run_accounting.json could not be written: %s", exc
            )

        if get_verbosity().is_quiet:
            for emitted_path in emitted_paths:
                console.print(str(emitted_path.absolute()))

    except Exception as exc:
        view.error("Compilation failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)
