"""
Core workflow CLI commands for StreamlineExtract.

This module contains the main data processing pipeline commands:
- process: Extract structured data from documents to JSON
- consolidate: Merge JSON files into Excel/CSV
"""

import csv
import json
import hashlib
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import click
from click.core import ParameterSource
from dotenv import load_dotenv
from rich.logging import RichHandler

from streamline_extract.utils.config import get_config
from streamline_extract.extraction import DocumentExtractor, load_schema
from streamline_extract.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS,
)
from streamline_extract.consolidation.consolidator import Consolidator
from streamline_extract.acquisition import (
    AcquisitionEngine,
    AcquisitionRequest,
)
from streamline_extract.cli.ui import (
    console,
    print_header,
    print_error,
    print_warning,
    print_success,
    print_info,
    create_config_table,
    create_summary_table,
    create_extraction_progress,
    ask_confirm,
)
from streamline_extract.cli.dashboard import create_live_dashboard
from streamline_extract.benchmarking import (
    collect_benchmark_metrics,
    compare_benchmark_to_baseline,
    evaluate_benchmark_gates,
    load_benchmark_snapshot,
    write_benchmark_snapshot,
)
from streamline_extract.core import (
    compile_runtime_artifact,
    resolve_pack_ref_for_schema,
    ArtifactCompilerError,
)
from streamline_extract.config import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from streamline_extract.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)


# Global verbosity level (set by CLI flags)
VERBOSITY = "normal"  # 'quiet', 'normal', 'verbose', 'debug'

