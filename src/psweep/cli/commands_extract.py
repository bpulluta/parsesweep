"""`extract` command and helpers."""

from __future__ import annotations

import csv
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from dotenv import load_dotenv

from psweep.cli.run_view import RunView
from psweep.cli.dashboard import create_live_dashboard
from psweep.cli.ui import (
    Verbosity,
    ask_confirm,
    get_verbosity,
    print_error,
    print_info,
    print_warning,
)
from psweep.config import RuntimeConfigError
from psweep.extraction.document_utils import (
    extract_text_from_document,
)
from psweep.exceptions import ParseSweepError
from psweep.extraction.llm_factory import DEFAULT_MAX_CONTEXT
from psweep.extraction.record_writer import write_extraction_record
from psweep.pipeline import _execute_extraction, _prepare_extraction
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.validation.utils import sanitize_model_name


def _format_extraction_accounting(
    num_docs: int,
    total_cost_usd: float,
    total_llm_calls: int,
    total_time_sec: float,
    model: str | None,
) -> str:
    """Format concise extraction accounting summary for terminal display.

    Parameters
    ----------
    num_docs : int
        Number of documents successfully extracted
    total_cost_usd : float
        Total LLM cost in USD
    total_llm_calls : int
        Total number of LLM calls
    total_time_sec : float
        Total elapsed time in seconds
    model : str
        Model name/identifier

    Returns
    -------
    str
        Single-line accounting summary
    """
    return f"✓ Extraction: {num_docs} docs, {total_llm_calls} calls, ${total_cost_usd:.3f}, {total_time_sec:.1f}s [{model}]"


def _context_budget_suggestions_for_process(
    *,
    error_record: Dict[str, Any],
    schema_path: Path,
    document_path: Path,
    repo_root: Optional[Path] = None,
) -> Optional[List[str]]:
    """Return actionable CLI suggestions for context-budget failures."""
    if error_record.get("code") != "context_window_exceeded":
        return None

    resolved_repo_root = repo_root or Path(__file__).resolve().parents[3]
    suggestions = [
        "Rerun with a smaller input scope using --pages START-END for a single PDF or --pages-csv for a batch.",
        "For large documents, start with the most relevant 25-100 pages instead of a whole-document run and reduce --max-context further if needed.",
    ]

    try:
        relative_document_path = document_path.resolve().relative_to(
            resolved_repo_root.resolve()
        )
    except ValueError:
        relative_document_path = document_path

    path_parts = relative_document_path.parts
    if len(path_parts) >= 3 and path_parts[0] == "documents":
        config_path = (
            resolved_repo_root / "config" / path_parts[1] / "page_ranges.csv"
        )
        if config_path.exists():
            try:
                display_path = (
                    config_path.resolve()
                    .relative_to(resolved_repo_root.resolve())
                    .as_posix()
                )
            except ValueError:
                display_path = config_path.as_posix()
            suggestions.insert(
                1,
                f"If this document set already has a repo page-range config, rerun with --pages-csv {display_path}.",
            )

    return suggestions


def _apply_page_targeting(
    *,
    doc_files: list,
    page_range_map: dict,
    config: dict,
    models: dict | None = None,
    extraction_model: str | None = None,
    pages_csv: str | None = None,
    output_dir: Path | None = None,
) -> None:
    """Fill page_range_map for large PDFs via LLM-assisted page targeting."""
    from psweep.extraction.page_locator import (
        DEFAULT_MAX_SELECTED_PAGES,
        DEFAULT_PAGE_TRIGGER_CHARS,
        PageLocator,
    )
    from psweep.extraction.pdf_utils import extract_pages_text

    description = str(config.get("section_description") or "").strip()
    if not description:
        print_warning(
            "pages.auto_locate is set but section_description is missing; "
            "skipping page targeting."
        )
        return

    trigger_chars = int(
        config.get("trigger_chars", DEFAULT_PAGE_TRIGGER_CHARS)
        or DEFAULT_PAGE_TRIGGER_CHARS
    )
    locator = PageLocator(
        description,
        model=config.get("model"),
        models=models,
        default_model=extraction_model,
        trigger_chars=trigger_chars,
        max_selected_pages=int(
            config.get("max_selected_pages", DEFAULT_MAX_SELECTED_PAGES)
            or DEFAULT_MAX_SELECTED_PAGES
        ),
        keywords=config.get("keywords"),
    )

    discovered: list[tuple[str, int, int]] = []
    non_pdf_skipped = 0

    for doc in doc_files:
        if doc.suffix.lower() != ".pdf":
            non_pdf_skipped += 1
            continue
        if doc in page_range_map:
            continue
        pages = extract_pages_text(doc)
        if not pages:
            continue
        if sum(len(p) for p in pages) <= trigger_chars:
            continue
        rng = locator.locate(doc, pages=pages)
        if rng:
            page_range_map[doc] = rng
            discovered.append((doc.name, rng[0], rng[1]))
            if not get_verbosity().is_quiet:
                print_info(
                    f"Page targeting: {doc.name} → pages {rng[0]}-{rng[1]}"
                )

    if non_pdf_skipped and not get_verbosity().is_quiet:
        print_info(
            f"Page targeting skipped {non_pdf_skipped} non-PDF file(s): not a PDF"
        )

    if discovered and config.get("save_discovered", False):
        if pages_csv:
            dest = Path(pages_csv).parent / "discovered_page_ranges.csv"
        elif output_dir:
            dest = output_dir / "discovered_page_ranges.csv"
        else:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)

        with open(dest, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["file_path", "start_page", "end_page"])
            for name, start, end in discovered:
                writer.writerow([name, start, end])
        if not get_verbosity().is_quiet:
            print_info(
                f"Discovered page ranges written to {dest} — review and "
                "promote to pages.csv if correct."
            )


