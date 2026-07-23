"""
Core workflow CLI commands for ParseSweep.

This module contains the main data pipeline commands:
- extract: Extract structured data from documents to JSON
- compile: Merge JSON files into Excel/CSV
- discover: Find and download documents from the web
- curate: Filter discovered documents via human review
- compare: Generate QA/QC comparison reports
- benchmark: Performance profiling and gate evaluation
"""

import csv
import json
import hashlib
import logging
import os
import re
import sys
import warnings as std_warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import click
from click.core import ParameterSource
from dotenv import load_dotenv
from rich.logging import RichHandler

from psweep.utils.config import get_config
from psweep.extraction import DocumentExtractor, load_schema
from psweep.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS,
)
from psweep.compilation.data_compiler import DataCompiler
from psweep.discovery import (
    DiscoveryEngine,
    DiscoveryRequest,
)
from psweep.cli.ui import (
    console,
    print_header,
    print_error,
    print_warning,
    print_success,
    print_info,
    key_values,
    create_extraction_progress,
    ask_confirm,
    Verbosity,
    set_verbosity,
    get_verbosity,
)
from psweep.cli.run_view import RunView
from psweep.cli.dashboard import (
    create_live_dashboard,
    create_discovery_live_dashboard,
)
from psweep.benchmarking import (
    collect_benchmark_metrics,
    compare_benchmark_to_baseline,
    evaluate_benchmark_gates,
    load_benchmark_snapshot,
    write_benchmark_snapshot,
)
from psweep.config import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)


# Global verbosity level (set by CLI flags)

_BENCHMARK_PROFILE_PATH_FIELDS = {
    "path",
    "extraction_baseline_dir",
    "qaqc_baseline_dir",
    "compilation_baseline_dir",
    "compilation_schema",
    "baseline_snapshot",
    "write_snapshot",
}


def _explicit_cli_overrides(param_names: List[str]) -> Dict[str, Any]:
    """Return only values that were explicitly provided on the command line."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}

    overrides: Dict[str, Any] = {}
    for name in param_names:
        if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE:
            overrides[name] = ctx.params.get(name)
    return overrides


def _print_effective_config(
    command_name: str, resolved_values: Dict[str, Any]
) -> None:
    """Print resolved runtime config values and their source layer."""
    sources = resolved_values.get("_config_sources", {})
    display = {
        key: f"{value} [dim]({sources.get(key, 'default')})[/dim]"
        for key, value in resolved_values.items()
        if not key.startswith("_")
    }
    print_header(f"{command_name.upper()} EFFECTIVE CONFIG")
    console.print(key_values(display))


def _resolve_runtime_command_inputs(
    *,
    command_name: str,
    config_path: Optional[str],
    strict: bool,
    cli_values: Dict[str, Any],
) -> Dict[str, Any]:
    """Load and resolve command inputs from config files and CLI overrides.

    Two usage modes:
    - --config: loads full domain config (schema, page targeting, dedup, etc.)
    - --schema: quick mode with just the extraction schema (no runtime config)
    """
    config_data = None

    if config_path:
        config_data = load_runtime_config_file(Path(config_path))

    result = resolve_command_config(
        command=command_name,
        cli_values=cli_values,
        config_data=config_data,
        strict=strict,
    )

    return result


def configure_logging(verbosity: str) -> None:
    """
    Configure logging with RichHandler for clean integration with Rich UI components.

    RichHandler ensures log messages don't interfere with progress bars and other
    Rich Live displays. This function removes any existing handlers to ensure clean
    state regardless of prior logging configuration.

    Args:
        verbosity: One of 'quiet', 'normal', 'verbose', 'debug'
    """
    # Get root logger and clear any existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Configure based on verbosity level
    if verbosity == "quiet":
        level = logging.ERROR
        show_level = False
        show_path = False
    elif verbosity == "debug":
        level = logging.DEBUG
        show_level = True
        show_path = True
    else:  # 'normal' or 'verbose'
        level = logging.WARNING
        show_level = False
        show_path = False

    # Create and add RichHandler
    handler = RichHandler(
        console=console,
        show_time=False,
        show_level=show_level,
        show_path=show_path,
        markup=True,
    )
    handler.setLevel(level)
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def begin_run(
    command: str,
    *,
    quiet: bool = False,
    verbose: bool = False,
    debug: bool = False,
) -> RunView:
    """Resolve verbosity, wire logging, and return a RunView for a command.

    The single entry point for command setup: it resolves the shared
    :class:`Verbosity` (readable anywhere via ``get_verbosity()``), configures
    logging, and hands back the narrative controller the command renders
    through. This replaces the per-command verbosity/logging boilerplate that
    used to be copy-pasted into every command.
    """
    resolved = Verbosity.from_flags(quiet=quiet, verbose=verbose, debug=debug)
    set_verbosity(resolved)
    configure_logging(resolved.value)
    return RunView(command, verbosity=resolved)


def detect_api_provider() -> Tuple[str, bool, str]:
    """
    Auto-detect which API provider to use based on .env credentials.

    Returns:
        Tuple of (provider_name, is_valid, error_message)
        provider_name: 'azure' or 'openai' or None
    """
    # Check for Azure credentials first (preferred if available)
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")

    if azure_key and azure_endpoint:
        return "azure", True, None

    # Fall back to OpenAI
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return "openai", True, None

    # No credentials found
    error = (
        "No API credentials found in .env file.\n\n"
        "Option 1 - Use Azure OpenAI (recommended, higher rate limits):\n"
        "  AZURE_OPENAI_API_KEY=your-azure-key\n"
        "  AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/\n"
        "  AZURE_OPENAI_MODEL=your-model-name\n\n"
        "Option 2 - Use OpenAI:\n"
        "  OPENAI_API_KEY=sk-your-key-here"
    )
    return None, False, error


def _load_benchmark_gate_profile(profile_path: Path) -> Dict[str, Any]:
    """Load a benchmark gate profile and resolve relative paths against the profile location."""
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
    """Prefer an explicit CLI value, then fall back to the loaded benchmark profile."""
    return cli_value if cli_value is not None else profile.get(key)


def _resolve_runtime_artifact(
    category: Optional[str],
    schema_path: Path,
    profile_name: str = "default",
    **kwargs,
) -> Optional[Dict[str, Any]]:
    """Runtime artifact resolution — returns None (config-driven system)."""
    return None


def _format_runtime_artifact_summary(
    runtime_artifact: Optional[Dict[str, Any]],
) -> str:
    """Return a concise user-facing summary of the runtime configuration."""
    if not runtime_artifact:
        return "config-driven (schema + domain config)"

    lineage = runtime_artifact.get("lineage") or {}
    pack_name = (
        lineage.get("pack_name")
        or runtime_artifact.get("pack_name")
        or "unknown-pack"
    )
    profile_name = (
        lineage.get("profile_id")
        or runtime_artifact.get("profile_name")
        or "default"
    )
    artifact_id = (
        runtime_artifact.get("artifact_id")
        or lineage.get("artifact_id")
        or "artifact://runtime/unresolved"
    )
    artifact_suffix = artifact_id.rsplit("/", 1)[-1]
    return (
        f"pack={pack_name}, profile={profile_name}, artifact={artifact_suffix}"
    )


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
        "rows_after_dedup": rows_before_dedup
        - dedup_preview["duplicates_removed"],
        "duplicates_removed": dedup_preview["duplicates_removed"],
        "key_fields": dedup_preview["key_fields"],
        "compare_columns": dedup_preview["compare_columns"],
        "warnings": dedup_preview.get("warnings") or [],
        "suspicious_groups_count": dedup_preview.get(
            "suspicious_groups_count", 0
        ),
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


def _generate_run_id(
    *,
    schema_path: Path,
    provider: str,
    model: str,
    enable_qa_qc: bool,
    doc_files: List[Path],
    artifact_id: Optional[str],
) -> str:
    """Generate a deterministic run identifier for lineage joins."""
    seed = {
        "schema": schema_path.as_posix(),
        "provider": provider,
        "model": model,
        "mode": "qa_qc" if enable_qa_qc else "single_model",
        "documents": sorted(path.as_posix() for path in doc_files),
        "artifact_id": artifact_id,
    }
    canonical = json.dumps(seed, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"run://{digest[:16]}"


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


def _build_run_manifest(
    *,
    run_id: str,
    mode: str,
    schema_path: Path,
    provider: str,
    model: str,
    runtime_artifact: Optional[Dict[str, Any]],
    doc_files: List[Path],
    successful_output_paths: List[Path],
    started_at: str,
    finished_at: str,
    total_processed: int,
    successful_count: int,
    failed_count: int,
    failed_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a deterministic run manifest payload for process executions."""
    lineage = (runtime_artifact or {}).get("lineage") or {}
    manifest_errors = summarize_error_records(
        error
        for result in (failed_results or [])
        for error in result.get("errors", [])
    )
    return {
        "manifest_version": "1.0.0",
        "run_id": run_id,
        "mode": mode,
        "lineage": {
            "artifact_id": (runtime_artifact or {}).get("artifact_id")
            or "artifact://runtime/unresolved",
            "profile_id": lineage.get("profile_id") or "default",
            "schema_id": schema_path.as_posix(),
            "provider": provider,
            "model": model,
        },
        "documents": sorted(path.as_posix() for path in doc_files),
        "outputs": {
            "records": sorted(
                path.as_posix() for path in successful_output_paths
            ),
        },
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "status": {
            "total_processed": total_processed,
            "successful": successful_count,
            "failed": failed_count,
            "result": "success" if failed_count == 0 else "partial_failure",
        },
        "errors": manifest_errors,
    }


def _write_run_manifest(
    output_dir: Path, run_id: str, manifest: Dict[str, Any]
) -> Path:
    """Persist a run manifest in the run_manifests output folder."""
    manifest_dir = output_dir / "run_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    run_suffix = run_id.replace("run://", "")
    manifest_path = manifest_dir / f"{run_suffix}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _resolve_schema_ref(schema_path: Path) -> Path:
    """Resolve a schema reference, unwrapping a domain pack to its JSON schema.

    If ``schema_path`` is a domain pack (``*.yaml``/``*.yml`` with a
    ``schema_path`` key), return the JSON schema it declares (resolved relative
    to the repo root or the pack's own directory). Otherwise return the path
    unchanged so a direct JSON schema still works.
    """
    if schema_path.suffix.lower() not in {".yaml", ".yml"}:
        return schema_path
    try:
        import yaml

        data = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - fall through to a clear "not found" error
        return schema_path
    declared = data.get("schema_path")
    if not declared:
        return schema_path
    declared_path = Path(declared)
    if declared_path.is_absolute() and declared_path.exists():
        return declared_path
    for base in (Path.cwd(), schema_path.parent):
        candidate = base / declared
        if candidate.exists():
            return candidate
    return declared_path