_BENCHMARK_PROFILE_PATH_FIELDS = {
    "path",
    "extraction_baseline_dir",
    "qaqc_baseline_dir",
    "consolidation_baseline_dir",
    "consolidation_schema",
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
    console.print(create_config_table("", display))


def _resolve_runtime_command_inputs(
    *,
    command_name: str,
    config_path: Optional[str],
    strict: bool,
    cli_values: Dict[str, Any],
) -> Dict[str, Any]:
    """Load and resolve command inputs from config files and CLI overrides."""
    config_data = None
    if config_path:
        config_data = load_runtime_config_file(Path(config_path))

    return resolve_command_config(
        command=command_name,
        cli_values=cli_values,
        config_data=config_data,
        strict=strict,
    )


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
    *,
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a compiled runtime artifact for lineage emission when available."""
    root = repo_root or Path(__file__).resolve().parents[3]
    packs_root = domain_packs_dir or (root / "schemas/domain_packs")
    profiles_root = profiles_dir or (root / "schemas/profiles")

    if not packs_root.exists() or not profiles_root.exists():
        return None

    candidate_pack_refs: List[str] = []
    if category:
        candidate_pack_refs.append(category)
    candidate_pack_refs.append(schema_path.stem)
    resolved_pack_ref = resolve_pack_ref_for_schema(
        schema_path,
        repo_root=root,
        domain_packs_dir=packs_root,
    )
    if resolved_pack_ref:
        candidate_pack_refs.append(resolved_pack_ref)

    seen = set()
    for pack_ref in candidate_pack_refs:
        if pack_ref in seen:
            continue
        seen.add(pack_ref)

        try:
            return compile_runtime_artifact(
                pack_ref,
                profile_name,
                repo_root=root,
                domain_packs_dir=packs_root,
                profiles_dir=profiles_root,
            )
        except ArtifactCompilerError:
            continue

    return None


def _format_runtime_artifact_summary(
    runtime_artifact: Optional[Dict[str, Any]],
) -> str:
    """Return a concise user-facing summary of the resolved runtime artifact."""
    if not runtime_artifact:
        return "schema-only (no pack/profile runtime artifact resolved)"

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


@click.command()
@click.argument("path", type=click.Path(), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to runtime config file (.yaml/.yml/.json)",
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
    help="Validate resolved command inputs and exit without processing",
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
    help="Path to JSON schema file (required unless provided in --config)",
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
    help="Load acquired files from a download_index.csv (bypasses PATH argument)",
)
@click.option(
    "--filter-state",
    "filter_state",
    type=str,
    default=None,
    help="With --from-index: keep only files where source_state matches (e.g., ca)",
)
@click.option(
    "--filter-jurisdiction",
    "filter_jurisdiction",
    type=str,
    default=None,
    help="With --from-index: keep only files where source_jurisdiction matches (e.g., imperial-county)",
)
def process(
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
    filter_state: Optional[str] = None,
    filter_jurisdiction: Optional[str] = None,
):
    """
    Process documents and extract structured data.

    This command reads PDF documents and extracts structured information
    based on a JSON schema. Works with any document type (permits, ordinances, regulations, etc.).

    Supports multiple LLM providers: OpenAI, Azure OpenAI, Claude, Gemini, and more.
    See docs/MODEL_COSTS.md for cost comparison and model selection guidance.

    The output will be saved as JSON files in a parallel folder structure.
    For example: documents/Category/ → processed/Category/

    \b
    EXAMPLES:
        # Process with the production tariff schema
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json

        # Use Claude for high-quality extraction
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json --model claude-3.5-sonnet

        # Use Gemini for budget-friendly processing
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json --model gemini-1.5-flash

        # Use Azure OpenAI
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json --provider azure

        # Use the production runtime profile for artifact lineage
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json --profile prod

        # Process with page ranges
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json --pages-csv config/tariffs/page_ranges.csv

        # Test with first 5 documents
        streamline-extract process documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json -n 5

    \b
    SUPPORTED MODELS:
        OpenAI:     gpt-4o, gpt-4o-mini (default), gpt-4.1, gpt-5
        Claude:     claude-3.5-sonnet, claude-opus-4.5, claude-haiku-4.5
        Gemini:     gemini-1.5-pro, gemini-1.5-flash, gemini-3-pro

        See docs/MODEL_COSTS.md for detailed cost comparison.

    \b
    REQUIREMENTS:
        • Documents in the specified directory
        • JSON schema file (--schema flag is REQUIRED)
        • API key in .env file for your chosen provider
    """
    # Set global verbosity
    global VERBOSITY
    if quiet:
        VERBOSITY = "quiet"
    elif debug:
        VERBOSITY = "debug"
    elif verbose:
        VERBOSITY = "verbose"
    else:
        VERBOSITY = "normal"

    # Configure logging with RichHandler for clean integration with progress bars
    configure_logging(VERBOSITY)
    load_dotenv()

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
            if filter_state:
                _fi_state_key = filter_state.lower().strip()
                _fi_downloaded = [
                    r
                    for r in _fi_downloaded
                    if (r.get("source_state") or "").lower() == _fi_state_key
                ]
            if filter_jurisdiction:
                _fi_jur_key = re.sub(
                    r"[^a-z0-9]+", "-", filter_jurisdiction.lower()
                ).strip("-")
                _fi_downloaded = [
                    r
                    for r in _fi_downloaded
                    if (r.get("source_jurisdiction") or "").lower()
                    == _fi_jur_key
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
            command_name="process",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        print_error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    for warning in warnings:
        if VERBOSITY != "quiet":
            print_warning(warning)

    if show_effective_config and VERBOSITY != "quiet":
        _print_effective_config("process", resolved_inputs)
        console.print()

    if validate_config_only:
        if VERBOSITY == "quiet":
            click.echo(
                json.dumps(
                    {
                        "command": "process",
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
                "Runtime config validation passed for process command"
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
    # documents/category/ → processed/category/
    if output:
        output_dir = Path(output)
    elif is_dir:
        # Replace 'documents' with 'processed' at project root level
        parts = list(path.parts)
        if "documents" in parts:
            idx = parts.index("documents")
            parts[idx] = "processed"
            output_dir = Path(*parts)
        else:
            # Fallback: create in project root processed/
            project_root = Path.cwd()
            output_dir = project_root / "processed" / path.name
    else:
        # Single file: parent directory logic
        parts = list(path.parent.parts)
        if "documents" in parts:
            idx = parts.index("documents")
            parts[idx] = "processed"
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / "processed" / path.parent.name

    output_dir.mkdir(parents=True, exist_ok=True)

    # Get document files - support all formats (PDF, DOCX, TXT, XLSX, CSV)
    # Tracks per-file output directories for nested folder structures
    file_output_dirs = {}  # Maps doc_path -> its specific output directory

    if is_dir:
        # Find all supported document types in this directory (non-recursive first)
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(path.glob(f"*{ext}")))
        doc_files = sorted(doc_files)

        # If no documents found directly, search recursively in subdirectories
        if not doc_files:
            for ext in SUPPORTED_EXTENSIONS:
                doc_files.extend(sorted(path.rglob(f"*{ext}")))
            doc_files = sorted(doc_files)

            if doc_files and VERBOSITY != "quiet":
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
                    console.print(
                        f"\n[bold]📁 Found {len(doc_files)} document(s) across {len(subdirs_found)} subfolder(s)[/bold]"
                    )
                    # Show per-subfolder counts
                    for sdir in sorted(subdirs_found):
                        sdir_docs = [
                            d
                            for d in doc_files
                            if d.relative_to(path).parts[0] == sdir
                        ]
                        console.print(
                            f"  → {sdir}: {len(sdir_docs)} document(s)"
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
            if skipped > 0 and len(doc_files) > 0 and VERBOSITY != "quiet":
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

    # --from-index: override doc_files with files listed in the acquisition CSV.
    # This wires the acquire → process pipeline without any manual path wrangling.
    if from_index and _from_index_data.get("doc_files"):
        doc_files = _from_index_data["doc_files"]
        if limit:
            doc_files = doc_files[:limit]
        # Override output dir to processed/<domain>/ unless explicitly set
        if not output:
            output_dir = Path("processed") / _from_index_data["domain"]
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
            if skipped > 0 and VERBOSITY != "quiet":
                print_info(
                    f'Skipping {skipped} already processed file{"s" if skipped != 1 else ""} (use --reprocess to extract again)'
                )
        if not doc_files:
            print_error(
                "No new files to process from download index",
                "All acquired files have already been processed. Use --reprocess to extract again.",
            )
            sys.exit(0)
        if VERBOSITY != "quiet":
            print_info(
                f"From-index mode: {len(doc_files)} file(s) loaded from {from_index}"
            )

    # Handle page range specifications
    from streamline_extract.utils.page_range import (
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

            if VERBOSITY != "quiet":
                mapped_count = sum(
                    1 for v in page_range_map.values() if v is not None
                )
                console.print(
                    f"[dim]Loaded page ranges for {mapped_count} file(s) from CSV[/dim]"
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

            if VERBOSITY != "quiet":
                console.print(
                    f"[dim]Extracting pages {page_range_tuple[0]}-{page_range_tuple[1]} only[/dim]"
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

    # Final validation - check if we have files to process
    if not doc_files:
        if is_dir and VERBOSITY != "quiet":
            print_success(f"All {original_count} file(s) already processed!")
            console.print(f"[dim]Output directory: {output_dir}[/dim]")
            console.print("[dim]Use --reprocess to extract them again[/dim]\n")
        return

    # Display header and configuration
    if VERBOSITY != "quiet":
        print_header("DOCUMENT EXTRACTION")

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
    model_display = config.llm_config.get("model", model)

    # Add model/provider info to config
    if VERBOSITY != "quiet":
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

    # Load schema (required via CLI or runtime config)
    schema_path = Path(schema)
    if not schema_path.exists():
        print_error("Schema not found", schema_path.as_posix())
        sys.exit(1)
    loaded_schema = load_schema(schema_path)
    runtime_artifact = _resolve_runtime_artifact(
        category, schema_path, profile_name=profile_name
    )
    if VERBOSITY != "quiet":
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
    if VERBOSITY != "quiet":
        table = create_config_table("Configuration", config_info)
        console.print(table)
        console.print()

    # Cost estimation and confirmation for large batches
    if len(doc_files) > 10 and VERBOSITY != "quiet":
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

            # Rough cost estimate (gpt-4o-mini rates)
            input_cost = (estimated_tokens / 1_000_000) * 0.15
            output_cost = (estimated_tokens * 0.1 / 1_000_000) * 0.60
            total_est_cost = input_cost + output_cost

            if total_est_cost > 1.0:  # Threshold for confirmation
                console.print(
                    f"\n[yellow]⚠ Cost Estimate:[/yellow] ${total_est_cost:.2f}"
                )
                console.print(
                    f"[dim]  Processing {len(doc_files)} documents with ~{estimated_tokens:,} tokens[/dim]\n"
                )

                if not ask_confirm("Proceed with extraction?", default=True):
                    console.print("[yellow]Operation cancelled[/yellow]\n")
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

    # Track the actual model being used for output
    actual_model = (
        config.llm_config.get("model", model)
        if provider != "openai"
        else model
    )
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
            verbosity=VERBOSITY,
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
            from streamline_extract.utils.schema_metadata import SchemaMetadata

            schema_metadata = SchemaMetadata(schema_path)
        except Exception as e:
            if VERBOSITY == "verbose":
                console.print(
                    f"[dim yellow]Could not load schema metadata: {e}[/dim yellow]"
                )

    # Initialize extractor with clean configuration
    extractor = DocumentExtractor(
        api_key=api_key,
        model=actual_model,
        max_context_chars=max_context,
        schema_metadata=schema_metadata,
        provider=provider,
        azure_endpoint=config.llm_config.get("azure_endpoint"),
        azure_api_version=config.llm_config.get("azure_api_version"),
    )

    # Processing section
    if VERBOSITY == "normal" or VERBOSITY == "verbose":
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()

    results = []
    total_cost = 0.0
    total_time = 0.0

    # Use live dashboard for multiple files if requested
    if len(doc_files) > 3 and live_dashboard and VERBOSITY != "quiet":
        live, dashboard = create_live_dashboard(len(doc_files), actual_model)

        with live:
            for doc_path in doc_files:
                dashboard.start_document(doc_path.name)

                try:
                    # Extract text from document
                    text = extract_text_from_document(doc_path)
                    result = extractor.extract(text, loaded_schema)

                    # Save result (use per-file output dir for nested structures)
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
                    )

                    # Update dashboard
                    dashboard.complete_document(
                        doc_path.name,
                        success=True,
                        cost=result.cost,
                        input_tokens=0,  # Would need to track from extractor
                        output_tokens=0,
                    )

                    # Track results
                    results.append(
                        {
                            "file": doc_path.name,
                            "items": num_items,
                            "cost": result.cost,
                            "time": result.processing_time,
                            "output_path": (
                                doc_output_dir / f"{doc_path.stem}.json"
                            ).as_posix(),
                            "success": True,
                        }
                    )
                    total_cost += result.cost
                    total_time += result.processing_time

                except Exception as e:
                    error_record = build_error_record(
                        e,
                        stage="process",
                        document_path=doc_path.as_posix(),
                        model=actual_model,
                        provider=provider,
                    )
                    suggestions = _context_budget_suggestions_for_process(
                        error_record=error_record,
                        schema_path=schema_path,
                        document_path=doc_path,
                    )
                    dashboard.complete_document(doc_path.name, success=False)
                    results.append(
                        {
                            "file": doc_path.name,
                            "success": False,
                            "error": str(e),
                            "errors": [error_record],
                            "suggestions": suggestions,
                        }
                    )

    # Use progress bar for multiple files, simple output for single file
    elif len(doc_files) > 1 and VERBOSITY != "quiet":
        progress = create_extraction_progress()
        # Start with first document name instead of generic "Extracting..." message
        first_doc = doc_files[0]
        first_desc = first_doc.name
        # Show page range in progress bar if specified
        if (
            first_doc in page_range_map
            and page_range_map[first_doc] is not None
        ):
            start, end = page_range_map[first_doc]
            first_desc += f" [dim](pages {start}-{end})[/dim]"

        task = progress.add_task(first_desc, total=len(doc_files))

        with progress:
            for idx, doc_path in enumerate(doc_files):
                # Update progress description to show current document (skip first since already set)
                if idx > 0:
                    desc = doc_path.name
                    if (
                        doc_path in page_range_map
                        and page_range_map[doc_path] is not None
                    ):
                        start, end = page_range_map[doc_path]
                        desc += f" [dim](pages {start}-{end})[/dim]"
                    progress.update(task, description=desc)

                try:
                    # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                    # Use page range if specified for this file
                    page_range = page_range_map.get(doc_path)
                    text = extract_text_from_document(
                        doc_path, page_range=page_range
                    )
                    result = extractor.extract(text, loaded_schema)

                    # Save result (use per-file output dir for nested structures)
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
                    )

                    # Track results
                    results.append(
                        {
                            "file": doc_path.name,
                            "items": num_items,
                            "cost": result.cost,
                            "time": result.processing_time,
                            "output_path": (
                                doc_output_dir / f"{doc_path.stem}.json"
                            ).as_posix(),
                            "success": True,
                        }
                    )
                    total_cost += result.cost
                    total_time += result.processing_time

                except Exception as e:
                    error_record = build_error_record(
                        e,
                        stage="process",
                        document_path=doc_path.as_posix(),
                        model=actual_model,
                        provider=provider,
                    )
                    suggestions = _context_budget_suggestions_for_process(
                        error_record=error_record,
                        schema_path=schema_path,
                        document_path=doc_path,
                    )
                    results.append(
                        {
                            "file": doc_path.name,
                            "success": False,
                            "error": str(e),
                            "errors": [error_record],
                            "suggestions": suggestions,
                        }
                    )

                progress.update(task, advance=1)
    else:
        # Single file or quiet mode
        for doc_path in doc_files:
            if VERBOSITY == "verbose" or VERBOSITY == "normal":
                console.print()
                # Show page range if specified
                page_range = page_range_map.get(doc_path)
                if page_range is not None:
                    start, end = page_range
                    console.print(
                        f"[cyan]→[/cyan] {doc_path.name} [dim](pages {start}-{end})[/dim]"
                    )
                else:
                    console.print(f"[cyan]→[/cyan] {doc_path.name}")

            try:
                # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                # Use page range if specified for this file
                page_range = page_range_map.get(doc_path)
                text = extract_text_from_document(
                    doc_path, page_range=page_range
                )
                result = extractor.extract(text, loaded_schema)

                # Save result (use per-file output dir for nested structures)
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
                )

                # Track results
                results.append(
                    {
                        "file": doc_path.name,
                        "items": num_items,
                        "cost": result.cost,
                        "time": result.processing_time,
                        "output_path": (
                            doc_output_dir / f"{doc_path.stem}.json"
                        ).as_posix(),
                        "success": True,
                    }
                )
                total_cost += result.cost
                total_time += result.processing_time

                if VERBOSITY == "verbose" or VERBOSITY == "normal":
                    console.print(
                        f"  [green]✓[/green] {num_items} items • [magenta]${result.cost:.4f}[/magenta] • [dim]{result.processing_time:.1f}s[/dim]"
                    )

            except Exception as e:
                error_record = build_error_record(
                    e,
                    stage="process",
                    document_path=doc_path.as_posix(),
                    model=actual_model,
                    provider=provider,
                )
                suggestions = _context_budget_suggestions_for_process(
                    error_record=error_record,
                    schema_path=schema_path,
                    document_path=doc_path,
                )
                results.append(
                    {
                        "file": doc_path.name,
                        "success": False,
                        "error": str(e),
                        "errors": [error_record],
                        "suggestions": suggestions,
                    }
                )
                if VERBOSITY != "quiet":
                    console.print(
                        f"  [yellow]✗[/yellow] [dim]Error: {str(e)[:60]}[/dim]"
                    )

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
        if VERBOSITY == "verbose":
            console.print(
                f"[dim]Run manifest: {manifest_path.as_posix()}[/dim]"
            )
    except Exception as exc:
        if VERBOSITY != "quiet":
            print_warning(f"Run manifest write failed: {exc}")

    # Summary
    if VERBOSITY != "quiet":
        summary_stats = {
            "Processed": f"{len(results)} file{'s' if len(results) != 1 else ''}",
            "Successful": f"[green]{len(successful)}[/green]",
        }

        if failed:
            summary_stats["Failed"] = f"[yellow]{len(failed)}[/yellow]"

        if successful:
            avg_cost = total_cost / len(successful)
            avg_time = total_time / len(successful)
            summary_stats["Total Cost"] = (
                f"[magenta]${total_cost:.4f}[/magenta]"
            )
            summary_stats["Avg Cost/File"] = (
                f"[magenta]${avg_cost:.4f}[/magenta]"
            )
            summary_stats["Total Time"] = f"{total_time:.1f}s"
            summary_stats["Avg Time/File"] = f"{avg_time:.1f}s"

            total_items = sum(r.get("items", 0) or 0 for r in successful)
            if total_items > 0:
                summary_stats["Total Items"] = f"[green]{total_items}[/green]"

        console.print()
        table = create_summary_table("Extraction Summary", summary_stats)
        console.print(table)

        suggestion_failures = [
            failure for failure in failed if failure.get("suggestions")
        ]
        if suggestion_failures:
            console.print()
            for failure in suggestion_failures[:3]:
                print_error(
                    f"Processing failed for {failure['file']}",
                    failure.get("error"),
                    failure.get("suggestions"),
                )

        console.print()
        console.print("[bold green]✓ Results saved to:[/bold green]")
        console.print(f"  [bold]{output_dir.absolute()}[/bold]")
        console.print()


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
    4. Saving outputs to processed/qa_qc/{doc_name}/
    """
    from streamline_extract.qa_qc import (
        ModelDetector,
        run_multi_model_extraction,
    )
    from streamline_extract.extraction.document_utils import (
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

    # Show QA/QC configuration
    if verbosity != "quiet":
        console.print()
        console.print(
            "[bold cyan]━━━ QA/QC Multi-Model Validation Mode ━━━[/bold cyan]"
        )
        console.print(f"  Provider: [bold]{qa_provider.upper()}[/bold]")
        console.print(f"  Models: [bold]{', '.join(qa_models)}[/bold]")
        console.print(f"  Documents: [bold]{len(doc_files)}[/bold]")
        console.print(f"  QA/QC Lane: [bold]{qaqc_lane or 'default'}[/bold]")
        console.print()
        console.print(
            f"[yellow]⚠ Cost Warning:[/yellow] This will run [bold]{len(qa_models)}x[/bold] extractions per document"
        )
        console.print(
            f"[dim]  Total API calls: {len(doc_files)} docs × {len(qa_models)} models = {len(doc_files) * len(qa_models)} extractions[/dim]"
        )
        console.print()

        if not ask_confirm("Proceed with QA/QC extraction?", default=True):
            console.print("[yellow]Operation cancelled[/yellow]\n")
            return

    # Process each document with multi-model extraction
    results = []
    total_cost = 0.0
    total_time = 0.0

    if verbosity != "quiet":
        console.print()
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()

    for doc_idx, doc_path in enumerate(doc_files, 1):
        if verbosity != "quiet":
            console.print(
                f"[cyan][{doc_idx}/{len(doc_files)}][/cyan] {doc_path.name}"
            )

        try:
            # Extract text from document (respecting page ranges)
            page_range = page_range_map.get(doc_path)
            text = extract_text_from_document(doc_path, page_range=page_range)

            if verbosity == "verbose":
                console.print(
                    f"  [dim]Extracted {len(text):,} characters[/dim]"
                )

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

            if verbosity != "quiet":
                status = (
                    "[green]✓[/green]"
                    if successful == len(qa_models)
                    else "[yellow]⚠[/yellow]"
                )
                console.print(
                    f"  {status} {successful}/{len(qa_models)} models • [magenta]${doc_cost:.4f}[/magenta] • [dim]{doc_time:.1f}s[/dim]"
                )

        except Exception as e:
            results.append(
                {
                    "file": doc_path.name,
                    "success": False,
                    "error": str(e),
                }
            )
            if verbosity != "quiet":
                console.print(f"  [red]✗[/red] Error: {str(e)[:60]}")

    # Summary
    if verbosity != "quiet":
        console.print()
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()

        successful_docs = [r for r in results if r.get("success")]
        failed_docs = [r for r in results if not r.get("success")]

        summary_stats = {
            "Documents Processed": f"{len(results)}",
            "Successful": f"[green]{len(successful_docs)}[/green]",
        }

        if failed_docs:
            summary_stats["Failed"] = f"[red]{len(failed_docs)}[/red]"

        summary_stats["Models Used"] = (
            f"{len(qa_models)} ({', '.join(qa_models[:3])}{'...' if len(qa_models) > 3 else ''})"
        )
        summary_stats["Total API Calls"] = (
            f"{len(successful_docs) * len(qa_models)}"
        )
        summary_stats["Total Cost"] = f"[magenta]${total_cost:.4f}[/magenta]"
        summary_stats["Total Time"] = f"{total_time:.1f}s"

        table = create_summary_table("QA/QC Extraction Summary", summary_stats)
        console.print(table)

        # Output location
        qa_qc_output = output_dir / "qa_qc"
        console.print()
        console.print("[bold green]✓ QA/QC outputs saved to:[/bold green]")
        console.print(f"  [bold]{qa_qc_output.absolute()}[/bold]")
        console.print()
        compare_command = (
            "pixi run streamline-extract compare "
            f"{qa_qc_output.as_posix()} --schema {schema_path.as_posix()}"
        )
        if qaqc_lane:
            compare_command += f" --qaqc-lane {qaqc_lane}"
        console.print("[dim]Next step: run the comparison workflow:[/dim]")
        console.print(f"[dim]  {compare_command}[/dim]")
        console.print()


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
) -> int:
    """Helper to extract items count and save result to JSON."""
    # Universal schema detection - find main array and identifier dynamically
    num_items = 0
    identifier = "N/A"

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
            for id_field in [
                "id",
                "identifier",
                "jurisdiction",
                "number",
                "name",
            ]:
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
            and key.lower() in ["id", "identifier", "jurisdiction", "number"]
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
            "input_tokens": None,
            "output_tokens": None,
        },
    }

    output_file = output_dir / f"{doc_path.stem}.json"
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    return num_items