def _apply_section_targeting(
    *,
    doc_files: list,
    section_text_map: dict,
    config: dict,
    models: dict | None = None,
    extraction_model: str | None = None,
) -> None:
    """Fill section_text_map for large non-PDF files via LLM-assisted section targeting."""
    from psweep.extraction.section_locator import SectionLocator
    from psweep.extraction.page_locator import DEFAULT_PAGE_TRIGGER_CHARS

    description = str(config.get("section_description") or "").strip()
    if not description:
        return

    trigger_chars = int(
        config.get("trigger_chars", DEFAULT_PAGE_TRIGGER_CHARS)
        or DEFAULT_PAGE_TRIGGER_CHARS
    )
    locator = SectionLocator(
        description,
        model=config.get("model"),
        models=models,
        default_model=extraction_model,
        trigger_chars=trigger_chars,
        keywords=config.get("keywords"),
    )

    _non_pdf_types = {".html", ".htm", ".docx", ".doc", ".txt"}
    targeted_count = 0

    for doc in doc_files:
        if doc.suffix.lower() not in _non_pdf_types:
            continue
        if doc in section_text_map:
            continue
        filtered = locator.locate(doc)
        if filtered is not None:
            section_text_map[doc] = filtered
            targeted_count += 1
            if not get_verbosity().is_quiet:
                print_info(
                    f"Section targeting: {doc.name} → {len(filtered):,} chars"
                )
            if config.get("save_discovered"):
                sidecar = doc.parent / f"{doc.stem}.section_target.txt"
                try:
                    sidecar.write_text(filtered, encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass

    if targeted_count and not get_verbosity().is_quiet:
        print_info(
            f"Section targeting: {targeted_count} non-PDF file(s) targeted"
        )


def _document_progress_desc(doc_path: Path, page_range_map: dict) -> str:
    """Build a progress-bar label for a document, with an optional page suffix."""
    desc = doc_path.name
    page_range = page_range_map.get(doc_path)
    if page_range is not None:
        start, end = page_range
        desc += f" [dim](pages {start}-{end})[/dim]"
    return desc


def _render_extraction(plan, view, live_dashboard):
    """Keep progress display outside shared extraction execution."""
    dashboard = None
    live = None
    if live_dashboard and not view.is_quiet:
        live, dashboard = create_live_dashboard(
            len(plan["pending"]),
            plan["llm"]["model"],
        )

    def progress(doc_path, result):
        if dashboard is not None:
            if result is None:
                dashboard.start_document(doc_path.name)
            else:
                dashboard.complete_document(
                    doc_path.name,
                    success=result["success"],
                    cost=result.get("cost") or 0,
                    input_tokens=result.get("input_tokens") or 0,
                    output_tokens=result.get("output_tokens") or 0,
                )
            return
        if view.is_quiet or result is None:
            return
        if result["success"]:
            view.success(f"{doc_path.name}: {result['items']} items")
        else:
            view.error(
                f"Extraction failed for {doc_path.name}", result["error"]
            )

    with live if live is not None else contextlib.nullcontext():
        return _execute_extraction(plan, on_progress=progress)


@click.command()
@click.argument("path", type=click.Path(), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain run config file (RECOMMENDED — extraction input, schema, model, and output paths)",
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
    help="Validate resolved command inputs and exit without extracting",
)
@click.option(
    "--config-strict",
    is_flag=True,
    help="Fail on unknown keys in runtime config sections",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output directory (auto-detected if not specified)",
)
@click.option(
    "--schema",
    "-s",
    type=click.Path(exists=True),
    required=False,
    help="Schema file (for quick testing without a config YAML)",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name, or an alias defined in the run.yaml models: block. "
    "For Azure, use your deployment name (e.g. gpt-4o).",
)
@click.option(
    "--provider",
    type=click.Choice(
        ["openai", "azure", "anthropic", "gemini", "auto"],
        case_sensitive=False,
    ),
    default="auto",
    show_default=True,
    help="LLM provider (auto-detects from .env)",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=1),
    help="Process only first N pending files",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    show_default=True,
    help="Reset the entire resolved extraction output "
    "after successful preflight and re-extract the selected documents. "
    "Valid raw OCR (.text) and targeting (.pages/.sections) "
    "caches are reused.",
)
@click.option(
    "--max-context",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_CONTEXT,
    show_default=True,
    help="Max document characters to process",
)
@click.option(
    "--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)"
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Detailed output with statistics"
)
@click.option("--debug", is_flag=True, help="Debug mode with full logs")
@click.option(
    "--live-dashboard",
    is_flag=True,
    help="Show live dashboard during extraction",
)
@click.option(
    "--pages",
    type=str,
    default=None,
    help='Page range to extract (e.g., "615-759"). Only for single PDF files.',
)
@click.option(
    "--pages-csv",
    type=click.Path(exists=True),
    default=None,
    help="CSV file mapping documents to page ranges",
)
@click.option(
    "--from-index",
    "from_index",
    type=click.Path(exists=True),
    default=None,
    help="Load discovered files from a download_index.csv (bypasses PATH argument)",
)
@click.option(
    "--filter",
    "index_filters",
    type=str,
    multiple=True,
    help="With --from-index: keep only rows where COLUMN matches VALUE "
    "(format: column=value; repeatable; matches any download_index.csv column)",
)
def extract(
    path: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    output: Optional[str],
    schema: Optional[str],
    model: str,
    provider: str,
    limit: Optional[int],
    fresh: bool,
    max_context: int,
    quiet: bool,
    verbose: bool,
    debug: bool,
    live_dashboard: bool,
    pages: Optional[str],
    pages_csv: Optional[str],
    from_index: Optional[str] = None,
    index_filters: tuple = (),
):
    """Extract structured data from documents using an LLM.

    Reads PDF, DOCX, TXT, and XLSX files from PATH (or the configured
    input_dir) and writes one JSON file per document to the output directory.
    Already-processed files are skipped by default; pass --fresh to re-extract
    documents that already have output JSON (cached text/sidecars are still
    reused). Output defaults to ``extracted/<domain>/`` when --output is omitted;
    run ``psweep extract --help`` for every option and its default.

    Examples
    --------
    ::

        psweep extract --config config/my_domain/run.yaml --fresh
        psweep extract docs/ --schema schemas/my_schema.json
        psweep extract docs/ --schema s.json --model gpt-4o --provider azure
    """
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("extract", quiet=quiet, verbose=verbose, debug=debug)

    load_dotenv()
    cli_overrides = _explicit_cli_overrides(
        [
            "path",
            "schema",
            "output",
            "pages_csv",
            "pages",
            "provider",
            "model",
            "limit",
            "max_context",
            "live_dashboard",
            "fresh",
        ]
    )

    if from_index and "path" not in cli_overrides:
        cli_overrides["path"] = str(Path(from_index).resolve().parent)

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="extract",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    view.warnings(resolved_inputs.get("_config_warnings", []))
    if show_effective_config and not view.is_quiet:
        _print_effective_config("extract", resolved_inputs)
    resolved_inputs.setdefault("max_context", max_context)
    resolved_inputs.setdefault("provider", provider)
    try:
        plan = _prepare_extraction(
            resolved_inputs,
            fresh=fresh,
            from_index=from_index,
            index_filters=index_filters,
            config_path=config_path,
            validate_only=validate_config_only,
        )
        if validate_config_only:
            if view.is_quiet:
                click.echo(
                    json.dumps(
                        {
                            "command": "extract",
                            "status": "valid",
                            "resolved": {
                                k: v
                                for k, v in resolved_inputs.items()
                                if not k.startswith("_")
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                view.success(
                    "Runtime config validation passed for extract command"
                )
            return
        view.header("DOCUMENT EXTRACTION")
        view.config(
            {
                "Input": str(resolved_inputs["path"]),
                "Files": str(len(plan["documents"])),
                "Model": plan["llm"]["model"],
                "Output": str(plan["output"]),
            }
        )

        outcome = _render_extraction(
            plan,
            view,
            resolved_inputs.get("live_dashboard", live_dashboard),
        )
    except (ParseSweepError, OSError, ValueError, TypeError) as exc:
        view.error("Extraction failed", str(exc))
        raise click.exceptions.Exit(1) from exc
    if view.is_quiet:
        click.echo(str(outcome.manifest_path))
    else:
        if outcome.status == "reused":
            view.success(
                f"All {outcome.successful} file(s) already processed "
                "(verified reuse)"
            )
        view.summary(
            {
                "Status": outcome.status,
                "Successful": str(outcome.successful),
                "Failed": str(outcome.failed),
                "Items": str(outcome.total_items),
                "Known Cost": f"${outcome.total_cost:.4f}",
                "Unknown Cost Calls": str(outcome.unknown_cost_calls),
            },
            title="Extraction Summary",
        )
        view.outputs(
            {
                "Extracted data": str(outcome.output_dir),
                "Run manifest": str(outcome.manifest_path),
            }
        )
        if outcome.exit_code == 0:
            compile_cmd = (
                f"pixi run psweep compile --config {config_path}"
                if config_path
                else (
                    f"pixi run psweep compile {outcome.output_dir} "
                    f"--schema {schema}"
                )
            )
            view.next_steps(
                [
                    f"Inspect a result: pixi run psweep check "
                    f"{outcome.output_dir}/<name>.json --show-data",
                    f"Compile into a spreadsheet: {compile_cmd}",
                ]
            )
    raise click.exceptions.Exit(outcome.exit_code)


def _run_validation_extraction(
    doc_files: List[Path],
    loaded_schema: dict,
    schema_path: Path,
    output_dir: Path,
    registry,
    validation_models: List[str],
    max_context: int,
    timeout_seconds: Optional[int],
    page_range_map: dict,
    verbosity: str,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    config_path: Optional[str] = None,
    prompt_confirm: bool = True,
    skip_existing: bool = True,
    seed_output_dir: Optional[Path] = None,
    write_primary_canonical: bool = True,
    view: Optional[RunView] = None,
) -> Optional[Path]:
    """Run QA/QC multi-model extraction for documents.

    Model tiers and credentials are resolved through the unified
    :class:`ModelRegistry`; ``validation_models`` is the ``validation.models`` reference
    list from the run config.
    """
    from psweep.validation import run_multi_model_extraction

    if len({path.stem for path in doc_files}) != len(doc_files):
        message = (
            "QA/QC document stem collision; scope or rename ambiguous sources"
        )
        raise ValueError(message)
    qa_model_defs = registry.get_models(list(validation_models))
    qa_models = [definition.model for definition in qa_model_defs]
    seed_output_dir = seed_output_dir or output_dir
    if len(qa_model_defs) < 2:
        hint = (
            f"resolved to: {qa_models}"
            if qa_models
            else "resolved to nothing — validation.models is empty or missing"
        )
        print_error(
            "QA/QC Configuration Error",
            f"QA/QC requires at least 2 distinct models, but {hint}",
            [
                "Add a validation: section to your run config YAML with at least 2 model tiers:",
                "  validation:",
                "    models: [primary, secondary]",
                "Make sure both tiers are defined in the top-level models: block",
                "Each tier must resolve to a different model name",
                "See config/testing/validation_cross_provider.yaml for a ready-to-use example",
            ],
        )
        return None

    primary_def = qa_model_defs[0]
    primary_llm_kwargs = registry.to_llm_kwargs(primary_def.tier)
    qa_provider = primary_llm_kwargs.get("provider", "openai")

    # Build a provider-aware model list for the config display
    model_provider_labels = []
    for defn in qa_model_defs:
        kw = registry.to_llm_kwargs(defn.tier)
        provider_label = kw.get("provider", "openai")
        model_provider_labels.append(f"{defn.model} [{provider_label}]")

    view = view or RunView("extract", verbosity=Verbosity(verbosity))
    if view.command == "extract":
        view.header("QA/QC MULTI-MODEL VALIDATION")
        view.config(
            {
                "Models": ", ".join(model_provider_labels),
                "Documents": str(len(doc_files)),
                "Endpoint": primary_llm_kwargs.get("base_url")
                or "direct provider",
            }
        )
    if not view.is_quiet:
        view.warning(
            f"This will run {len(qa_models)}x extractions per document",
            f"Max fresh extractions: {len(doc_files)} docs × {len(qa_models)} models "
            f"= {len(doc_files) * len(qa_models)} (lower when model outputs are reused)",
        )
        if prompt_confirm and not ask_confirm(
            "Proceed with QA/QC extraction?", default=True
        ):
            view.info("Operation cancelled")
            return None

    results = []
    total_cost = 0.0
    total_time = 0.0
    total_fresh_extractions = 0
    total_reused_outputs = 0
    view.phase("Extracting with multiple models")

    # The primary model (first in the QA/QC list) doubles as a compile-ready
    # deliverable. Its extraction is written to the normal per-document location,
    # plus multi-model sidecars under validation/ for comparison.
    primary_model = primary_def.model
    progress = view.make_progress()
    task_id = None
    if progress is not None:
        task_id = progress.add_task(
            "Extracting QA/QC", total=len(doc_files), phase=""
        )

    def _update_phase(message: str) -> None:
        if progress is not None and task_id is not None:
            progress.update(task_id, phase=message)

    progress_ctx = (
        progress if progress is not None else contextlib.nullcontext()
    )
    with progress_ctx:
        for doc_idx, doc_path in enumerate(doc_files, 1):
            if progress is not None and task_id is not None:
                progress.update(
                    task_id,
                    description=f"Extracting [{doc_idx}/{len(doc_files)}] {doc_path.name[:48]}",
                )
                _update_phase("→ reading")
            elif not view.is_quiet:
                view.status(
                    "info", f"[{doc_idx}/{len(doc_files)}] {doc_path.name}"
                )

            try:
                page_range = page_range_map.get(doc_path)
                text = extract_text_from_document(
                    doc_path, page_range=page_range
                )
                seed_records_by_model = {}
                if skip_existing:
                    canonical_path = seed_output_dir / f"{doc_path.stem}.json"
                    if canonical_path.exists():
                        try:
                            with open(canonical_path, encoding="utf-8") as fh:
                                existing_record = json.load(fh)
                            existing_model = (
                                (existing_record.get("lineage") or {}).get(
                                    "model"
                                )
                                if isinstance(existing_record, dict)
                                else None
                            )
                            if (
                                isinstance(existing_model, str)
                                and existing_model in qa_models
                                and isinstance(
                                    existing_record.get("payload"), dict
                                )
                            ):
                                seed_records_by_model[existing_model] = (
                                    existing_record
                                )
                        except Exception as exc:
                            if view.verbosity.shows_detail:
                                view.status(
                                    "warning",
                                    f"Could not reuse existing extraction for {doc_path.name}",
                                    str(exc)[:80],
                                )

                    qa_doc_dir = output_dir / "validation" / doc_path.stem
                    if qa_doc_dir.exists():
                        for model_name in qa_models:
                            if model_name in seed_records_by_model:
                                continue
                            model_path = (
                                qa_doc_dir
                                / f"{sanitize_model_name(model_name)}.json"
                            )
                            if not model_path.exists():
                                continue
                            try:
                                with open(model_path, encoding="utf-8") as fh:
                                    existing_record = json.load(fh)
                                if isinstance(
                                    existing_record.get("payload"), dict
                                ):
                                    seed_records_by_model[model_name] = (
                                        existing_record
                                    )
                            except Exception as exc:
                                if view.verbosity.shows_detail:
                                    view.status(
                                        "warning",
                                        f"Could not reuse QA/QC output for {doc_path.name} ({model_name})",
                                        str(exc)[:80],
                                    )

                if view.verbosity.shows_detail:
                    view.status("info", f"Extracted {len(text):,} characters")

                def _model_status(event: Dict[str, Any]) -> None:
                    model_name = str(event.get("model") or "model")
                    kind = str(event.get("event") or "")
                    if kind == "model_start":
                        _update_phase(f"→ running {model_name}")
                    elif kind == "model_reused":
                        _update_phase(f"→ reused {model_name}")
                    elif kind == "model_success":
                        cost = event.get("cost")
                        display = (
                            "unknown cost" if cost is None else f"${cost:.4f}"
                        )
                        _update_phase(f"→ done {model_name} {display}")
                    elif kind == "model_error":
                        _update_phase(f"→ failed {model_name}")

                model_results = run_multi_model_extraction(
                    doc_text=text,
                    doc_name=doc_path.stem,
                    schema=loaded_schema,
                    registry=registry,
                    model_tiers=validation_models,
                    output_dir=output_dir,
                    max_context_chars=max_context,
                    timeout_seconds=timeout_seconds,
                    runtime_artifact=runtime_artifact,
                    run_id=run_id,
                    schema_id=schema_path.as_posix(),
                    seed_records_by_model=seed_records_by_model,
                    model_status_callback=_model_status,
                )

                primary_result = model_results.get(primary_model)
                if (
                    write_primary_canonical
                    and primary_result is not None
                    and primary_result.success
                    and primary_result.result is not None
                ):
                    write_extraction_record(
                        seed_output_dir / f"{doc_path.stem}.json",
                        doc_path=doc_path,
                        result=primary_result.result,
                        model=primary_model,
                        runtime_artifact=runtime_artifact,
                        run_id=run_id,
                        provider=qa_provider,
                        schema_id=schema_path.as_posix(),
                        schema_metadata=SchemaMetadata(schema_path),
                    )

                doc_cost = sum(r.cost or 0 for r in model_results.values())
                unknown_costs = sum(
                    r.cost is None for r in model_results.values()
                )
                doc_time = sum(
                    r.processing_time for r in model_results.values()
                )
                successful = sum(
                    1 for r in model_results.values() if r.success
                )
                doc_fresh = sum(
                    1
                    for r in model_results.values()
                    if r.success and not r.reused
                )
                doc_reused = sum(
                    1 for r in model_results.values() if r.success and r.reused
                )

                total_cost += doc_cost
                total_time += doc_time
                total_fresh_extractions += doc_fresh
                total_reused_outputs += doc_reused

                results.append(
                    {
                        "file": doc_path.name,
                        "success": True,
                        "models_successful": successful,
                        "models_total": len(qa_models),
                        "cost": doc_cost,
                        "unknown_cost_models": unknown_costs,
                        "time": doc_time,
                        "fresh_extractions": doc_fresh,
                        "reused_outputs": doc_reused,
                    }
                )

                level = (
                    "success" if successful == len(qa_models) else "warning"
                )
                view.status(
                    level,
                    f"{successful}/{len(qa_models)} models",
                    f"${doc_cost:.4f} known • {unknown_costs} unknown • "
                    f"{doc_time:.1f}s",
                )

            except Exception as e:
                results.append(
                    {
                        "file": doc_path.name,
                        "success": False,
                        "error": str(e),
                    }
                )
                view.status("error", "Extraction failed", str(e)[:60])

            if progress is not None and task_id is not None:
                progress.update(task_id, phase="", advance=1)

    if not view.is_quiet:
        successful_docs = [r for r in results if r.get("success")]
        failed_docs = [r for r in results if not r.get("success")]

        summary_stats = {
            "Documents Processed": str(len(results)),
            "Successful": str(len(successful_docs)),
        }
        if failed_docs:
            summary_stats["Failed"] = str(len(failed_docs))
        summary_stats["Models Used"] = (
            f"{len(qa_models)} ({', '.join(qa_models[:3])}"
            f"{'...' if len(qa_models) > 3 else ''})"
        )
        summary_stats["Fresh Model Extractions"] = str(total_fresh_extractions)
        summary_stats["Reused Existing Outputs"] = str(total_reused_outputs)
        summary_stats["Known Cost"] = f"${total_cost:.4f}"
        summary_stats["Unknown Cost Models"] = str(
            sum(r.get("unknown_cost_models", 0) for r in results)
        )
        summary_stats["Total Time"] = f"{total_time:.1f}s"

        view.summary(summary_stats, title="QA/QC Extraction Summary")

        validation_output = output_dir / "validation"
        view.outputs({"QA/QC outputs": str(validation_output.absolute())})

        validate_compare_command = "pixi run psweep validate"
        if config_path:
            validate_compare_command += (
                f" --config {Path(config_path).as_posix()}"
            )
        validate_compare_command += " --compare-only"
        view.next_steps(
            [
                "Rebuild QA/QC reports with validate (no re-extraction): "
                f"{validate_compare_command}"
            ]
        )

    return output_dir / "validation"