def _slugify_value(value) -> str:
    """Lowercase + hyphenate a value for case/format-insensitive matching."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _build_index_filters(
    filters,
    filter_state: Optional[str] = None,
    filter_jurisdiction: Optional[str] = None,
) -> List[tuple]:
    """Return ``[(column, slug_value), ...]`` for download-index row filtering.

    Domain-neutral: ``--filter column=value`` filters on any
    ``download_index.csv`` column. The legacy ``--filter-state`` /
    ``--filter-jurisdiction`` options are thin back-compat aliases that map onto
    the same mechanism (columns ``source_state`` / ``source_jurisdiction``), so
    existing runs behave identically while new domains use the generic flag.
    """
    pairs: List[tuple] = []
    for raw in filters or ():
        if "=" in str(raw):
            col, val = str(raw).split("=", 1)
            col = col.strip()
            if col:
                pairs.append((col, _slugify_value(val)))
    if filter_state:
        pairs.append(("source_state", _slugify_value(filter_state)))
    if filter_jurisdiction:
        pairs.append(
            ("source_jurisdiction", _slugify_value(filter_jurisdiction))
        )
    return pairs


def _row_matches_filters(row: dict, filters: List[tuple]) -> bool:
    """True when a CSV row matches every ``(column, slug_value)`` filter."""
    return all(_slugify_value(row.get(col)) == val for col, val in filters)


def _build_source_context_map(
    input_path: Optional[Path],
    from_index: Optional[str],
) -> Dict[str, Dict[str, str]]:
    """Map each discovered document's filename to its origin URL + queried target.

    Reads the discover run's ``download_index.csv`` (which records the source
    ``url``/``final_url`` and the ``target_metadata`` each file was found for) so
    the process step can (1) cite the source URL for extracted facts and
    (2) anchor extraction to the project the document was searched for, rather
    than to unrelated content elsewhere on the page. Domain-neutral: any
    web-discover run benefits. Returns ``{}`` when no index can be located
    (e.g. processing a hand-curated directory).
    """
    index_path: Optional[Path] = None

    if from_index:
        candidate = Path(from_index)
        if candidate.is_file():
            index_path = candidate

    if index_path is None and input_path is not None:
        # input_dir is typically <run>/curated or <domain>/latest/curated;
        # download_index.csv sits at the run root. Walk up a few levels but do
        # not wander above the discovery tree.
        base = input_path if input_path.is_dir() else input_path.parent
        for up in (base, *base.parents):
            candidate = up / "download_index.csv"
            if candidate.is_file():
                index_path = candidate
                break
            if up.name == "discovered":
                break

    if index_path is None:
        return {}

    context_map: Dict[str, Dict[str, str]] = {}
    try:
        with index_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                path_val = row.get("path") or row.get("relative_path")
                if not path_val:
                    continue
                name = Path(path_val).name
                meta: Dict[str, Any] = {}
                meta_raw = row.get("target_metadata")
                if meta_raw:
                    try:
                        meta = json.loads(meta_raw)
                    except Exception:
                        meta = {}
                context_map[name] = {
                    "url": (
                        row.get("final_url") or row.get("url") or ""
                    ).strip(),
                    "site_name": str(
                        meta.get("site_name")
                        or row.get("target_label")
                        or ""
                    ),
                    "company_name": str(meta.get("company_name") or ""),
                    "city": str(meta.get("city") or ""),
                    "state": str(meta.get("state") or ""),
                }
    except Exception:
        return {}
    return context_map


def _prepend_source_context(
    text: str,
    doc_path: Path,
    context_map: Dict[str, Dict[str, str]],
) -> str:
    """Prepend a CONTEXT block (source URL + queried site) to document text.

    This lets schema fields copy the origin URL as a citation and anchor the
    extraction to the intended project. No-op when the document has no discover
    provenance in ``context_map``.
    """
    info = context_map.get(doc_path.name)
    if not info or not (info.get("url") or info.get("site_name")):
        return text

    location = ", ".join(
        part for part in (info.get("city"), info.get("state")) if part
    )
    lines = [
        "=== CONTEXT (added by ParseSweep; not part of the source document) ==="
    ]
    if info.get("url"):
        lines.append(f"SOURCE_URL: {info['url']}")
    if info.get("site_name"):
        lines.append(f"QUERIED_SITE: {info['site_name']}")
    if info.get("company_name"):
        lines.append(f"QUERIED_COMPANY: {info['company_name']}")
    if location:
        lines.append(f"QUERIED_LOCATION: {location}")
    lines.append("=== END CONTEXT ===")
    lines.append("")
    return "\n".join(lines) + "\n" + text


def _apply_page_targeting(
    *,
    doc_files: list,
    page_range_map: dict,
    config: dict,
    models: dict | None = None,
    pages_csv: str | None = None,
    output_dir: Path | None = None,
) -> None:
    """Fill page_range_map for large PDFs via LLM-assisted page targeting.

    Only touches PDFs that are large (full text exceeds ``trigger_chars``) and
    have no entry in the page_range_map — manual CSV ranges and explicit
    full-doc entries always win.
    Best-effort: any locator failure leaves the file for full extraction.

    Writes discovered page ranges to ``discovered_page_ranges.csv`` next to
    the configured pages CSV (or in output_dir) for human review.
    """
    from psweep.extraction.page_locator import PageLocator
    from psweep.extraction.pdf_utils import extract_pages_text

    description = str(config.get("section_description") or "").strip()
    if not description:
        print_warning(
            "pages.auto_locate is set but section_description is missing; "
            "skipping page targeting."
        )
        return

    trigger_chars = int(config.get("trigger_chars", 200_000) or 200_000)
    locator = PageLocator(
        description,
        model=config.get("model"),
        models=models,
        trigger_chars=trigger_chars,
        max_selected_pages=int(config.get("max_selected_pages", 30) or 30),
        keywords=config.get("keywords"),
    )

    discovered: list[tuple[str, int, int]] = []

    for doc in doc_files:
        if doc.suffix.lower() != ".pdf":
            continue
        if doc in page_range_map:
            continue  # listed in CSV (with range or explicit full-doc)
        pages = extract_pages_text(doc)
        if not pages:
            continue
        if sum(len(p) for p in pages) <= trigger_chars:
            continue  # small enough to extract in full
        rng = locator.locate(doc, pages=pages)
        if rng:
            page_range_map[doc] = rng
            discovered.append((doc.name, rng[0], rng[1]))
            if not get_verbosity().is_quiet:
                print_info(
                    f"Page targeting: {doc.name} → pages {rng[0]}-{rng[1]}"
                )

    # Write discovered ranges to a CSV for human review/promotion.
    if discovered and config.get("save_discovered", False):
        if pages_csv:
            dest = Path(pages_csv).parent / "discovered_page_ranges.csv"
        elif output_dir:
            dest = output_dir / "discovered_page_ranges.csv"
        else:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        import csv

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


def _document_progress_desc(doc_path: Path, page_range_map: dict) -> str:
    """Build a progress-bar label for a document, with an optional page suffix."""
    desc = doc_path.name
    page_range = page_range_map.get(doc_path)
    if page_range is not None:
        start, end = page_range
        desc += f" [dim](pages {start}-{end})[/dim]"
    return desc


def _extract_one_document(
    doc_path: Path,
    *,
    extractor,
    loaded_schema: dict,
    page_range_map: dict,
    source_context_map: dict,
    file_output_dirs: dict,
    output_dir: Path,
    category: str,
    actual_model: str,
    enable_qa_qc: bool,
    runtime_artifact,
    run_id,
    provider,
    schema_path: Path,
    identifier_fields=None,
) -> dict:
    """Extract one document and persist its record.

    Shared core of the three presentation loops (live dashboard / progress bar /
    single-file). Returns a result dict describing success or failure — the exact
    shape the run-summary and manifest aggregation consume — so each caller only
    has to render its own UI. Never raises: any extraction error is captured into
    a structured failure record.
    """
    try:
        page_range = page_range_map.get(doc_path)
        text = extract_text_from_document(doc_path, page_range=page_range)
        text = _prepend_source_context(text, doc_path, source_context_map)
        result = extractor.extract(text, loaded_schema)

        doc_output_dir = file_output_dirs.get(doc_path, output_dir)
        num_items = _extract_and_save_result(
            doc_path,
            result,
            doc_output_dir,
            category,
            actual_model,
            enable_qa_qc,
            runtime_artifact=runtime_artifact,
            run_id=run_id,
            provider=provider,
            schema_id=loaded_schema.get("$id"),
            identifier_fields=identifier_fields,
        )
        return {
            "file": doc_path.name,
            "items": num_items,
            "cost": result.cost,
            "time": result.processing_time,
            "input_tokens": getattr(result, "input_tokens", None),
            "output_tokens": getattr(result, "output_tokens", None),
            "output_path": (doc_output_dir / f"{doc_path.stem}.json").as_posix(),
            "success": True,
        }
    except Exception as e:
        error_record = build_error_record(
            e,
            stage="extract",
            document_path=doc_path.as_posix(),
            model=actual_model,
            provider=provider,
        )
        suggestions = _context_budget_suggestions_for_process(
            error_record=error_record,
            schema_path=schema_path,
            document_path=doc_path,
        )
        return {
            "file": doc_path.name,
            "success": False,
            "error": str(e),
            "errors": [error_record],
            "suggestions": suggestions,
        }


@click.command()
@click.argument("path", type=click.Path(), required=False)
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
    "--category",
    help="Category name (auto-detected from path if not specified)",
)
@click.option(
    "--model",
    default="gpt-4o-mini",
    show_default=True,
    help="AI model (e.g., gpt-4o-mini, claude-3.5-sonnet, gemini-1.5-pro)",
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
    "--profile",
    "profile_name",
    default="default",
    show_default=True,
    help="Runtime profile to compile into artifact lineage (for example: default, dev, staging, prod)",
)
@click.option(
    "--enable-qa-qc", is_flag=True, help="Enable multi-model QA/QC validation"
)
@click.option(
    "--qaqc-lane",
    type=str,
    default=None,
    help="Optional QA/QC lane name to use in the follow-up compare workflow when --enable-qa-qc is used",
)
@click.option("--limit", "-n", type=int, help="Process only first N files")
@click.option(
    "--skip-existing/--reprocess",
    default=True,
    show_default=True,
    help="Skip files already processed",
)
@click.option(
    "--max-context",
    type=int,
    default=400000,
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
@click.option(
    "--filter-state",
    "filter_state",
    type=str,
    default=None,
    help="Back-compat alias for --filter source_state=VALUE",
)
@click.option(
    "--filter-jurisdiction",
    "filter_jurisdiction",
    type=str,
    default=None,
    help="Back-compat alias for --filter source_jurisdiction=VALUE",
)
def extract(
    path: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    output: Optional[str],
    schema: Optional[str],
    category: Optional[str],
    model: str,
    provider: str,
    profile_name: str,
    enable_qa_qc: bool,
    qaqc_lane: Optional[str],
    limit: Optional[int],
    skip_existing: bool,
    max_context: int,
    quiet: bool,
    verbose: bool,
    debug: bool,
    live_dashboard: bool,
    pages: Optional[str],
    pages_csv: Optional[str],
    from_index: Optional[str] = None,
    index_filters: tuple = (),
    filter_state: Optional[str] = None,
    filter_jurisdiction: Optional[str] = None,
):
    """
    Extract structured data from documents.

    This command reads documents and extracts structured information
    based on a JSON schema. Works with any document type (permits, ordinances, regulations, etc.).

    Supports multiple LLM providers: OpenAI, Azure OpenAI, Claude, Gemini, and more.
    See docs/MODEL_COSTS.md for cost comparison and model selection guidance.

    The output will be saved as JSON files in a parallel folder structure.
    For example: documents/Category/ → extracted/Category/

    \b
    EXAMPLES:
        # Extract from a directory against a schema (or a domain-pack pack.yaml)
        psweep extract <input_dir> --schema <schema>

        # Drive everything from a domain's runtime config
        psweep extract --config config/<domain>/run.yaml

        # Pick a specific model / provider for this run
        psweep extract <input_dir> --schema <schema> --model <model_name>
        psweep extract <input_dir> --schema <schema> --provider azure

        # Compile artifact lineage under a named profile
        psweep extract <input_dir> --schema <schema> --profile prod

        # Restrict large PDFs to specific pages
        psweep extract <input_dir> --schema <schema> --pages-csv <page_ranges.csv>

        # Test with the first 5 documents
        psweep extract <input_dir> --schema <schema> -n 5

    \b
    MODELS:
        Any model name your provider supports (OpenAI, Azure, Claude, Gemini,
        and more). Unset → inherit the model from your environment/.env.
        See docs/MODEL_COSTS.md for cost comparison and model selection guidance.

    \b
    REQUIREMENTS:
        • Documents in the specified directory
        • JSON schema file (--schema flag is REQUIRED)
        • API key in .env file for your chosen provider
    """
    view = begin_run("extract", quiet=quiet, verbose=verbose, debug=debug)

    # Load environment variables from .env file
    load_dotenv()

    cli_overrides = _explicit_cli_overrides(
        [
            "path",
            "schema",
            "output",
            "pages_csv",
            "pages",
            "profile_name",
            "provider",
            "model",
            "limit",
            "skip_existing",
            "max_context",
            "enable_qa_qc",
            "qaqc_lane",
            "live_dashboard",
        ]
    )

    # When --from-index is provided with no explicit path, read the CSV early to inject
    # a valid path so the config resolver's required-path check does not fail.
    _from_index_data: dict = {}
    if from_index and "path" not in cli_overrides:
        try:
            _fi_path = Path(from_index)
            with _fi_path.open(encoding="utf-8", newline="") as _fi_handle:
                _fi_rows = list(csv.DictReader(_fi_handle))
            _fi_downloaded = [
                r for r in _fi_rows if r.get("status") == "downloaded"
            ]
            _fi_filters = _build_index_filters(
                index_filters, filter_state, filter_jurisdiction
            )
            if _fi_filters:
                _fi_downloaded = [
                    r
                    for r in _fi_downloaded
                    if _row_matches_filters(r, _fi_filters)
                ]
            if _fi_downloaded:
                _fi_existing = [
                    Path(r["path"])
                    for r in _fi_downloaded
                    if r.get("path") and Path(r["path"]).exists()
                ]
                _from_index_data = {
                    "doc_files": _fi_existing,
                    "domain": _fi_downloaded[0].get("domain", "acquired"),
                }
                if _fi_existing:
                    cli_overrides["path"] = str(_fi_existing[0].parent)
        except Exception as _fi_exc:
            print_warning(f"Could not pre-read download index: {_fi_exc}")

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


    warnings = resolved_inputs.get("_config_warnings", [])
    view.warnings(warnings)

    if show_effective_config and not get_verbosity().is_quiet:
        _print_effective_config("extract", resolved_inputs)
        console.print()

    if validate_config_only:
        if get_verbosity().is_quiet:
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
            print_success(
                "Runtime config validation passed for extract command"
            )
        return

    path = Path(resolved_inputs["path"])
    schema = resolved_inputs["schema"]
    output = resolved_inputs.get("output", output)
    pages_csv = resolved_inputs.get("pages_csv", pages_csv)
    pages = resolved_inputs.get("pages", pages)
    profile_name = resolved_inputs.get("profile_name", profile_name)
    provider = resolved_inputs.get("provider", provider)
    model = resolved_inputs.get("model", model)
    limit = resolved_inputs.get("limit", limit)
    skip_existing = resolved_inputs.get("skip_existing", skip_existing)
    max_context = resolved_inputs.get("max_context", max_context)
    enable_qa_qc = resolved_inputs.get("enable_qa_qc", enable_qa_qc)
    qaqc_lane = resolved_inputs.get("qaqc_lane", qaqc_lane)
    live_dashboard = resolved_inputs.get("live_dashboard", live_dashboard)

    # Validate path exists
    if not path.exists():
        print_error(
            f"Path not found: {path}",
            "The file or directory you specified doesn't exist.",
            [
                "Check the path spelling and try again",
                f"Current directory: {Path.cwd()}",
                "Use 'ls' or 'dir' to see available files and folders",
            ],
        )
        sys.exit(1)

    # Load configuration
    config = get_config()
    path = Path(path)

    # Determine if single file or directory
    is_dir = path.is_dir()

    # Determine category from path for metadata
    if is_dir:
        category = path.name
    else:
        category = path.parent.name

    # Setup output directory - CLEAN parallel structure
    # documents/category/ → extracted/category/
    # discovered/<domain>/.../curated → extracted/<domain>/
    def _discover_domain(p: Path) -> Optional[str]:
        """Return <domain> if p is under discovered/<domain>/..."""
        resolved_parts = list(p.resolve().parts)
        if "discovered" in resolved_parts:
            i = resolved_parts.index("discovered")
            if i + 1 < len(resolved_parts):
                return resolved_parts[i + 1]
        return None

    if output:
        output_dir = Path(output)
    else:
        probe = path if is_dir else path.parent
        disc_domain = _discover_domain(probe)
        if disc_domain:
            # Curated discovery output → extracted/<domain>/
            output_dir = Path.cwd() / "extracted" / disc_domain
        else:
            parts = list(probe.parts)
            if "documents" in parts:
                idx = parts.index("documents")
                parts[idx] = "extracted"
                output_dir = Path(*parts)
            else:
                # Fallback: create in project root extracted/
                output_dir = Path.cwd() / "extracted" / probe.name

    output_dir.mkdir(parents=True, exist_ok=True)

    # Get document files - support all formats (PDF, DOCX, TXT, XLSX, CSV)
    # Tracks per-file output directories for nested folder structures
    file_output_dirs = {}  # Maps doc_path -> its specific output directory

    def _not_sidecar(p: Path) -> bool:
        """Exclude hidden helper dirs (e.g. .text/ OCR cache, .review/)."""
        return not any(part.startswith(".") for part in p.relative_to(path).parts)

    if is_dir:
        # Find all supported document types in this directory (non-recursive first)
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(path.glob(f"*{ext}")))
        doc_files = sorted(f for f in doc_files if _not_sidecar(f))

        # If no documents found directly, search recursively in subdirectories
        if not doc_files:
            for ext in SUPPORTED_EXTENSIONS:
                doc_files.extend(sorted(path.rglob(f"*{ext}")))
            doc_files = sorted(f for f in doc_files if _not_sidecar(f))

            if doc_files and not get_verbosity().is_quiet:
                # Show subfolder summary
                subdirs_found = set()
                for doc in doc_files:
                    try:
                        rel = doc.relative_to(path)
                        if len(rel.parts) > 1:
                            subdirs_found.add(rel.parts[0])
                    except ValueError:
                        pass
                if subdirs_found:
                    view.info(
                        f"Found {len(doc_files)} document(s) across "
                        f"{len(subdirs_found)} subfolder(s)"
                    )
                    # Show per-subfolder counts
                    for sdir in sorted(subdirs_found):
                        sdir_docs = [
                            d
                            for d in doc_files
                            if d.relative_to(path).parts[0] == sdir
                        ]
                        view.status(
                            "info", f"{sdir}: {len(sdir_docs)} document(s)"
                        )

        # Build per-file output directory mapping (mirrors input structure)
        for doc in doc_files:
            try:
                rel_parent = doc.parent.relative_to(path)
                file_output_dirs[doc] = output_dir / rel_parent
            except ValueError:
                file_output_dirs[doc] = output_dir
            file_output_dirs[doc].mkdir(parents=True, exist_ok=True)

        # Check if we found any documents at all
        if not doc_files:
            supported_exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            print_error(
                f"No supported documents found in: {path}",
                "The directory exists but doesn't contain any supported document files.",
                [
                    f"Supported formats: {supported_exts}",
                    "Check if documents are in a subdirectory",
                    f"Use 'ls {path}' to see what's in this folder",
                ],
            )
            sys.exit(1)

        if limit:
            doc_files = doc_files[:limit]
        if skip_existing:
            original_count = len(doc_files)
            doc_files = [
                p
                for p in doc_files
                if not (
                    file_output_dirs.get(p, output_dir) / f"{p.stem}.json"
                ).exists()
            ]
            skipped = original_count - len(doc_files)
            if skipped > 0 and len(doc_files) > 0 and not get_verbosity().is_quiet:
                print_info(
                    f"Skipping {skipped} already processed file{'s' if skipped != 1 else ''} (use --reprocess to extract again)"
                )
    else:
        # Single file
        if not is_supported_document(path):
            supported_exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            print_error(
                f"Unsupported file format: {path.name}",
                f"This tool works with: {supported_exts}",
                [
                    "Make sure the file has a supported extension",
                    "Check if you specified the correct file path",
                ],
            )
            sys.exit(1)
        doc_files = [path]

    # --from-index: override doc_files with files listed in the discovery CSV.
    # This wires the discover → extract pipeline without any manual path wrangling.
    if from_index and _from_index_data.get("doc_files"):
        doc_files = _from_index_data["doc_files"]
        if limit:
            doc_files = doc_files[:limit]
        # Override output dir to extracted/<domain>/ unless explicitly set
        if not output:
            output_dir = Path("extracted") / _from_index_data["domain"]
            output_dir.mkdir(parents=True, exist_ok=True)
            category = _from_index_data["domain"]
        # Rebuild file_output_dirs for the --from-index files
        file_output_dirs = {}
        for doc in doc_files:
            file_output_dirs[doc] = output_dir
        if skip_existing:
            original_count = len(doc_files)
            doc_files = [
                p
                for p in doc_files
                if not (
                    file_output_dirs.get(p, output_dir) / f"{p.stem}.json"
                ).exists()
            ]
            skipped = original_count - len(doc_files)
            if skipped > 0 and not get_verbosity().is_quiet:
                print_info(
                    f'Skipping {skipped} already processed file{"s" if skipped != 1 else ""} (use --reprocess to extract again)'
                )
        if not doc_files:
            print_error(
                "No new files to process from download index",
                "All discovered files have already been processed. Use --reprocess to extract again.",
            )
            sys.exit(0)
        if not get_verbosity().is_quiet:
            print_info(
                f"From-index mode: {len(doc_files)} file(s) loaded from {from_index}"
            )

    # Handle page range specifications
    from psweep.utils.page_range import (
        parse_page_range,
        load_pages_csv,
    )

    page_range_map = {}  # Maps file paths to (start, end) tuples

    if pages_csv:
        # Load page ranges from CSV file
        try:
            page_mappings = load_pages_csv(Path(pages_csv))

            # Match file names from doc_files to mappings
            for doc in doc_files:
                # Try exact match first
                if str(doc) in page_mappings:
                    page_range_map[doc] = page_mappings[str(doc)]
                elif doc.name in page_mappings:
                    page_range_map[doc] = page_mappings[doc.name]

            if not view.is_quiet:
                mapped_count = sum(
                    1 for v in page_range_map.values() if v is not None
                )
                view.status(
                    "info",
                    f"Loaded page ranges for {mapped_count} file(s) from CSV",
                )

        except Exception as e:
            print_error(
                "Invalid page ranges CSV",
                str(e),
                [
                    "CSV format should be:",
                    "  file_path,start_page,end_page",
                    "  tariff1.pdf,615,759",
                    "  tariff2.pdf,400,550",
                ],
            )
            sys.exit(1)

    elif pages:
        # Single file with page range
        if len(doc_files) > 1:
            print_error(
                "--pages flag only works with single file",
                f"You specified --pages but selected {len(doc_files)} files",
                [
                    "Use --pages only when processing a single PDF",
                    "For multiple files, use --pages-csv instead",
                ],
            )
            sys.exit(1)

        if doc_files[0].suffix.lower() != ".pdf":
            print_error(
                "--pages only works with PDF files",
                f"File {doc_files[0].name} is not a PDF",
                ["Page ranges are only supported for PDF documents"],
            )
            sys.exit(1)

        try:
            page_range_tuple = parse_page_range(pages)
            page_range_map[doc_files[0]] = page_range_tuple

            if not view.is_quiet:
                view.status(
                    "info",
                    f"Extracting pages {page_range_tuple[0]}-"
                    f"{page_range_tuple[1]} only",
                )

        except ValueError as e:
            print_error(
                "Invalid page range format",
                str(e),
                [
                    "Use format like: --pages 615-759",
                    "Or with colons: --pages 100:200",
                ],
            )
            sys.exit(1)

    # LLM-assisted page targeting (from pages.auto_locate or legacy
    # page_targeting). For large PDFs with no CSV entry, auto-locate the pages
    # holding the described section so extraction targets them instead of
    # overflowing the context. CSV entries always win.
    page_targeting = resolved_inputs.get("page_targeting")
    if isinstance(page_targeting, dict) and page_targeting.get("enabled"):
        _apply_page_targeting(
            doc_files=doc_files,
            page_range_map=page_range_map,
            config=page_targeting,
            models=resolved_inputs.get("models"),
            pages_csv=pages_csv,
            output_dir=output_dir,
        )

    # Final validation - check if we have files to process
    if not doc_files:
        if is_dir and not view.is_quiet:
            view.header("DOCUMENT EXTRACTION")
            view.success(f"All {original_count} file(s) already processed")
            view.outputs({"Output directory": str(output_dir)})
            view.next_steps(
                ["Re-extract everything with the --reprocess flag"]
            )
        return

    # Display header and configuration
    view.header("DOCUMENT EXTRACTION")
    if not view.is_quiet:
        # Build configuration display - use relative paths where possible
        try:
            rel_input = path.relative_to(Path.cwd())
            input_display = str(rel_input)
        except ValueError:
            input_display = str(path)

        config_info = {
            "Input": input_display,
            "Files": f"{len(doc_files)} document{'s' if len(doc_files) != 1 else ''}",
        }

    # Determine provider and model info from config
    provider_name = config.llm_config.get("provider", "unknown").title()
    # Resolve model display through the tier system so the banner matches what
    # the extraction will actually use (not just the env default).
    if model and resolved_inputs.get("models"):
        from psweep.extraction.llm_factory import resolve_model_name
        model_display = resolve_model_name(
            model, models=resolved_inputs.get("models"),
            llm_config=config.llm_config,
        )
    else:
        model_display = config.llm_config.get("model", model)

    # Add model/provider info to config
    if not get_verbosity().is_quiet:
        config_info["Model"] = model_display
        config_info["Provider"] = provider_name
        config_info["QA/QC"] = "Enabled" if enable_qa_qc else "Disabled"

        # Show page range status
        if page_range_map:
            # Count how many files have page ranges
            files_with_ranges = sum(
                1 for v in page_range_map.values() if v is not None
            )
            if files_with_ranges == 1 and len(doc_files) == 1:
                # Single file with specific pages
                start, end = list(page_range_map.values())[0]
                if pages_csv:
                    # Show CSV source
                    try:
                        csv_rel = Path(pages_csv).relative_to(Path.cwd())
                        config_info["Pages"] = f"{csv_rel} ({start}-{end})"
                    except ValueError:
                        config_info["Pages"] = f"{pages_csv} ({start}-{end})"
                else:
                    config_info["Pages"] = f"{start}-{end}"
            elif files_with_ranges > 0:
                # Multiple files with ranges from CSV
                summary = f"{files_with_ranges} file(s) with ranges, {len(doc_files) - files_with_ranges} full"
                if pages_csv:
                    try:
                        csv_rel = Path(pages_csv).relative_to(Path.cwd())
                        config_info["Pages"] = f"{csv_rel} ({summary})"
                    except ValueError:
                        config_info["Pages"] = f"{pages_csv} ({summary})"
                else:
                    config_info["Pages"] = summary
            else:
                config_info["Pages"] = "All pages"
        else:
            config_info["Pages"] = "All pages"

        # Show relative path for output
        try:
            rel_output = output_dir.relative_to(Path.cwd())
            config_info["Output"] = str(rel_output)
        except ValueError:
            config_info["Output"] = str(output_dir)

    # Load schema (required via CLI or runtime config). A domain pack
    # (``pack.yaml``) may be referenced instead of a JSON schema — resolve it to
    # the schema it declares so configs can point at packs (which also carry
    # QA/QC lanes) rather than duplicating the schema path.
    schema_path = _resolve_schema_ref(Path(schema))
    if not schema_path.exists():
        print_error("Schema not found", schema_path.as_posix())
        sys.exit(1)
    loaded_schema = load_schema(schema_path)
    runtime_artifact = _resolve_runtime_artifact(
        category, schema_path, profile_name=profile_name
    )
    if not get_verbosity().is_quiet:
        # Show relative path for clarity
        try:
            schema_rel = schema_path.relative_to(Path.cwd())
            config_info["Schema"] = str(schema_rel)
        except ValueError:
            config_info["Schema"] = str(schema_path)

        if runtime_artifact:
            config_info["Artifact"] = runtime_artifact["artifact_id"]
            config_info["Profile"] = runtime_artifact["lineage"]["profile_id"]
        else:
            config_info["Profile"] = profile_name

    # Display configuration table
    if not view.is_quiet:
        view.config(config_info)

    # Cost estimation and confirmation for large batches
    if len(doc_files) > 10 and not view.is_quiet:
        # Quick estimation
        sample_size = min(3, len(doc_files))
        total_chars = 0
        for doc in doc_files[:sample_size]:
            try:
                # Use page range for estimation if specified
                page_range = page_range_map.get(doc)
                text = extract_text_from_document(doc, page_range=page_range)
                total_chars += len(text)
            except Exception:
                pass

        if total_chars > 0:
            avg_chars = total_chars / sample_size
            estimated_total_chars = avg_chars * len(doc_files)
            estimated_tokens = int(estimated_total_chars / 4)

            # Rough cost estimate using the ACTUAL selected model's rates (from
            # the shared pricing DB), so the confirmation gate is accurate for
            # any provider/model — not just the default. Output is estimated at
            # ~10% of input tokens.
            from psweep.extraction.llm_factory import (
                resolve_model_name,
            )
            from psweep.utils.model_pricing import (
                get_model_pricing,
            )

            est_model = resolve_model_name(
                resolved_inputs.get("model"),
                models=resolved_inputs.get("models"),
                llm_config=config.llm_config,
            )
            input_rate, output_rate = get_model_pricing(est_model)
            input_cost = (estimated_tokens / 1_000_000) * input_rate
            output_cost = (estimated_tokens * 0.1 / 1_000_000) * output_rate
            total_est_cost = input_cost + output_cost

            if total_est_cost > 1.0:  # Threshold for confirmation
                view.warning(
                    f"Estimated cost: ${total_est_cost:.2f}",
                    f"Processing {len(doc_files)} documents "
                    f"with ~{estimated_tokens:,} tokens",
                )
                if not ask_confirm("Proceed with extraction?", default=True):
                    view.info("Operation cancelled")
                    return

    # Load configuration
    config = get_config()

    # Determine provider from CLI flag or auto-detect from environment
    if provider == "auto":
        provider = config.llm_config.get("provider", "openai")

    # Get API credentials
    api_key = config.llm_config.get("api_key")
    if not api_key:
        print_error(
            f"{provider.upper()} API key not found",
            f"Configure {provider.upper()}_API_KEY in .env file",
        )
        return

    # Track the actual model being used for output. When a model is explicitly
    # configured (run.yaml processing.model or --model), route it through the
    # shared tiering resolver so it is honored for ALL providers (the legacy
    # branch below silently discarded processing.model for non-OpenAI). When no
    # model is specified, preserve the exact legacy env-driven behavior.
    if "model" in resolved_inputs:
        from psweep.extraction.llm_factory import (
            resolve_llm_kwargs,
        )

        _mk = resolve_llm_kwargs(
            resolved_inputs["model"],
            models=resolved_inputs.get("models"),
            llm_config=config.llm_config,
        )
        actual_model = _mk["model"]
        api_key = _mk["api_key"] or api_key
        provider = _mk["provider"] or provider
        azure_endpoint = _mk["azure_endpoint"]
        azure_api_version = _mk["azure_api_version"]
    else:
        actual_model = (
            config.llm_config.get("model", model)
            if provider != "openai"
            else model
        )
        azure_endpoint = config.llm_config.get("azure_endpoint")
        azure_api_version = config.llm_config.get("azure_api_version")
    run_id = _generate_run_id(
        schema_path=schema_path,
        provider=provider,
        model=actual_model,
        enable_qa_qc=enable_qa_qc,
        doc_files=doc_files,
        artifact_id=runtime_artifact.get("artifact_id")
        if runtime_artifact
        else None,
    )
    run_started_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # ========================================================================
    # QA/QC Multi-Model Extraction Mode
    # ========================================================================
    if enable_qa_qc:
        _run_qa_qc_extraction(
            doc_files=doc_files,
            loaded_schema=loaded_schema,
            schema_path=schema_path,
            output_dir=output_dir,
            api_key=api_key,
            provider=provider,
            config=config,
            max_context=max_context,
            page_range_map=page_range_map,
            verbosity=get_verbosity().value,
            runtime_artifact=runtime_artifact,
            run_id=run_id,
            qaqc_lane=qaqc_lane,
        )
        return

    # ========================================================================
    # Normal Single-Model Extraction Mode
    # ========================================================================

    # Create schema metadata if available
    schema_metadata = None
    if schema_path:
        try:
            from psweep.utils.schema_metadata import SchemaMetadata

            schema_metadata = SchemaMetadata(schema_path)
        except Exception as e:
            if get_verbosity() is Verbosity.VERBOSE:
                console.print(
                    f"[dim yellow]Could not load schema metadata: {e}[/dim yellow]"
                )

    # Identifier fields for the saved record are schema-driven (no domain terms
    # baked into code). When the schema declares extraction.identifier_fields,
    # use their leaf names; otherwise fall back to a neutral default list.
    identifier_fields = None
    if schema_metadata is not None:
        try:
            declared = schema_metadata.get_identifier_fields() or []
            identifier_fields = [p.split(".")[-1] for p in declared] or None
        except Exception:
            identifier_fields = None

    # Optional per-model context-window guard (fail fast before an over-budget
    # request). Sourced from the top-level ``model_context_windows`` config block;
    # no deployment names are hardcoded.
    context_windows = resolved_inputs.get("model_context_windows") or None

    # Initialize extractor with clean configuration
    extractor = DocumentExtractor(
        api_key=api_key,
        model=actual_model,
        max_context_chars=max_context,
        schema_metadata=schema_metadata,
        provider=provider,
        azure_endpoint=azure_endpoint,
        azure_api_version=azure_api_version,
        context_windows=context_windows,
    )

    # Processing section
    view.phase("Extracting documents")

    # Provenance: map each document to its discover origin URL + queried target
    # so extraction can cite the source and stay anchored to the intended
    # project (guards against extracting dates from unrelated page content).
    source_context_map = _build_source_context_map(
        Path(path) if path else None, from_index
    )
    if source_context_map and not view.is_quiet:
        matched = sum(1 for d in doc_files if d.name in source_context_map)
        view.status(
            "info",
            f"Source-context: matched {matched}/{len(doc_files)} "
            "document(s) to discover provenance",
        )

    results = []
    total_cost = 0.0
    total_time = 0.0

    # One place does the actual extraction+save for a document; the three
    # branches below differ only in how they report progress.
    def _run(doc_path: Path) -> dict:
        return _extract_one_document(
            doc_path,
            extractor=extractor,
            loaded_schema=loaded_schema,
            page_range_map=page_range_map,
            source_context_map=source_context_map,
            file_output_dirs=file_output_dirs,
            output_dir=output_dir,
            category=category,
            actual_model=actual_model,
            enable_qa_qc=enable_qa_qc,
            runtime_artifact=runtime_artifact,
            run_id=run_id,
            provider=provider,
            schema_path=schema_path,
            identifier_fields=identifier_fields,
        )

    # Use live dashboard for multiple files if requested
    if len(doc_files) > 3 and live_dashboard and not view.is_quiet:
        live, dashboard = create_live_dashboard(len(doc_files), actual_model)

        with live:
            for doc_path in doc_files:
                dashboard.start_document(doc_path.name)
                res = _run(doc_path)
                results.append(res)
                if res["success"]:
                    dashboard.complete_document(
                        res["file"],
                        success=True,
                        cost=res["cost"],
                        input_tokens=res.get("input_tokens") or 0,
                        output_tokens=res.get("output_tokens") or 0,
                    )
                    total_cost += res["cost"]
                    total_time += res["time"]
                else:
                    dashboard.complete_document(res["file"], success=False)

    # Use progress bar for multiple files, simple output for single file
    elif len(doc_files) > 1 and not view.is_quiet:
        progress = create_extraction_progress()
        # Start with first document name instead of generic "Extracting..." message
        task = progress.add_task(
            _document_progress_desc(doc_files[0], page_range_map),
            total=len(doc_files),
        )

        with progress:
            for idx, doc_path in enumerate(doc_files):
                # Update description to the current document (first is already set)
                if idx > 0:
                    progress.update(
                        task,
                        description=_document_progress_desc(
                            doc_path, page_range_map
                        ),
                    )

                res = _run(doc_path)
                results.append(res)
                if res["success"]:
                    total_cost += res["cost"]
                    total_time += res["time"]

                progress.update(task, advance=1)
    else:
        # Single file or quiet mode
        for doc_path in doc_files:
            if not view.is_quiet:
                page_range = page_range_map.get(doc_path)
                detail = None
                if page_range is not None:
                    start, end = page_range
                    detail = f"pages {start}-{end}"
                view.status("info", doc_path.name, detail)

            res = _run(doc_path)
            results.append(res)
            if res["success"]:
                total_cost += res["cost"]
                total_time += res["time"]
                view.status(
                    "success",
                    f"{res['items']} items",
                    f"${res['cost']:.4f} • {res['time']:.1f}s",
                )
            elif not view.is_quiet:
                view.status("error", "Extraction failed", res["error"][:60])

    run_finished_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    successful_output_paths = [
        Path(r["output_path"]) for r in successful if r.get("output_path")
    ]
    try:
        run_manifest = _build_run_manifest(
            run_id=run_id,
            mode="single_model",
            schema_path=schema_path,
            provider=provider,
            model=actual_model,
            runtime_artifact=runtime_artifact,
            doc_files=doc_files,
            successful_output_paths=successful_output_paths,
            started_at=run_started_at,
            finished_at=run_finished_at,
            total_processed=len(results),
            successful_count=len(successful),
            failed_count=len(failed),
            failed_results=failed,
        )
        manifest_path = _write_run_manifest(output_dir, run_id, run_manifest)
        if view.verbosity.shows_detail:
            view.status("info", f"Run manifest: {manifest_path.as_posix()}")
    except Exception as exc:
        view.warning(f"Run manifest write failed: {exc}")

    # Summary
    if not view.is_quiet:
        summary_stats = {
            "Extracted": f"{len(results)} file{'s' if len(results) != 1 else ''}",
            "Successful": str(len(successful)),
        }
        if failed:
            summary_stats["Failed"] = str(len(failed))
        if successful:
            avg_cost = total_cost / len(successful)
            avg_time = total_time / len(successful)
            summary_stats["Total Cost"] = f"${total_cost:.4f}"
            summary_stats["Avg Cost/File"] = f"${avg_cost:.4f}"
            summary_stats["Total Time"] = f"{total_time:.1f}s"
            summary_stats["Avg Time/File"] = f"{avg_time:.1f}s"
            total_items = sum(r.get("items", 0) or 0 for r in successful)
            if total_items > 0:
                summary_stats["Total Items"] = str(total_items)

        view.summary(summary_stats, title="Extraction Summary")

        for failure in [f for f in failed if f.get("suggestions")][:3]:
            view.error(
                f"Processing failed for {failure['file']}",
                failure.get("error"),
                failure.get("suggestions"),
            )

        view.outputs({"Extracted data": str(output_dir.absolute())})
        view.next_steps(
            [
                f"Inspect a result: pixi run psweep check {output_dir}/<name>.json --show-data",
                f"Compile into a spreadsheet: pixi run psweep compile {output_dir} --schema {schema}",
            ]
        )


def _run_qa_qc_extraction(
    doc_files: List[Path],
    loaded_schema: dict,
    schema_path: Path,
    output_dir: Path,
    api_key: str,
    provider: str,
    config,
    max_context: int,
    page_range_map: dict,
    verbosity: str,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    qaqc_lane: Optional[str] = None,
) -> None:
    """
    Run QA/QC multi-model extraction for documents.

    This function handles the --enable-qa-qc flag by:
    1. Auto-detecting models from environment
    2. Confirming with user (Nx cost warning)
    3. Running extraction with multiple models
    4. Saving outputs to extracted/qa_qc/{doc_name}/
    """
    from psweep.qa_qc import (
        ModelDetector,
        run_multi_model_extraction,
    )
    from psweep.extraction.document_utils import (
        extract_text_from_document,
    )

    # Get QA/QC models from environment
    try:
        qa_models = ModelDetector.get_qa_models()
        qa_provider = ModelDetector.get_provider()
    except ValueError as e:
        print_error(
            "QA/QC Configuration Error",
            str(e),
            [
                "Set QAQC_MODELS in .env with 2+ comma-separated models",
                "Example: QAQC_MODELS=gpt-4o,gpt-4-turbo,gpt-3.5-turbo",
                "Or leave empty to use default models for your provider",
            ],
        )
        return

    view = RunView("extract", verbosity=Verbosity(verbosity))

    # Show QA/QC configuration
    view.header("QA/QC MULTI-MODEL VALIDATION")
    view.config(
        {
            "Provider": qa_provider.upper(),
            "Models": ", ".join(qa_models),
            "Documents": str(len(doc_files)),
            "QA/QC Lane": qaqc_lane or "default",
        }
    )
    if not view.is_quiet:
        view.warning(
            f"This will run {len(qa_models)}x extractions per document",
            f"Total API calls: {len(doc_files)} docs × {len(qa_models)} models "
            f"= {len(doc_files) * len(qa_models)} extractions",
        )
        if not ask_confirm("Proceed with QA/QC extraction?", default=True):
            view.info("Operation cancelled")
            return

    # Process each document with multi-model extraction
    results = []
    total_cost = 0.0
    total_time = 0.0

    view.phase("Extracting with multiple models")

    for doc_idx, doc_path in enumerate(doc_files, 1):
        if not view.is_quiet:
            view.status("info", f"[{doc_idx}/{len(doc_files)}] {doc_path.name}")

        try:
            # Extract text from document (respecting page ranges)
            page_range = page_range_map.get(doc_path)
            text = extract_text_from_document(doc_path, page_range=page_range)

            if view.verbosity.shows_detail:
                view.status("info", f"Extracted {len(text):,} characters")

            # Run multi-model extraction
            model_results = run_multi_model_extraction(
                doc_text=text,
                doc_name=doc_path.stem,
                schema=loaded_schema,
                models=qa_models,
                output_dir=output_dir,
                api_key=api_key,
                provider=qa_provider,
                azure_endpoint=config.llm_config.get("azure_endpoint"),
                azure_api_version=config.llm_config.get("azure_api_version"),
                max_context_chars=max_context,
                runtime_artifact=runtime_artifact,
                run_id=run_id,
            )

            # Calculate totals for this document
            doc_cost = sum(r.cost for r in model_results.values())
            doc_time = sum(r.processing_time for r in model_results.values())
            successful = sum(1 for r in model_results.values() if r.success)

            total_cost += doc_cost
            total_time += doc_time

            results.append(
                {
                    "file": doc_path.name,
                    "success": True,
                    "models_successful": successful,
                    "models_total": len(qa_models),
                    "cost": doc_cost,
                    "time": doc_time,
                }
            )

            level = "success" if successful == len(qa_models) else "warning"
            view.status(
                level,
                f"{successful}/{len(qa_models)} models",
                f"${doc_cost:.4f} • {doc_time:.1f}s",
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

    # Summary
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
        summary_stats["Total API Calls"] = str(
            len(successful_docs) * len(qa_models)
        )
        summary_stats["Total Cost"] = f"${total_cost:.4f}"
        summary_stats["Total Time"] = f"{total_time:.1f}s"

        view.summary(summary_stats, title="QA/QC Extraction Summary")

        qa_qc_output = output_dir / "qa_qc"
        view.outputs({"QA/QC outputs": str(qa_qc_output.absolute())})

        compare_command = (
            "pixi run psweep compare "
            f"{qa_qc_output.as_posix()} --schema {schema_path.as_posix()}"
        )
        if qaqc_lane:
            compare_command += f" --qaqc-lane {qaqc_lane}"
        view.next_steps([f"Run the comparison workflow: {compare_command}"])


def _extract_and_save_result(
    doc_path: Path,
    result,
    output_dir: Path,
    category: str,
    model: str,
    qa_qc_enabled: bool,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    provider: Optional[str] = None,
    schema_id: Optional[str] = None,
    identifier_fields: Optional[List[str]] = None,
) -> int:
    """Helper to extract items count and save result to JSON."""
    # Universal schema detection - find main array and identifier dynamically.
    # Identifier field names are domain-neutral: schema-supplied when the schema
    # declares extraction.identifier_fields, else a generic default. No domain
    # terms (e.g. "jurisdiction") are hardcoded here.
    num_items = 0
    identifier = "N/A"
    id_field_names = identifier_fields or ["id", "identifier", "number", "name"]

    # Find main array field (the one with the most data)
    main_array_key = None
    max_items = 0
    for key, value in result.data.items():
        if isinstance(value, list) and value:
            if len(value) > max_items:
                max_items = len(value)
                main_array_key = key

    if main_array_key:
        main_array = result.data.get(main_array_key, [])
        num_items = len(main_array)

    # Find identifier field dynamically
    for key, value in result.data.items():
        if isinstance(value, dict):
            for id_field in id_field_names:
                if id_field in value:
                    id_val = value[id_field]
                    if isinstance(id_val, dict):
                        parts = [str(v) for v in id_val.values() if v]
                        identifier = "-".join(parts) if parts else "N/A"
                    elif id_val:
                        identifier = str(id_val)
                    break
        elif (
            isinstance(value, (str, int))
            and value
            and key.lower() in id_field_names
        ):
            identifier = str(value)

    extracted_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    lineage = {
        "artifact_id": (runtime_artifact or {}).get("artifact_id")
        or "artifact://runtime/unresolved",
        "profile_id": ((runtime_artifact or {}).get("lineage") or {}).get(
            "profile_id"
        )
        or "default",
        "run_id": run_id or f"run://{doc_path.stem}",
        "model": model,
        "provider": provider or "unknown",
        "schema_id": schema_id,
        "extracted_at": extracted_at,
    }

    output_data = {
        "record_id": f"record://{lineage['run_id'].replace('run://', '')}/{doc_path.stem}",
        "contract_version": "1.0.0",
        "document": {
            "source_document_id": identifier
            if identifier != "N/A"
            else doc_path.stem,
            "source_path": doc_path.as_posix(),
            "source_filename": doc_path.name,
        },
        "lineage": lineage,
        "payload": result.data,
        "quality": {
            "overall_confidence": result.completeness_score,
            "warnings": result.validation_notes or [],
            "errors": normalize_error_records(
                getattr(result, "processing_errors", None)
                or getattr(result, "errors", None)
            ),
        },
        "processing_metrics": {
            "duration_seconds": result.processing_time,
            "cost_usd": result.cost,
            "input_tokens": getattr(result, "input_tokens", None),
            "output_tokens": getattr(result, "output_tokens", None),
        },
    }

    output_file = output_dir / f"{doc_path.stem}.json"
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    return num_items


def _resolve_compilation_output_formats(
    metadata_overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Resolve which compilation outputs to emit.

    Runtime pack overrides are the only source that currently changes output
    selection. Schema-owned `default_format` remains non-authoritative so
    existing domains keep emitting both files until they explicitly opt in at
    the runtime-pack layer.
    """
    output_config = (
        (metadata_overrides or {}).get("compilation") or {}
    ).get("output") or {}
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
@click.argument("extraction_file", type=click.Path(exists=True))
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option(
    "--show-data",
    is_flag=True,
    help="Display extracted data with syntax highlighting",
)
def check(extraction_file: str, verbose: bool, show_data: bool):
    """
    Check an extraction result against the schema.

    \b
    EXAMPLES:
        psweep check extracted/data/doc.json
        psweep check extracted/data/doc.json --show-data
    """
    from psweep.cli.ui import display_json

    extraction_file = Path(extraction_file)
    view = begin_run("check", verbose=verbose)

    view.header(f"Validation: {extraction_file.name}")

    # Load extraction
    try:
        with open(extraction_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        view.error("Invalid JSON file", str(e))
        return

    # Load schema
    config = get_config()
    schema = load_schema(config.default_schema)

    # Validate
    from jsonschema import validate as json_validate, ValidationError

    try:
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError(
                "Extraction file must use canonical extraction-record format with a 'payload' object"
            )
        json_validate(instance=payload, schema=schema)
        view.status("success", "Schema validation passed")
    except ValidationError as e:
        view.error("Schema validation failed", e.message)
        sys.exit(1)

    # Display metadata
    if verbose or show_data:
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if "item_count" in data:
            item_count = data.get("item_count", 0)
        else:
            item_count = 0
            for value in payload.values():
                if isinstance(value, list):
                    item_count = max(item_count, len(value))

        metadata = {
            "Source File": data.get("document", {}).get(
                "source_filename", "N/A"
            ),
            "Extraction Date": data.get("lineage", {}).get(
                "extracted_at", "N/A"
            ),
            "Model": data.get("lineage", {}).get("model", "N/A"),
            "Cost": f"${data.get('processing_metrics', {}).get('cost_usd', 0):.4f}",
            "Processing Time": f"{data.get('processing_metrics', {}).get('duration_seconds', 0):.1f}s",
            "Item Count": str(item_count),
        }

        view.summary(metadata, title="Extraction Metadata")

    # Show extracted data with syntax highlighting
    if show_data and "payload" in data:
        console.print()
        from psweep.cli.ui import display_json

        display_json(data.get("payload"), title="Extracted Data")

    view.next_steps(
        [
            f"Compile the folder: pixi run psweep compile "
            f"{extraction_file.parent} --schema <schema>",
        ]
    )


@click.command()
@click.argument("target", required=False)
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
    help="Validate resolved command inputs and exit without running discovery",
)
@click.option(
    "--config-strict",
    is_flag=True,
    help="Fail on unknown keys in runtime config sections",
)
@click.option(
    "--domain",
    type=str,
    default=None,
    help="Domain key for discovery output structure",
)
@click.option(
    "--seed-url",
    "seed_urls",
    multiple=True,
    help="Seed URL for discovery (repeatable)",
)
@click.option(
    "--query",
    type=str,
    default=None,
    help="Discovery query hint for seeker stage",
)
@click.option(
    "--state",
    type=str,
    default=None,
    help="Optional state hint used for jurisdiction-based output organization (for example: CA, Colorado)",
)
@click.option(
    "--jurisdiction",
    type=str,
    default=None,
    help="Optional jurisdiction hint used for output organization (for example: Imperial County)",
)
@click.option(
    "--partition-mode",
    type=click.Choice(["auto", "jurisdiction", "host"], case_sensitive=False),
    default=None,
    help="Download organization mode: auto prefers jurisdiction when available, else host",
)
@click.option(
    "--digger-provider",
    type=str,
    default=None,
    help="Digger provider (for example: seed_only, http, crawlee_playwright)",
)
@click.option(
    "--enable-serpapi/--disable-serpapi",
    default=None,
    help="Enable optional SerpApi seeker provider",
)
@click.option(
    "--max-concurrent-downloads",
    type=int,
    default=None,
    help="Maximum parallel downloads during discover runs",
)
@click.option(
    "--min-request-interval-ms",
    type=int,
    default=None,
    help="Minimum delay between outbound discovery requests in milliseconds",
)
@click.option(
    "--robots-policy-mode",
    type=click.Choice(["ignore", "warn", "enforce"], case_sensitive=False),
    default=None,
    help="Robots policy mode for target-site requests",
)
@click.option(
    "--tos-policy-mode",
    type=click.Choice(["ignore", "warn", "enforce"], case_sensitive=False),
    default=None,
    help="Terms acknowledgement mode for target-site requests",
)
@click.option(
    "--acknowledge-tos-domain",
    "acknowledged_tos_domains",
    multiple=True,
    help="Host or parent domain acknowledged for target-site terms checks (repeatable)",
)
@click.option(
    "--output-documents",
    type=click.Path(),
    default=None,
    help="Directory for discovered documents (defaults to run-scoped deterministic path)",
)
@click.option(
    "--output-manifest",
    type=click.Path(),
    default=None,
    help="Path to discovery manifest JSON (defaults to run-scoped deterministic path)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover and emit manifest scaffold without downloads",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
@click.option(
    "--debug", is_flag=True, help="Debug output with diagnostic context"
)
def discover(
    target: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    domain: Optional[str],
    seed_urls: tuple[str, ...],
    query: Optional[str],
    state: Optional[str],
    jurisdiction: Optional[str],
    partition_mode: Optional[str],
    digger_provider: Optional[str],
    enable_serpapi: Optional[bool],
    max_concurrent_downloads: Optional[int],
    min_request_interval_ms: Optional[int],
    robots_policy_mode: Optional[str],
    tos_policy_mode: Optional[str],
    acknowledged_tos_domains: tuple[str, ...],
    output_documents: Optional[str],
    output_manifest: Optional[str],
    dry_run: bool,
    quiet: bool,
    verbose: bool,
    debug: bool,
):
    """Discover and download source documents from web targets."""
    view = begin_run("discover", quiet=quiet, verbose=verbose, debug=debug)

    cli_overrides = _explicit_cli_overrides(
        [
            "domain",
            "seed_urls",
            "query",
            "state",
            "jurisdiction",
            "partition_mode",
            "digger_provider",
            "enable_serpapi",
            "max_concurrent_downloads",
            "min_request_interval_ms",
            "robots_policy_mode",
            "tos_policy_mode",
            "acknowledged_tos_domains",
            "output_documents",
            "output_manifest",
            "dry_run",
        ]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="discover",
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
        _print_effective_config("discover", resolved_inputs)
        console.print()

    if validate_config_only:
        if get_verbosity().is_quiet:
            click.echo(
                json.dumps(
                    {
                        "command": "discover",
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
            print_success(
                "Runtime config validation passed for discover command"
            )
        return

    resolved_domain = (
        resolved_inputs.get("domain") or domain or target or "default"
    )
    resolved_seed_urls = list(resolved_inputs.get("seed_urls") or seed_urls)
    resolved_query = resolved_inputs.get("query", query)
    resolved_state = resolved_inputs.get("state", state)
    resolved_jurisdiction = resolved_inputs.get("jurisdiction", jurisdiction)
    resolved_partition_mode = (
        resolved_inputs.get("partition_mode", partition_mode) or "auto"
    ).lower()
    resolved_digger_provider = (
        (
            resolved_inputs.get("digger_provider", digger_provider)
            or "seed_only"
        )
        .strip()
        .lower()
    )
    resolved_topology_mode = resolved_inputs.get("topology_mode")
    resolved_enable_serpapi = bool(
        resolved_inputs.get("enable_serpapi", enable_serpapi or False)
    )
    resolved_hub_pages = resolved_inputs.get("hub_pages") or None
    resolved_allowed_domains = resolved_inputs.get("allowed_domains") or None
    resolved_targets = resolved_inputs.get("targets") or None
    resolved_query_templates = resolved_inputs.get("query_templates") or None
    resolved_query_families = resolved_inputs.get("query_families") or None
    resolved_use_query_family = resolved_inputs.get("use_query_family")
    resolved_seeker_max_results = int(
        resolved_inputs.get("seeker_max_results", 10) or 10
    )
    resolved_link_prioritization_mode = str(
        resolved_inputs.get("link_prioritization_mode", "heuristic")
        or "heuristic"
    ).lower()
    # 0 = no global cap; per-target selection controls recall.
    resolved_link_top_k = int(resolved_inputs.get("link_top_k", 0) or 0)
    resolved_link_prioritization_keywords = (
        resolved_inputs.get("link_prioritization_keywords") or None
    )
    resolved_link_prioritization_domain_scores = (
        resolved_inputs.get("link_prioritization_domain_scores") or None
    )
    resolved_power_range_kw = resolved_inputs.get("power_range_kw") or None
    resolved_selection_primary_per_target = int(
        resolved_inputs.get("selection_primary_per_target", 1) or 1
    )
    resolved_selection_exclude_draft = bool(
        resolved_inputs.get("selection_exclude_draft", True)
    )
    resolved_selection_draft_patterns = (
        resolved_inputs.get("selection_draft_patterns") or None
    )
    resolved_selection_relevance_require_any_terms = (
        resolved_inputs.get("selection_relevance_require_any_terms") or None
    )
    resolved_selection_relevance_require_legal_marker_terms = (
        resolved_inputs.get("selection_relevance_require_legal_marker_terms")
        or None
    )
    resolved_selection_relevance_exclude_any_terms = (
        resolved_inputs.get("selection_relevance_exclude_any_terms") or None
    )
    resolved_selection_relevance_allowed_domain_patterns = (
        resolved_inputs.get("selection_relevance_allowed_domain_patterns")
        or None
    )
    resolved_selection_require_supported_document = bool(
        resolved_inputs.get("selection_require_supported_document", True)
    )
    resolved_selection_target_identity_require_any_templates = (
        resolved_inputs.get("selection_target_identity_require_any_templates")
        or None
    )
    resolved_selection_target_identity_require_all_templates = (
        resolved_inputs.get("selection_target_identity_require_all_templates")
        or None
    )
    resolved_selection_target_identity_exclude_any_templates = (
        resolved_inputs.get("selection_target_identity_exclude_any_templates")
        or None
    )
    resolved_include_url_patterns = (
        resolved_inputs.get("include_url_patterns") or None
    )
    resolved_include_link_text_patterns = (
        resolved_inputs.get("include_link_text_patterns") or None
    )
    resolved_index_page_mode = resolved_inputs.get("index_page_mode") or None
    resolved_index_links = resolved_inputs.get("index_links") or None
    resolved_max_depth = resolved_inputs.get("max_depth")
    resolved_max_pages = resolved_inputs.get("max_pages")
    resolved_max_files = resolved_inputs.get("max_files")
    resolved_timeout_seconds = resolved_inputs.get("timeout_seconds")
    resolved_retry_max_attempts = int(
        resolved_inputs.get("retry_max_attempts", 3) or 3
    )
    resolved_retry_initial_backoff_seconds = float(
        resolved_inputs.get("retry_initial_backoff_seconds", 1.0) or 1.0
    )
    resolved_retry_max_backoff_seconds = float(
        resolved_inputs.get("retry_max_backoff_seconds", 8.0) or 8.0
    )
    resolved_max_concurrent_downloads = int(
        resolved_inputs.get(
            "max_concurrent_downloads", max_concurrent_downloads or 2
        )
        or 2
    )
    resolved_min_request_interval_ms = int(
        resolved_inputs.get(
            "min_request_interval_ms", min_request_interval_ms or 0
        )
        or 0
    )
    resolved_robots_policy_mode = str(
        resolved_inputs.get(
            "robots_policy_mode", robots_policy_mode or "ignore"
        )
        or "ignore"
    ).lower()
    resolved_tos_policy_mode = str(
        resolved_inputs.get("tos_policy_mode", tos_policy_mode or "ignore")
        or "ignore"
    ).lower()
    resolved_acknowledged_tos_domains = list(
        resolved_inputs.get("acknowledged_tos_domains")
        or acknowledged_tos_domains
        or []
    )
    resolved_document_classifier = (
        resolved_inputs.get("document_classifier") or None
    )
    resolved_document_review = (
        resolved_inputs.get("document_review") or None
    )
    resolved_models = resolved_inputs.get("models") or None
    resolved_seeker_cache = bool(resolved_inputs.get("seeker_cache") or False)
    resolved_seeker_cache_ttl_minutes = float(
        resolved_inputs.get("seeker_cache_ttl_minutes", 0) or 0
    )
    resolved_query_context_aliases = (
        resolved_inputs.get("query_context_aliases") or None
    )
    resolved_partition_by = resolved_inputs.get("partition_by") or None
    resolved_browser_mode = bool(resolved_inputs.get("browser_mode") or False)
    _raw_seeker_extra = resolved_inputs.get("seeker_extra_params")
    resolved_seeker_extra_params = (
        dict(_raw_seeker_extra) if isinstance(_raw_seeker_extra, dict) else None
    )

    if not resolved_seed_urls and not resolved_query and not resolved_targets:
        print_error(
            "Discovery input missing",
            "Provide at least one --seed-url, --query, or discovery.targets entry in config.",
        )
        sys.exit(1)

    resolved_output_documents = (
        resolved_inputs.get("output_documents") or output_documents
    )
    resolved_output_manifest = (
        resolved_inputs.get("output_manifest") or output_manifest
    )

    documents_dir = (
        Path(resolved_output_documents) if resolved_output_documents else None
    )
    manifest_path = (
        Path(resolved_output_manifest) if resolved_output_manifest else None
    )

    if not view.is_quiet:
        if view.verbosity.shows_detail:
            config_info = {
                "Domain": resolved_domain,
                "Seeds": str(len(resolved_seed_urls)),
                "Query": resolved_query or "(none)",
                "State": resolved_state or "(none)",
                "Jurisdiction": resolved_jurisdiction or "(none)",
                "Partition Mode": resolved_partition_mode,
                "Digger Provider": resolved_digger_provider,
                "Topology": resolved_topology_mode or "(default)",
                "Hub Pages": str(len(resolved_hub_pages or [])),
                "Targets": str(len(resolved_targets or [])),
                "Seeker": "serpapi"
                if resolved_enable_serpapi
                else "seed-only",
                "Max Concurrent Downloads": str(
                    max(1, resolved_max_concurrent_downloads)
                ),
                "Min Request Interval (ms)": str(
                    max(0, resolved_min_request_interval_ms)
                ),
                "Robots Policy": resolved_robots_policy_mode,
                "ToS Policy": resolved_tos_policy_mode,
                "Acknowledged ToS Domains": str(
                    len(resolved_acknowledged_tos_domains)
                ),
                "Documents Output": str(documents_dir)
                if documents_dir
                else "(auto: run-scoped)",
                "Manifest": str(manifest_path)
                if manifest_path
                else "(auto: run-scoped)",
                "Mode": "dry-run" if dry_run else "run",
            }
        else:
            config_info = {
                "Domain": resolved_domain,
                "Input": f"seeds={len(resolved_seed_urls)}, targets={len(resolved_targets or [])}, query={'yes' if resolved_query else 'no'}",
                "Seeker": "serpapi"
                if resolved_enable_serpapi
                else "seed-only",
                "Topology": resolved_topology_mode or "(default)",
                "Documents Output": str(documents_dir)
                if documents_dir
                else "(auto: run-scoped)",
                "Mode": "dry-run" if dry_run else "run",
            }
        view.header("DISCOVERY")
        view.config(config_info)

        if resolved_targets:
            view.info(
                f"Preparing discovery plan for {len(resolved_targets)} target(s)..."
            )
            if view.verbosity.shows_detail:
                preview_limit = 8
                for idx, target_meta in enumerate(
                    resolved_targets[:preview_limit], start=1
                ):
                    label = DiscoveryEngine._target_label(
                        target_meta if isinstance(target_meta, dict) else None, idx
                    )
                    query_preview = (
                        (target_meta.get("query") if isinstance(target_meta, dict) else None)
                        or resolved_query
                        or "(query templates)"
                    )
                    view.status("info", f"{idx}. {label}", f"query: {query_preview}")
                if len(resolved_targets) > preview_limit:
                    remaining = len(resolved_targets) - preview_limit
                    view.status("info", f"... and {remaining} more target(s)")
            console.print()

    # Keep urllib3 TLS warnings out of the CLI output and surface a single
    # styled warning instead so the run view remains cohesive.
    try:
        from urllib3.exceptions import InsecureRequestWarning

        std_warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    except Exception:
        pass

    def _env_ssl_verify(env_var: str, fallback_var: str) -> bool:
        raw = os.getenv(env_var) or os.getenv(fallback_var) or "false"
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    seeker_ssl_verify = _env_ssl_verify(
        "SERPAPI_SSL_VERIFY", "PSWEEP_SSL_VERIFY"
    )
    download_ssl_verify = _env_ssl_verify(
        "DISCOVERY_SSL_VERIFY", "PSWEEP_SSL_VERIFY"
    )

    if resolved_enable_serpapi and not seeker_ssl_verify:
        view.warning(
            "SerpApi TLS verification is disabled (SERPAPI_SSL_VERIFY=false).",
            "Set SERPAPI_SSL_VERIFY=true to suppress insecure-request warnings.",
        )
    if not dry_run and not download_ssl_verify:
        view.warning(
            "Download TLS verification is disabled (DISCOVERY_SSL_VERIFY=false)."
        )

    discover_live = None
    discover_dashboard = None
    if view.verbosity in {Verbosity.NORMAL, Verbosity.VERBOSE} and not dry_run:
        discover_live, discover_dashboard = create_discovery_live_dashboard(
            domain=resolved_domain,
            mode="dry-run" if dry_run else "run",
            total_targets=len(resolved_targets or []),
            seeker_enabled=resolved_enable_serpapi,
        )

    def _discover_progress(message: str) -> None:
        if view.is_quiet:
            return
        if discover_dashboard is not None:
            discover_dashboard.push_event(message)
            return
        print_info(message)

    request = DiscoveryRequest(
        domain=resolved_domain,
        seed_urls=resolved_seed_urls,
        query=resolved_query,
        enable_serpapi=resolved_enable_serpapi,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=dry_run,
        state=resolved_state,
        jurisdiction=resolved_jurisdiction,
        partition_mode=resolved_partition_mode,
        digger_provider=resolved_digger_provider,
        topology_mode=resolved_topology_mode,
        hub_pages=resolved_hub_pages,
        allowed_domains=resolved_allowed_domains,
        targets=resolved_targets,
        query_templates=resolved_query_templates,
        query_families=resolved_query_families,
        use_query_family=resolved_use_query_family,
        seeker_max_results=max(1, resolved_seeker_max_results),
        link_prioritization_mode=resolved_link_prioritization_mode,
        link_top_k=max(0, resolved_link_top_k),
        link_prioritization_keywords=resolved_link_prioritization_keywords,
        link_prioritization_domain_scores=resolved_link_prioritization_domain_scores,
        power_range_kw=resolved_power_range_kw,
        selection_primary_per_target=max(
            1, resolved_selection_primary_per_target
        ),
        selection_exclude_draft=resolved_selection_exclude_draft,
        selection_draft_patterns=resolved_selection_draft_patterns,
        selection_relevance_require_any_terms=resolved_selection_relevance_require_any_terms,
        selection_relevance_require_legal_marker_terms=resolved_selection_relevance_require_legal_marker_terms,
        selection_relevance_exclude_any_terms=resolved_selection_relevance_exclude_any_terms,
        selection_relevance_allowed_domain_patterns=resolved_selection_relevance_allowed_domain_patterns,
        selection_require_supported_document=resolved_selection_require_supported_document,
        selection_target_identity_require_any_templates=resolved_selection_target_identity_require_any_templates,
        selection_target_identity_require_all_templates=resolved_selection_target_identity_require_all_templates,
        selection_target_identity_exclude_any_templates=resolved_selection_target_identity_exclude_any_templates,
        document_classifier=resolved_document_classifier,
        document_review=resolved_document_review,
        models=resolved_models,
        seeker_cache=resolved_seeker_cache,
        seeker_cache_ttl_minutes=resolved_seeker_cache_ttl_minutes,
        progress_callback=_discover_progress,
        query_context_aliases=resolved_query_context_aliases,
        partition_by=resolved_partition_by,
        browser_mode=resolved_browser_mode,
        seeker_extra_params=resolved_seeker_extra_params,
        include_url_patterns=resolved_include_url_patterns,
        include_link_text_patterns=resolved_include_link_text_patterns,
        index_page_mode=resolved_index_page_mode,
        index_links=resolved_index_links,
        max_depth=resolved_max_depth,
        max_pages=resolved_max_pages,
        max_files=resolved_max_files,
        timeout_seconds=resolved_timeout_seconds,
        retry_max_attempts=resolved_retry_max_attempts,
        retry_initial_backoff_seconds=resolved_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=resolved_retry_max_backoff_seconds,
        max_concurrent_downloads=max(1, resolved_max_concurrent_downloads),
        min_request_interval_ms=max(0, resolved_min_request_interval_ms),
        robots_policy_mode=resolved_robots_policy_mode,
        tos_policy_mode=resolved_tos_policy_mode,
        acknowledged_tos_domains=resolved_acknowledged_tos_domains or None,
    )

    try:
        with view.live(discover_live):
            result = DiscoveryEngine().run(request)
    except Exception as exc:
        view.error("Discovery failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    if view.is_quiet:
        click.echo(str(result.manifest_path))
        return

    view.success(f"Discovery complete (run_id={result.run_id})")

    try:
        manifest_data = json.loads(
            result.manifest_path.read_text(encoding="utf-8")
        )
    except Exception:
        manifest_data = {}

    notes = manifest_data.get("notes") or []
    errors = manifest_data.get("errors") or []
    download_summary = (
        (manifest_data.get("stage_summaries") or {}).get("downloads") or {}
    )
    downloaded_count = int(download_summary.get("downloaded") or 0)
    total_count = int(download_summary.get("total") or 0)

    view.summary(
        {
            "Run ID": result.run_id,
            "Mode": "dry-run" if dry_run else "run",
            "Downloaded": f"{downloaded_count}/{total_count}",
            "Notes": str(len(notes)),
            "Errors": str(len(errors)),
        },
        title="Discovery Summary",
    )

    view.notes(notes, title="Discovery Notes")

    if errors:
        error_lines = []
        for error in errors[:5]:
            stage = error.get("stage") or "unknown-stage"
            message = error.get("message") or error.get("error") or "unknown error"
            error_lines.append(f"{stage}: {message}")
        if len(errors) > 5:
            error_lines.append(
                f"... and {len(errors) - 5} more (see manifest for full details)"
            )
        view.warnings(error_lines)

    outputs = {
        "Run folder": str(result.manifest_path.parent),
        "Downloads": str(result.documents_dir),
    }
    if result.curated_dir is not None:
        outputs["Curated"] = (
            f"{result.curated_count} document(s) → {result.curated_dir}"
        )
    elif result.review_index_path is not None:
        outputs["Curated"] = "0 documents passed review"
    view.outputs(outputs)

    next_steps = []
    if result.curated_count == 0 and result.review_index_path is not None:
        # Zero curated — guide the user toward fixing the problem.
        next_steps.append(
            "No documents matched the review criteria. Options:"
        )
        next_steps.append(
            "  • Adjust discovery queries or selection filters in your config"
        )
        next_steps.append(
            f"  • Override LLM picks: edit {result.review_index_path} "
            "(set human_decision=keep), then run: pixi run psweep curate"
        )
    elif result.review_index_path is not None:
        next_steps.append(
            f"Review/adjust picks: edit {result.review_index_path}, "
            "then run: pixi run psweep curate"
        )
    if result.curated_count > 0:
        next_steps.append(
            "Extract the documents: pixi run psweep extract "
            f"{result.curated_dir or result.documents_dir} --schema <schema>"
        )
    view.next_steps(next_steps)


@click.command()
@click.argument("target", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to runtime config file (.yaml/.yml/.json) — used to locate the domain's latest run",
)
@click.option(
    "--run",
    "run_dir_opt",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Path to a specific run directory (defaults to the domain's latest/)",
)
@click.option("--quiet", is_flag=True, help="Minimal output")
@click.option("--verbose", is_flag=True, help="Detailed output")
@click.option("--debug", is_flag=True, help="Debug output with tracebacks")
def curate(
    target: Optional[str],
    config_path: Optional[str],
    run_dir_opt: Optional[str],
    quiet: bool,
    verbose: bool,
    debug: bool,
):
    """Rebuild a run's curated/ set from human edits in review.csv.

    After a discovery run, open ``review.csv`` in the run folder and set the
    ``human_decision`` column to ``keep`` or ``reject`` for any file the LLM got
    wrong (blank = accept the LLM's call). Then run this command to re-materialize
    ``curated/`` accordingly. Idempotent.
    """
    view = begin_run("curate", quiet=quiet, verbose=verbose, debug=debug)

    # Resolve the run directory: explicit --run wins, else the domain's latest/.
    run_dir: Optional[Path] = None
    if run_dir_opt:
        run_dir = Path(run_dir_opt)
    else:
        domain = target
        if not domain and config_path:
            try:
                resolved = _resolve_runtime_command_inputs(
                    command_name="discover",
                    config_path=config_path,
                    strict=False,
                    cli_values={},
                )
                domain = resolved.get("domain")
            except RuntimeConfigError:
                domain = None
        if not domain:
            print_error(
                "Could not determine which run to curate",
                "Pass --config <run.yaml>, a domain name, or --run <dir>.",
            )
            sys.exit(1)
        latest = Path("discovered") / str(domain) / "latest"
        if not latest.exists():
            print_error(
                "No latest run found for domain",
                f"Expected {latest.as_posix()} (run `discover` first).",
            )
            sys.exit(1)
        run_dir = latest.resolve()

    review_csv = run_dir / "review.csv"
    documents_dir = run_dir / "documents"
    if not review_csv.exists():
        print_error(
            "review.csv not found in run",
            f"Expected {review_csv.as_posix()}.",
        )
        sys.exit(1)

    view.header("CURATION")
    view.config(
        {
            "Run folder": run_dir.as_posix(),
            "Review ledger": review_csv.name,
            "Documents": documents_dir.name,
        }
    )

    # Read the human-edited ledger and compute the effective keep set.
    with review_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    def _truthy(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"true", "1", "yes", "keep"}

    records: list[dict[str, object]] = []
    kept = overridden = 0
    for row in rows:
        decision = str(row.get("human_decision") or "").strip().lower()
        llm_keep = _truthy(row.get("llm_selected"))
        if decision in {"keep", "reject"}:
            effective = decision == "keep"
            if effective != llm_keep:
                overridden += 1
        else:
            effective = llm_keep
        if effective:
            kept += 1
        records.append(
            {
                "path": row.get("path"),
                "relative_path": row.get("relative_path"),
                "review_selected": effective,
            }
        )
        _update_review_sidecar(row)

    curated_dir, count = DiscoveryEngine._materialize_curated(
        documents_dir=documents_dir,
        download_records=records,
    )

    if view.is_quiet:
        click.echo(curated_dir.as_posix())
        return

    view.success(f"Curated {count} document(s)")
    view.summary(
        {
            "Reviewed": str(len(records)),
            "Kept": str(kept),
            "Rejected": str(len(records) - kept),
            "Human overrides": str(overridden),
        },
        title="Curation Summary",
    )
    view.outputs({"Curated documents": curated_dir.as_posix()})
    view.next_steps(
        [
            f"Extract the curated set: pixi run psweep extract {curated_dir} "
            "--schema <schema>",
        ]
    )


def _update_review_sidecar(row: dict) -> None:
    """Write human_decision/notes from a review.csv row into its .review JSON."""
    path_str = row.get("path")
    if not path_str:
        return
    src = Path(str(path_str))
    sidecar = src.parent / ".review" / f"{src.stem}.json"
    if not sidecar.exists():
        return
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - ignore unreadable sidecar
        return
    payload.setdefault("human", {})
    payload["human"]["decision"] = (
        str(row.get("human_decision") or "").strip() or None
    )
    payload["human"]["notes"] = (
        str(row.get("human_notes") or "").strip() or None
    )
    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return


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
@click.option(
    "--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)"
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Detailed output with statistics"
)
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
):
    """
    Compile extracted JSON files into clean Excel/CSV output.

    Works with ANY schema type - automatically detects structure and creates
    clean, readable output with intelligent deduplication.

    \b
    EXAMPLES:
        # Compile utility tariffs (specify same schema used for extraction)
        psweep compile extracted/tariffs --schema schemas/personal/electricity_tariff_schema.json

        # Compile geothermal ordinances
        psweep compile extracted/geothermal_ordinances --schema schemas/personal/geothermal_ordinance_schema.json

        # Specify custom output directory
        psweep compile extracted/data --schema schemas/your_schema.json --output my_analysis/

    \b
    OUTPUT:
        • Clean Excel file with auto-sized columns
        • CSV file for data analysis
        • Automatic deduplication of identical entries
    """
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
            print_success(
                "Runtime config validation passed for compile command"
            )
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
    synthesis_active = (
        isinstance(synthesis_cfg, dict) and bool(synthesis_cfg.get("enabled"))
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

    # Set up output directory - CLEAN structure
    # extracted/category/ → compiled/category/
    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if "extracted" in parts:
            idx = parts.index("extracted")
            parts[idx] = "compiled"
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / "compiled" / input_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)

    # Header
    if not emit_json_report:
        view.header("COMPILATION")

    # Load schema (enforced as required by Click)
    matched_schema = Path(schema)
    if not matched_schema.exists():
        print_error("Schema not found", matched_schema.as_posix())
        sys.exit(1)

    try:
        from psweep.utils.schema_metadata import SchemaMetadata

        # Build metadata overrides from config YAML compilation section
        metadata_overrides: Dict[str, Any] = {}
        compilation_overrides: Dict[str, Any] = {}

        # Deduplication settings from config
        config_dedup = resolved_inputs.get("deduplication")
        if isinstance(config_dedup, dict):
            compilation_overrides["deduplication"] = config_dedup

        # Output formatting from config
        config_output = resolved_inputs.get("compilation_output")
        if isinstance(config_output, dict):
            compilation_overrides["output"] = config_output

        # Normalization from config
        config_norm = resolved_inputs.get("normalization")
        if isinstance(config_norm, dict):
            compilation_overrides["normalization"] = config_norm

        if compilation_overrides:
            metadata_overrides["compilation"] = compilation_overrides

        runtime_artifact = None

        schema_metadata = SchemaMetadata(
            matched_schema, metadata_overrides=metadata_overrides or None
        )

        # Config snapshot (after schema load so it is complete)
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
                config_info["Overrides"] = (
                    "config-owned compilation settings active"
                )
            view.config(config_info)
    except Exception as e:
        print_error(
            "Schema validation failed",
            f"Schema {matched_schema.name} is missing required $metadata section: {e}\\n"
            "ParseSweep v2.0+ requires schemas with $metadata.\\n"
            "See schemas/SCHEMA_BEST_PRACTICES.md for examples.",
        )
        return

    # Compile
    if not emit_json_report:
        view.phase("Analyzing schema structure")
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=view.verbosity.shows_detail,
        debug=view.verbosity is Verbosity.DEBUG,
    )

    try:
        if synthesis_active:
            # Config-driven per-entity LLM synthesis: reconcile many
            # per-document records into one row per entity (conflict resolution,
            # confidence, chronology validation) instead of tabular dedup.
            from psweep.compilation.synthesizer import (
                Synthesizer,
            )
            from psweep.extraction.llm_factory import (
                build_llm_client,
            )

            # synthesis.model (an alias or a literal model name) selects the
            # model; falls back to the env-configured model when unset. Resolved
            # via the shared factory so it works for every provider.
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
                f"Dry run: synthesized {len(df)} entity row(s); "
                "no files written."
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
            preview_report["would_fail_on_suspicious"] = (
                _should_fail_on_suspicious(
                    preview_report,
                    fail_on_suspicious,
                )
            )

            if emit_json_report or view.is_quiet:
                click.echo(
                    json.dumps(preview_report, indent=2, sort_keys=True)
                )
            else:
                view.phase("Previewing deduplication")
                preview_stats = {
                    "Schema Type": preview_report["schema_type"],
                    "Rows Before Dedup": str(
                        preview_report["rows_before_dedup"]
                    ),
                    "Rows After Dedup": str(
                        preview_report["rows_after_dedup"]
                    ),
                    "Duplicates Removed": str(
                        preview_report["duplicates_removed"]
                    ),
                    "Suspicious Groups": str(
                        preview_report["suspicious_groups_count"]
                    ),
                    "Severity Mix": ", ".join(
                        f"{severity}={count}"
                        for severity, count in preview_report[
                            "suspicious_groups_by_severity"
                        ].items()
                        if count > 0
                    )
                    or "none",
                    "Fail Threshold": preview_report["fail_on_suspicious"],
                    "Key Fields": ", ".join(preview_report["key_fields"])
                    or "none",
                    "Compare Columns": ", ".join(
                        preview_report["compare_columns"]
                    )
                    or "none",
                }
                view.summary(preview_stats, title="Deduplication Preview")

                for warning in preview_report["warnings"]:
                    view.warning(warning)

                for index, group in enumerate(
                    preview_report["duplicate_groups"][:5], start=1
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
                        group_label += (
                            f" (suspicious:{group.get('severity', 'low')})"
                        )
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
                        f"Dry-run found suspicious groups at or above "
                        f"'{fail_on_suspicious}' severity",
                    )
                sys.exit(2)
            return

        view.phase("Creating outputs")

        # Generate output filename
        base_name = input_dir.name.replace("_", "-")
        output_formats = _resolve_compilation_output_formats(
            metadata_overrides or None
        )
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
                view.success(
                    "Excel saved (clean formatting, auto-sized columns)"
                )

        # Summary
        if not get_verbosity().is_quiet:
            summary_stats = {
                "Schema Type": schema_info["type"],
                "Records": str(len(df)),
                "Columns": str(len(df.columns)),
                "Outputs": ", ".join(output_formats),
            }

            # Category breakdown
            if schema_info.get("category_field"):
                category_display = (
                    "".join(
                        [
                            " " + c if c.isupper() else c
                            for c in schema_info.get("category_field", "")
                        ]
                    )
                    .strip()
                    .title()
                )
                if category_display and category_display in df.columns:
                    top_categories = (
                        df[category_display].value_counts().head(5)
                    )
                    if not top_categories.empty:
                        top_cat_str = ", ".join(
                            [
                                f"{cat} ({count})"
                                for cat, count in list(top_categories.items())[
                                    :3
                                ]
                            ]
                        )
                        summary_stats[f"Top {category_display}s"] = top_cat_str

            view.summary(summary_stats, title="Compilation Summary")
            view.outputs(
                {
                    (p.suffix.lstrip(".").upper() or "File"): str(p.absolute())
                    for p in emitted_paths
                }
            )
            view.next_steps(
                ["Open the CSV/Excel to review the compiled dataset"]
            )
        else:
            # Quiet mode - print emitted output path(s)
            for emitted_path in emitted_paths:
                console.print(str(emitted_path.absolute()))

    except Exception as e:
        view.error("Compilation failed", str(e))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)


@click.command()
@click.argument("qa_qc_path", type=click.Path(exists=True))
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
    schema: str,
    qaqc_lane: Optional[str],
    quiet: bool,
    verbose: bool,
):
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
    from psweep.qa_qc import ComparisonEngine, ReportGenerator
    from psweep.qa_qc.utils import resolve_qaqc_runtime_config
    from psweep.utils.schema_metadata import SchemaMetadata

    qa_qc_path = Path(qa_qc_path)
    schema_path = Path(schema)

    view = begin_run("compare", quiet=quiet, verbose=verbose)
    verbosity = view.verbosity.value

    # Load schema metadata
    try:
        schema_metadata = SchemaMetadata(schema_path)
        runtime_artifact = _resolve_runtime_artifact(None, schema_path)
        qa_qc_config = resolve_qaqc_runtime_config(
            schema_metadata,
            runtime_artifact=runtime_artifact,
            preferred_lane=qaqc_lane,
        )
    except Exception as e:
        view.error("Failed to load schema", str(e))
        sys.exit(1)

    # Display header
    view.header("QA/QC COMPARISON REPORT")
    view.config(
        {
            "Input": str(qa_qc_path),
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

    # Create comparison engine and report generator
    engine = ComparisonEngine(schema_metadata, qa_qc_config=qa_qc_config)
    report_gen = ReportGenerator()

    # Find document directories to process
    # If path is a document directory (contains .json files), process just that one
    # Otherwise, process all subdirectories
    doc_dirs = []
    json_files_in_path = list(qa_qc_path.glob("*.json"))

    ignored_qaqc_json_files = {"metadata.json", "comparison_summary.json"}

    if json_files_in_path and any(
        f.name not in ignored_qaqc_json_files for f in json_files_in_path
    ):
        # This is a single document directory
        doc_dirs = [qa_qc_path]
    else:
        # This is a parent directory containing document subdirectories
        doc_dirs = [d for d in sorted(qa_qc_path.iterdir()) if d.is_dir()]

    if not doc_dirs:
        print_error(
            "No QA/QC outputs found",
            f"No document directories found in {qa_qc_path}",
            [
                "Run extraction with --enable-qa-qc first",
                "Check the path is correct",
            ],
        )
        sys.exit(1)

    # Process each document directory
    results = []

    view.phase("Comparing model outputs")

    for doc_dir in doc_dirs:
        # Find model output files
        model_files = {}
        for f in doc_dir.glob("*.json"):
            if f.name in ignored_qaqc_json_files:
                continue
            model_name = f.stem
            model_files[model_name] = f

        if len(model_files) < 2:
            view.status(
                "warning",
                f"Skipping {doc_dir.name}: needs at least 2 model outputs",
            )
            continue

        # Run comparison
        try:
            result = engine.compare_outputs(model_files, doc_dir.name)

            # Generate reports
            report_gen.generate_report(result, doc_dir)

            results.append(
                {
                    "name": doc_dir.name,
                    "success": True,
                    "models": result.models,
                    "items_per_model": result.summary.get(
                        "items_per_model", {}
                    ),
                    "full_agreement_pct": result.summary.get(
                        "full_agreement_pct", 0
                    ),
                    "needs_review_count": result.summary.get(
                        "needs_review_count", 0
                    ),
                    "total_comparisons": result.summary.get(
                        "total_comparisons", 0
                    ),
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
                qualitative_gate = (
                    result.summary.get("qualitative_advisory_gate") or {}
                )
                if qualitative_gate:
                    gate_status = str(
                        qualitative_gate.get("status", "not_applicable")
                    ).upper()
                    aligned_pct = float(
                        qualitative_gate.get("aligned_pct", 0.0)
                    )
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
                    qualitative_breakdown.get("scope_variant_by_category")
                    or []
                )
                text_categories = (
                    qualitative_breakdown.get("text_difference_by_category")
                    or []
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

        except Exception as e:
            results.append(
                {
                    "name": doc_dir.name,
                    "success": False,
                    "error": str(e),
                }
            )
            view.status("error", doc_dir.name, str(e)[:50])

    # Summary
    if not view.is_quiet:
        successful = [r for r in results if r.get("success")]
        failed = [r for r in results if not r.get("success")]

        if successful:
            avg_agreement = sum(
                r["full_agreement_pct"] for r in successful
            ) / len(successful)
            total_reviews = sum(r["needs_review_count"] for r in successful)

            summary_stats = {
                "Documents Compared": str(len(successful)),
                "Average Agreement": f"{avg_agreement:.1f}%",
                "Total Fields Needing Review": str(total_reviews),
            }

            qualitative_gates = [
                r.get("qualitative_gate")
                for r in successful
                if r.get("qualitative_gate")
            ]
            if qualitative_gates:
                gate_counts = {}
                for gate in qualitative_gates:
                    gate_status = str(
                        gate.get("status", "not_applicable")
                    ).upper()
                    gate_counts[gate_status] = (
                        gate_counts.get(gate_status, 0) + 1
                    )
                summary_stats["Qualitative Gates"] = ", ".join(
                    f"{status}: {count}"
                    for status, count in sorted(gate_counts.items())
                )

            if failed:
                summary_stats["Failed"] = str(len(failed))

            view.summary(summary_stats, title="Comparison Summary")
            view.outputs(
                {
                    "Reports": str(qa_qc_path),
                    "Files": "comparison_report.xlsx, comparison_report.csv",
                }
            )
        else:
            view.warning("No documents were successfully compared")
    else:
        # Quiet mode - just print success count
        successful = len([r for r in results if r.get("success")])
        console.print(f"{successful} documents compared")


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
@click.option(
    "--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)"
)
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
):
    """Build a performance profile from run manifests and evaluate benchmark gates."""
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
        compilation_baseline_dir,
        profile_values,
        "compilation_baseline_dir",
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
        min_qaqc_qualitative_pass_rate,
        profile_values,
        "min_qaqc_qualitative_pass_rate",
    )
    min_compilation_correctness = _coalesce_benchmark_option(
        min_compilation_correctness,
        profile_values,
        "min_compilation_correctness",
    )
    max_failure_rate = _coalesce_benchmark_option(
        max_failure_rate, profile_values, "max_failure_rate"
    )
    max_average_seconds_per_document = _coalesce_benchmark_option(
        max_average_seconds_per_document,
        profile_values,
        "max_average_seconds_per_document",
    )
    min_documents_per_minute = _coalesce_benchmark_option(
        min_documents_per_minute, profile_values, "min_documents_per_minute"
    )
    max_total_errors = _coalesce_benchmark_option(
        max_total_errors, profile_values, "max_total_errors"
    )
    max_throughput_delta_percent = _coalesce_benchmark_option(
        max_throughput_delta_percent,
        profile_values,
        "max_throughput_delta_percent",
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
    if (
        min_compilation_correctness is not None
        and compilation_baseline_dir is None
    ):
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
            sys.exit(1)
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
            if baseline_comparison["baseline_median_document_duration_seconds"]
            is not None
            else "N/A",
            "Baseline Median Doc Cost": f"${baseline_comparison['baseline_median_document_cost_usd']:.4f}"
            if baseline_comparison["baseline_median_document_cost_usd"]
            is not None
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
            sys.exit(1)
    else:
        view.info("No thresholds supplied; reported metrics only")
