"""`validate` command for explicit QA/QC extraction + comparison."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from psweep.cli.ui import print_error
from psweep.config import RuntimeConfigError
from psweep.extraction import load_schema
from psweep.extraction.document_utils import SUPPORTED_EXTENSIONS


@click.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    required=True,
    help="Domain run config file with extraction + qaqc sections.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=1),
    default=None,
    help="Process only first N documents",
)
@click.option(
    "--compare-only",
    is_flag=True,
    help="Skip QA/QC extraction and only regenerate comparison reports from existing qa_qc outputs.",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    show_default=True,
    help="Re-extract all files (default: skip files already processed)",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
@click.option("--debug", is_flag=True, help="Debug mode with full logs")
def validate(
    config_path: str,
    limit: Optional[int],
    compare_only: bool,
    fresh: bool,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Run explicit QA/QC validation: multi-model extraction + comparison."""
    from psweep.cli.commands import (
        _resolve_runtime_command_inputs,
        begin_run,
    )
    from psweep.cli.commands_compare import (
        generate_comparison_reports,
        load_runtime_qaqc_config,
    )
    from psweep.cli.commands_extract import (
        _apply_page_targeting,
        _generate_run_id,
        _resolve_schema_ref,
        _run_qa_qc_extraction,
    )

    view = begin_run("validate", quiet=quiet, verbose=verbose, debug=debug)
    load_dotenv()

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="validate",
            config_path=config_path,
            strict=False,
            cli_values={},
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    path = Path(resolved_inputs["path"])
    schema_path = _resolve_schema_ref(Path(resolved_inputs["schema"]))
    extraction_output_dir = Path(
        resolved_inputs.get("output")
        or (Path.cwd() / "extracted" / resolved_inputs.get("domain", path.name))
    )
    qaqc_cfg = resolved_inputs.get("qaqc") or {}
    validation_output_dir = Path(
        qaqc_cfg.get("output_dir")
        or (Path.cwd() / "validated" / resolved_inputs.get("domain", path.name))
    )
    max_context = int(resolved_inputs.get("max_context", 400000))
    timeout_seconds = resolved_inputs.get("timeout_seconds")
    qaqc_models = (resolved_inputs.get("qaqc") or {}).get("models") or []
    registry = resolved_inputs.get("_model_registry")
    qa_qc_output = validation_output_dir / "qa_qc"

    if registry is None and not compare_only:
        view.error(
            "QA/QC model registry missing",
            "Run validate with --config so qa/qc models resolve from the config models block.",
        )
        sys.exit(1)
    if len(qaqc_models) < 2 and not compare_only:
        view.error(
            "QA/QC configuration error",
            "qaqc.models must resolve to at least 2 model tiers.",
        )
        sys.exit(1)
    if not schema_path.exists():
        print_error("Schema not found", schema_path.as_posix())
        sys.exit(1)

    try:
        schema_metadata, runtime_artifact, qa_qc_config = load_runtime_qaqc_config(
            schema_path=schema_path,
            config_path=config_path,
        )
    except Exception as exc:
        view.error("Failed to load QA/QC config", str(exc))
        sys.exit(1)

    judge_runtime = qa_qc_config.get("judge_runtime") or {}
    judge_enabled = bool((qa_qc_config.get("judge") or {}).get("enabled"))
    judge_model = judge_runtime.get("model") or "(disabled)"
    judge_provider = judge_runtime.get("provider") or "-"
    report_cfg = qa_qc_config.get("report") or {}

    if compare_only:
        view.header("VALIDATE — QA/QC")
        view.config(
            {
                "Mode": "reports-only (no extraction)",
                "QA/QC Output": str(qa_qc_output),
                "Comparison Approach": qa_qc_config.get("comparison_approach")
                or "mixed",
                "Judge Enabled": "yes" if judge_enabled else "no",
                "Judge Model": judge_model,
                "Judge Provider": judge_provider,
                "Queue Includes Presence": "yes"
                if bool(report_cfg.get("include_missing_in_queue", False))
                else "no",
                "Queue Includes Low-Signal Presence": "yes"
                if bool(
                    report_cfg.get(
                        "include_low_signal_presence_in_queue", False
                    )
                )
                else "no",
            }
        )
        if not qa_qc_output.exists():
            view.error(
                "QA/QC output not found",
                f"Expected existing outputs at {qa_qc_output}",
            )
            sys.exit(1)
        
        domain = resolved_inputs.get("domain", path.name)
        discovery_checkpoint = Path.cwd() / "discovered" / domain / "curated" / "checkpoint.json"
        discovery_checkpoint_path = discovery_checkpoint if discovery_checkpoint.exists() else None
        
        # Load schema for evidence display
        try:
            schema_dict = load_schema(schema_path)
        except Exception:
            schema_dict = None
        
        exit_code = generate_comparison_reports(
            qa_qc_path_obj=qa_qc_output,
            schema_metadata=schema_metadata,
            qa_qc_config=qa_qc_config,
            runtime_artifact=runtime_artifact,
            view=view,
            report_limit=limit,
            discovery_checkpoint_path=discovery_checkpoint_path,
            extraction_base_dir=extraction_output_dir,
            schema=schema_dict,
        )
        if exit_code != 0:
            sys.exit(exit_code)
        return

    if not path.exists():
        view.error("Input path not found", str(path))
        sys.exit(1)

    loaded_schema = load_schema(schema_path)
    supported_extensions = tuple(ext.lower() for ext in SUPPORTED_EXTENSIONS)
    if path.is_dir():
        doc_files = sorted(
            f
            for f in path.rglob("*")
            if f.is_file()
            and f.suffix.lower() in supported_extensions
            and not any(part.startswith(".") for part in f.relative_to(path).parts)
        )
    else:
        if path.suffix.lower() not in supported_extensions:
            view.error("Unsupported file", str(path))
            sys.exit(1)
        doc_files = [path]

    if not doc_files:
        view.warning("No documents found to validate")
        return
    if limit and limit > 0:
        doc_files = doc_files[:limit]

    model_labels = []
    if registry is not None:
        for definition in registry.get_models(list(qaqc_models)):
            llm_kwargs = registry.to_llm_kwargs(definition.tier)
            provider = llm_kwargs.get("provider", "openai")
            model_labels.append(f"{definition.model} [{provider}]")

    view.header("VALIDATE — QA/QC")
    view.config(
        {
            "Mode": "full (extract + compare)",
            "Documents": str(len(doc_files)),
            "Models": ", ".join(model_labels)
            if model_labels
            else ", ".join(qaqc_models),
            "Comparison Approach": qa_qc_config.get("comparison_approach")
            or "mixed",
            "Judge Enabled": "yes" if judge_enabled else "no",
            "Judge Model": judge_model,
            "Judge Provider": judge_provider,
            "Extraction Mode": "reprocess all models"
            if fresh
            else "reuse existing where available",
            "LLM Timeout (s)": str(timeout_seconds) if timeout_seconds else "default",
            "Report Output": str(qa_qc_output),
        }
    )

    page_range_map = {}
    extraction_output_dir.mkdir(parents=True, exist_ok=True)
    validation_output_dir.mkdir(parents=True, exist_ok=True)
    page_targeting = resolved_inputs.get("page_targeting")
    if page_targeting and page_targeting.get("enabled"):
        view.phase("Stage 1/3: Discovering page ranges")
        _apply_page_targeting(
            doc_files=doc_files,
            page_range_map=page_range_map,
            config=page_targeting,
            models=resolved_inputs.get("models"),
            output_dir=validation_output_dir,
            pages_csv=None,
        )
    else:
        view.phase("Stage 1/3: Page targeting disabled")

    run_id = _generate_run_id(
        schema_path=schema_path,
        provider="mixed",
        model="qa_qc",
        enable_qa_qc=True,
        doc_files=doc_files,
        artifact_id=None,
    )
    view.phase("Stage 2/3: Multi-model extraction")
    qa_qc_output = _run_qa_qc_extraction(
        doc_files=doc_files,
        loaded_schema=loaded_schema,
        schema_path=schema_path,
        output_dir=validation_output_dir,
        registry=registry,
        qaqc_models=qaqc_models,
        max_context=max_context,
        timeout_seconds=timeout_seconds,
        page_range_map=page_range_map,
        verbosity=view.verbosity.value,
        runtime_artifact=None,
        run_id=run_id,
        config_path=config_path,
        prompt_confirm=not view.is_quiet,
        skip_existing=not fresh,
        seed_output_dir=extraction_output_dir,
        write_primary_canonical=False,
        view=view,
    )
    if qa_qc_output is None:
        sys.exit(1)

    view.phase("Stage 3/3: Comparison and report generation")
    domain = resolved_inputs.get("domain", path.name)
    discovery_checkpoint = Path.cwd() / "discovered" / domain / "curated" / "checkpoint.json"
    discovery_checkpoint_path = discovery_checkpoint if discovery_checkpoint.exists() else None
    
    exit_code = generate_comparison_reports(
        qa_qc_path_obj=qa_qc_output,
        schema_metadata=schema_metadata,
        qa_qc_config=qa_qc_config,
        runtime_artifact=runtime_artifact,
        view=view,
        report_limit=None,
        discovery_checkpoint_path=discovery_checkpoint_path,
        extraction_base_dir=extraction_output_dir,
        schema=loaded_schema,
    )
    if exit_code != 0:
        sys.exit(exit_code)