def _resolve_consolidation_output_formats(
    metadata_overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Resolve which consolidation outputs to emit.

    Runtime pack overrides are the only source that currently changes output
    selection. Schema-owned `default_format` remains non-authoritative so
    existing domains keep emitting both files until they explicitly opt in at
    the runtime-pack layer.
    """
    output_config = (
        (metadata_overrides or {}).get("consolidation") or {}
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
        "Unsupported consolidation output format '%s'; falling back to csv+excel",
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
def validate(extraction_file: str, verbose: bool, show_data: bool):
    """
    Validate an extraction result against the schema.

    \b
    EXAMPLES:
        streamline-extract validate processed/data/doc.json
        streamline-extract validate processed/data/doc.json --show-data
    """
    from streamline_extract.cli.ui import display_json

    extraction_file = Path(extraction_file)

    print_header(f"Validation: {extraction_file.name}")

    # Load extraction
    try:
        with open(extraction_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print_error("Invalid JSON file", str(e))
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
        print_success("Schema validation passed")
    except ValidationError as e:
        print_error("Schema validation failed", e.message)
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

        console.print()
        table = create_config_table("Extraction Metadata", metadata)
        console.print(table)

    # Show extracted data with syntax highlighting
    if show_data and "payload" in data:
        console.print()
        from streamline_extract.cli.ui import display_json

        display_json(data.get("payload"), title="Extracted Data")

    console.print()


@click.command()
@click.argument("target", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to runtime config file (.yaml/.yml/.json)",
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
    help="Validate resolved command inputs and exit without running acquisition",
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
    help="Domain key for acquisition output structure",
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
    help="Maximum parallel downloads during acquire runs",
)
@click.option(
    "--min-request-interval-ms",
    type=int,
    default=None,
    help="Minimum delay between outbound acquisition requests in milliseconds",
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
    help="Directory for acquired documents (defaults to run-scoped deterministic path)",
)
@click.option(
    "--output-manifest",
    type=click.Path(),
    default=None,
    help="Path to acquisition manifest JSON (defaults to run-scoped deterministic path)",
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
def acquire(
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
    """Acquire source documents from web targets (scaffold entrypoint)."""
    global VERBOSITY
    if quiet:
        VERBOSITY = "quiet"
    elif debug:
        VERBOSITY = "debug"
    elif verbose:
        VERBOSITY = "verbose"
    else:
        VERBOSITY = "normal"

    configure_logging(VERBOSITY)

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
            command_name="acquire",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        print_error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    for warning in warnings:
        if VERBOSITY != "quiet":
            print_warning(warning)

    if show_effective_config and VERBOSITY != "quiet":
        _print_effective_config("acquire", resolved_inputs)
        console.print()

    if validate_config_only:
        if VERBOSITY == "quiet":
            click.echo(
                json.dumps(
                    {
                        "command": "acquire",
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
                "Runtime config validation passed for acquire command"
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
    resolved_query_context_aliases = (
        resolved_inputs.get("query_context_aliases") or None
    )
    resolved_partition_by = resolved_inputs.get("partition_by") or None
    resolved_browser_mode = bool(resolved_inputs.get("browser_mode") or False)

    if not resolved_seed_urls and not resolved_query and not resolved_targets:
        print_error(
            "Acquisition input missing",
            "Provide at least one --seed-url, --query, or acquisition.targets entry in config.",
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

    if VERBOSITY != "quiet":
        print_header("ACQUISITION")
        if VERBOSITY in {"verbose", "debug"}:
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
        console.print(create_config_table("", config_info))
        console.print()

    request = AcquisitionRequest(
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
        query_context_aliases=resolved_query_context_aliases,
        partition_by=resolved_partition_by,
        browser_mode=resolved_browser_mode,
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
        result = AcquisitionEngine().run(request)
    except Exception as exc:
        print_error("Acquisition failed", str(exc))
        if VERBOSITY == "debug":
            import traceback

            traceback.print_exc()
        sys.exit(1)

    if VERBOSITY == "quiet":
        click.echo(str(result.manifest_path))
        return

    print_success(f"Acquisition scaffold complete (run_id={result.run_id})")
    print_info(f"Manifest: {result.manifest_path}")
    print_info(f"Documents directory: {result.documents_dir}")
    if result.download_index_path is not None:
        print_info(f"Download index CSV: {result.download_index_path}")
    print_info(
        "Acquisition manifest emitted with seeker/scaffold candidates for this run."
    )


@click.command()
@click.argument("extracted_dir", type=click.Path(exists=True), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to runtime config file (.yaml/.yml/.json)",
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
    help="Validate resolved command inputs and exit without consolidating",
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
    help="Path to JSON schema file (required unless provided in --config)",
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
    help="Preview consolidation and deduplication without writing output files",
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
def consolidate(
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
    Consolidate extracted JSON files into clean Excel/CSV output.

    Works with ANY schema type - automatically detects structure and creates
    clean, readable output with intelligent deduplication.

    \b
    EXAMPLES:
        # Consolidate utility tariffs (specify same schema used for extraction)
        streamline-extract consolidate processed/tariffs --schema schemas/personal/electricity_tariff_schema.json

        # Consolidate geothermal ordinances
        streamline-extract consolidate processed/geothermal_ordinances --schema schemas/personal/geothermal_ordinance_schema.json

        # Specify custom output directory
        streamline-extract consolidate processed/data --schema schemas/your_schema.json --output my_analysis/

    \b
    OUTPUT:
        • Clean Excel file with auto-sized columns
        • CSV file for data analysis
        • Automatic deduplication of identical entries
    """
    # Set global verbosity
    global VERBOSITY
    if quiet:
        VERBOSITY = "quiet"
    elif debug:
        VERBOSITY = "debug"
    elif verbose:
        VERBOSITY = "verbose"
    else:
        VERBOSITY = "normal"

    # Configure logging with RichHandler for clean integration with UI
    configure_logging(VERBOSITY)

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
            command_name="consolidate",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        print_error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    for warning in warnings:
        if VERBOSITY != "quiet":
            print_warning(warning)

    if show_effective_config and VERBOSITY != "quiet":
        _print_effective_config("consolidate", resolved_inputs)
        console.print()

    if validate_config_only:
        if VERBOSITY == "quiet":
            click.echo(
                json.dumps(
                    {
                        "command": "consolidate",
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
                "Runtime config validation passed for consolidate command"
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
    # processed/category/ → consolidated/category/
    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if "processed" in parts:
            idx = parts.index("processed")
            parts[idx] = "consolidated"
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / "consolidated" / input_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)

    # Header
    if VERBOSITY != "quiet" and not emit_json_report:
        print_header("CONSOLIDATION")

        config_info = {"Input": str(input_dir), "Output": str(output_dir)}

        # Add schema to config after it's determined (will add after schema loading)
        table = create_config_table("", config_info)
        console.print(table)

    # Load schema (enforced as required by Click)
    matched_schema = Path(schema)
    if not matched_schema.exists():
        print_error("Schema not found", matched_schema.as_posix())
        sys.exit(1)

    try:
        from streamline_extract.utils.schema_metadata import SchemaMetadata

        runtime_artifact = _resolve_runtime_artifact(None, matched_schema)
        metadata_overrides = {}
        if runtime_artifact:
            pack_consolidation = (
                (runtime_artifact.get("resolved") or {}).get("pack") or {}
            ).get("consolidation")
            if isinstance(pack_consolidation, dict):
                metadata_overrides["consolidation"] = pack_consolidation

        schema_metadata = SchemaMetadata(
            matched_schema, metadata_overrides=metadata_overrides or None
        )

        # Add schema to config display after successful load
        if VERBOSITY != "quiet" and not emit_json_report:
            # Show relative path for schema
            try:
                schema_rel = matched_schema.relative_to(Path.cwd())
                schema_display = str(schema_rel)
            except ValueError:
                schema_display = str(matched_schema)

            console.print(f"  [bold]Schema[/bold]      {schema_display}")
            console.print(
                f"  [bold]Runtime[/bold]     {_format_runtime_artifact_summary(runtime_artifact)}"
            )
            if metadata_overrides:
                console.print(
                    "  [bold]Overrides[/bold]   pack-owned consolidation config active"
                )
            console.print()
    except Exception as e:
        print_error(
            "Schema validation failed",
            f"Schema {matched_schema.name} is missing required $metadata section: {e}\\n"
            "StreamlineExtract v2.0+ requires schemas with $metadata.\\n"
            "See schemas/SCHEMA_BEST_PRACTICES.md for examples.",
        )
        return

    # Consolidate
    if VERBOSITY != "quiet" and not emit_json_report:
        console.print("[cyan]→[/cyan] Analyzing schema structure...")
    consolidator = Consolidator(
        schema_metadata=schema_metadata,
        verbose=(VERBOSITY == "verbose" or VERBOSITY == "debug"),
        debug=(VERBOSITY == "debug"),
    )

    try:
        df, schema_info = consolidator.consolidate_from_directory(
            input_dir,
            apply_deduplication=not dry_run,
        )

        if df.empty:
            print_warning("No data found to consolidate")
            return

        if VERBOSITY != "quiet" and not emit_json_report:
            print_success(
                f"Schema detected: [cyan]{schema_info['type']}[/cyan]"
            )
            console.print(
                f"  [dim]Main entity: {schema_info['main_array_key']}[/dim]\n"
            )

        if dry_run:
            dedup_preview = consolidator.deduplicator.preview_deduplication(df)
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

            if emit_json_report or VERBOSITY == "quiet":
                click.echo(
                    json.dumps(preview_report, indent=2, sort_keys=True)
                )
            else:
                console.print("[cyan]→[/cyan] Previewing deduplication...")
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
                table = create_summary_table(
                    "Deduplication Preview", preview_stats
                )
                console.print()
                console.print(table)

                warnings = preview_report["warnings"]
                for warning in warnings:
                    print_warning(warning)

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
                    group_label = f"Preview {index}"
                    if group.get("suspicious"):
                        group_label += (
                            f" [suspicious:{group.get('severity', 'low')}]"
                        )
                    console.print(
                        f"  [yellow]{group_label}[/yellow] keep row {group['keep_index']} | "
                        f"drop {group['drop_indices']} | {sample_values}"
                    )
                    console.print(f"    [dim]{group['note']}[/dim]")
                    if group.get("conflicting_columns"):
                        console.print(
                            f"    [red]Conflicts:[/red] {', '.join(group['conflicting_columns'])}"
                        )

                if len(preview_report["duplicate_groups"]) > 5:
                    console.print(
                        f"  [dim]... {len(preview_report['duplicate_groups']) - 5} more duplicate group(s) omitted[/dim]"
                    )

                console.print()
                print_info(
                    "Dry run complete - no CSV/Excel files were written"
                )

            if preview_report["would_fail_on_suspicious"]:
                if not emit_json_report and VERBOSITY != "quiet":
                    print_error(
                        "Suspicious deduplication threshold exceeded",
                        f"Dry-run found suspicious groups at or above '{fail_on_suspicious}' severity",
                    )
                sys.exit(2)
            return

        if VERBOSITY != "quiet":
            console.print("[cyan]→[/cyan] Creating outputs...")

        # Generate output filename
        base_name = input_dir.name.replace("_", "-")
        output_formats = _resolve_consolidation_output_formats(
            metadata_overrides or None
        )
        emitted_paths: List[Path] = []

        if "csv" in output_formats:
            csv_path = output_dir / f"{base_name}.csv"
            consolidator.save_csv(df, csv_path)
            emitted_paths.append(csv_path)
            csv_size_mb = csv_path.stat().st_size / (1024 * 1024)

            if VERBOSITY == "verbose" or VERBOSITY == "debug":
                print_success(
                    f"CSV saved: {csv_path.name} ({csv_size_mb:.2f} MB, {len(df)} rows)"
                )
            elif VERBOSITY != "quiet":
                print_success(f"CSV saved ({len(df)} rows)")

        if "excel" in output_formats:
            excel_path = output_dir / f"{base_name}.xlsx"
            consolidator.save_excel(df, excel_path)
            emitted_paths.append(excel_path)
            excel_size_mb = excel_path.stat().st_size / (1024 * 1024)

            if VERBOSITY == "verbose" or VERBOSITY == "debug":
                print_success(
                    f"Excel saved: {excel_path.name} ({excel_size_mb:.2f} MB, {len(df)} rows)"
                )
            elif VERBOSITY != "quiet":
                print_success(
                    "Excel saved (clean formatting, auto-sized columns)"
                )

        # Summary
        if VERBOSITY != "quiet":
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

            console.print()
            table = create_summary_table(
                "Consolidation Summary", summary_stats
            )
            console.print(table)

            console.print("\n[bold green]✓ Output Location[/bold green]")
            for emitted_path in emitted_paths:
                console.print(f"  [bold]{emitted_path.absolute()}[/bold]")
            console.print()
        else:
            # Quiet mode - print emitted output path(s)
            for emitted_path in emitted_paths:
                console.print(str(emitted_path.absolute()))

    except Exception as e:
        print_error("Consolidation failed", str(e))
        if VERBOSITY == "debug":
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
        streamline-extract compare processed/qa_qc_test/qa_qc --schema schemas/personal/geothermal_ordinance_schema.json

        # Compare specific document folder
        streamline-extract compare "processed/qa_qc_test/qa_qc/Chaffee County Colorado" --schema schemas/personal/geothermal_ordinance_schema.json

    \b
    OUTPUT (per document):
        • comparison_report.xlsx - Color-coded Excel with agreement analysis
        • comparison_report.csv - Plain CSV for data analysis

    \b
    WORKFLOW:
        1. Run extraction with QA/QC: streamline-extract process docs/ --schema schema.json --enable-qa-qc
        2. Generate/update reports: streamline-extract compare processed/docs/qa_qc --schema schema.json
    """
    from streamline_extract.qa_qc import ComparisonEngine, ReportGenerator
    from streamline_extract.qa_qc.utils import resolve_qaqc_runtime_config
    from streamline_extract.utils.schema_metadata import SchemaMetadata

    qa_qc_path = Path(qa_qc_path)
    schema_path = Path(schema)

    # Set verbosity
    if quiet:
        verbosity = "quiet"
    elif verbose:
        verbosity = "verbose"
    else:
        verbosity = "normal"

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
        print_error("Failed to load schema", str(e))
        sys.exit(1)

    # Display header
    if verbosity != "quiet":
        print_header("QA/QC COMPARISON REPORT")

        config_info = {
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
        table = create_config_table("Configuration", config_info)
        console.print(table)
        console.print()

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

    if verbosity != "quiet":
        console.print("[dim]" + "─" * 60 + "[/dim]")
        console.print()

    for doc_dir in doc_dirs:
        # Find model output files
        model_files = {}
        for f in doc_dir.glob("*.json"):
            if f.name in ignored_qaqc_json_files:
                continue
            model_name = f.stem
            model_files[model_name] = f

        if len(model_files) < 2:
            if verbosity != "quiet":
                console.print(
                    f"[yellow]⚠[/yellow] Skipping {doc_dir.name}: needs at least 2 model outputs"
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

            if verbosity != "quiet":
                agreement_pct = result.summary.get("full_agreement_pct", 0)
                needs_review = result.summary.get("needs_review_count", 0)
                total = result.summary.get("total_comparisons", 0)

                # Color code based on agreement
                if agreement_pct >= 80:
                    status = "[green]✓[/green]"
                elif agreement_pct >= 50:
                    status = "[yellow]⚠[/yellow]"
                else:
                    status = "[red]![/red]"

                console.print(f"{status} {doc_dir.name}")
                console.print(
                    f"    Agreement: [bold]{agreement_pct:.1f}%[/bold] ({total - needs_review}/{total} fields)"
                )
                console.print(f"    Needs review: {needs_review} field(s)")
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
                    console.print(
                        f"    Qualitative advisory gate: [bold]{gate_status}[/bold] "
                        f"({aligned_pct:.1f}% aligned, {missing_pct:.1f}% missing items)"
                    )
                    if excluded_scope_variants:
                        console.print(
                            f"    Excluded scope variants: {excluded_scope_variants} auxiliary row(s)"
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
                    console.print(
                        f"    Top missing-item categories: {summary_text}"
                    )
                if scope_variant_categories:
                    summary_text = ", ".join(
                        f"{entry.get('label')} ({entry.get('count')})"
                        for entry in scope_variant_categories[:3]
                    )
                    console.print(
                        f"    Top scope-variant categories: {summary_text}"
                    )
                if text_categories:
                    summary_text = ", ".join(
                        f"{entry.get('label')} ({entry.get('count')})"
                        for entry in text_categories[:3]
                    )
                    console.print(
                        f"    Top text-difference categories: {summary_text}"
                    )
                console.print()

        except Exception as e:
            results.append(
                {
                    "name": doc_dir.name,
                    "success": False,
                    "error": str(e),
                }
            )
            if verbosity != "quiet":
                console.print(f"[red]✗[/red] {doc_dir.name}: {str(e)[:50]}")

    # Summary
    if verbosity != "quiet":
        console.print("[dim]" + "─" * 60 + "[/dim]")
        console.print()

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
                summary_stats["Failed"] = f"[red]{len(failed)}[/red]"

            table = create_summary_table("Comparison Summary", summary_stats)
            console.print(table)
            console.print()

            print_success(f"Reports saved to: {qa_qc_path}")
            console.print(
                "[dim]  Files: comparison_report.xlsx, comparison_report.csv[/dim]"
            )
            console.print()
        else:
            print_warning("No documents were successfully compared")
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
    "--consolidation-baseline-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing expected consolidated CSV outputs for row-correctness scoring. Files should mirror benchmark CSV relative paths or filenames.",
)
@click.option(
    "--consolidation-schema",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Schema used to generate consolidated outputs. Required for consolidation correctness scoring.",
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
    "--min-consolidation-correctness",
    type=float,
    default=None,
    help="Minimum required consolidation correctness percentage (0-100) when expected consolidated CSVs are provided.",
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
    consolidation_baseline_dir: Optional[Path],
    consolidation_schema: Optional[Path],
    baseline_snapshot: Optional[Path],
    write_snapshot: Optional[Path],
    snapshot_label: Optional[str],
    min_extraction_parity: Optional[float],
    min_qaqc_signal_quality: Optional[float],
    min_qaqc_qualitative_pass_rate: Optional[float],
    min_consolidation_correctness: Optional[float],
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
    consolidation_baseline_dir = _coalesce_benchmark_option(
        consolidation_baseline_dir,
        profile_values,
        "consolidation_baseline_dir",
    )
    consolidation_schema = _coalesce_benchmark_option(
        consolidation_schema, profile_values, "consolidation_schema"
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
    min_consolidation_correctness = _coalesce_benchmark_option(
        min_consolidation_correctness,
        profile_values,
        "min_consolidation_correctness",
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
        min_consolidation_correctness is not None
        and consolidation_baseline_dir is None
    ):
        raise click.UsageError(
            "--min-consolidation-correctness requires --consolidation-baseline-dir"
        )
    if consolidation_baseline_dir is not None and consolidation_schema is None:
        raise click.UsageError(
            "--consolidation-baseline-dir requires --consolidation-schema"
        )

    metrics = collect_benchmark_metrics(
        benchmark_path,
        repo_root=Path.cwd(),
        extraction_baseline_dir=extraction_baseline_dir,
        qaqc_baseline_dir=qaqc_baseline_dir,
        consolidation_baseline_dir=consolidation_baseline_dir,
        consolidation_schema_path=consolidation_schema,
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
        min_consolidation_correctness=min_consolidation_correctness,
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

    print_header("PERFORMANCE BENCHMARK")
    config_info = {
        "Input": str(benchmark_path),
        "Run Manifests": str(metrics["manifest_count"]),
        "Documents": str(metrics["total_documents"]),
    }
    if gate_profile is not None:
        config_info["Gate Profile"] = str(gate_profile)
    console.print(create_config_table("Benchmark Input", config_info))
    console.print()

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
        "Consolidation Correctness": f"{metrics['consolidation_correctness']:.2f}%"
        if metrics["consolidation_correctness"] is not None
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
    console.print(create_summary_table("Performance Profile", summary_stats))

    if baseline_comparison is not None:
        console.print()
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
        console.print(
            create_summary_table("Baseline Comparison", baseline_stats)
        )

    if verbose and metrics["error_categories"]:
        console.print()
        console.print(
            create_summary_table(
                "Error Categories", metrics["error_categories"]
            )
        )

    if verbose and metrics["qaqc_qualitative_gate_counts"]:
        console.print()
        console.print(
            create_summary_table(
                "QA/QC Qualitative Gates",
                metrics["qaqc_qualitative_gate_counts"],
            )
        )

    if snapshot_path is not None:
        console.print()
        print_info(f"Snapshot written to: {snapshot_path}")

    if gate_result["gates"]:
        console.print()
        gate_stats = {}
        for gate_name, gate in gate_result["gates"].items():
            actual = gate["actual"]
            threshold = gate["threshold"]
            gate_stats[gate_name] = (
                f"{'PASS' if gate['passed'] else 'FAIL'} (actual={actual}, threshold={threshold})"
            )
        console.print(create_summary_table("Benchmark Gates", gate_stats))
        console.print()
        if gate_result["overall_passed"]:
            print_success("Benchmark gates passed")
        else:
            print_warning("Benchmark gates failed")
            sys.exit(1)
    else:
        console.print()
        print_info("No thresholds supplied; reported metrics only")
