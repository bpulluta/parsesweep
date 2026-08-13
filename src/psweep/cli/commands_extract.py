"""`extract` command and helpers extracted from the legacy CLI monolith."""

from __future__ import annotations

import csv
import contextlib
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from dotenv import load_dotenv

from psweep.cli.dashboard import create_live_dashboard
from psweep.cli.run_view import RunView
from psweep.cli.ui import (
    Verbosity,
    ask_confirm,
    console,
    create_extraction_progress,
    get_verbosity,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from psweep.config import RuntimeConfigError
from psweep.extraction import DocumentExtractor, load_schema
from psweep.extraction.document_utils import (
    SUPPORTED_EXTENSIONS,
    extract_text_from_document,
    is_supported_document,
)
from psweep.extraction.llm_factory import DEFAULT_MAX_CONTEXT, DEFAULT_MODEL
from psweep.validation.utils import sanitize_model_name
from psweep.utils.config import get_config
from psweep.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)


def _format_extraction_accounting(
    num_docs: int,
    total_cost_usd: float,
    total_llm_calls: int,
    total_time_sec: float,
    model: str,
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


def _generate_run_id(
    *,
    schema_path: Path,
    provider: str,
    model: str,
    enable_validation: bool,
    doc_files: List[Path],
    artifact_id: Optional[str],
) -> str:
    """Generate a deterministic run identifier for lineage joins."""
    seed = {
        "schema": schema_path.as_posix(),
        "provider": provider,
        "model": model,
        "mode": "validation" if enable_validation else "single_model",
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
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a deterministic run manifest payload for process executions."""
    lineage = (runtime_artifact or {}).get("lineage") or {}
    manifest_errors = summarize_error_records(
        error
        for result in (failed_results or [])
        for error in result.get("errors", [])
    )
    manifest = {
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
    if costs:
        manifest["costs"] = costs
    return manifest


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
    except Exception:
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
    """Map each discovered document's filename to its origin URL + queried target."""
    index_path: Optional[Path] = None

    if from_index:
        candidate = Path(from_index)
        if candidate.is_file():
            index_path = candidate

    if index_path is None and input_path is not None:
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
    """Prepend a CONTEXT block (source URL + queried site) to document text."""
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
        trigger_chars=trigger_chars,
        max_selected_pages=int(
            config.get("max_selected_pages", DEFAULT_MAX_SELECTED_PAGES)
            or DEFAULT_MAX_SELECTED_PAGES
        ),
        keywords=config.get("keywords"),
    )

    discovered: list[tuple[str, int, int]] = []

    for doc in doc_files:
        if doc.suffix.lower() != ".pdf":
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
    enable_validation: bool,
    runtime_artifact,
    run_id,
    provider,
    schema_path: Path,
    identifier_fields=None,
) -> dict:
    """Extract one document and persist its record."""
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
            enable_validation,
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
    default=DEFAULT_MODEL,
    show_default=True,
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
    "--profile",
    "profile_name",
    default="default",
    show_default=True,
    help="Runtime profile to compile into artifact lineage (for example: default, dev, staging, prod)",
)
@click.option("--limit", "-n", type=int, help="Process only first N files")
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    show_default=True,
    help="Re-extract all files (default: skip files already processed)",
)
@click.option(
    "--max-context",
    type=int,
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
    filter_state: Optional[str] = None,
    filter_jurisdiction: Optional[str] = None,
):
    """Extract structured data from documents using an LLM.

    Reads PDF, DOCX, TXT, and XLSX files from PATH (or the configured
    input_dir) and writes one JSON file per document to the output directory.
    Already-processed files are skipped by default; pass --fresh to re-extract
    everything. Output defaults to ``extracted/<domain>/`` when --output is
    omitted; run ``psweep extract --help`` for every option and its default.

    Examples
    --------
    ::

        psweep extract docs/ --schema schemas/my_schema.json
        psweep extract --config config/my_domain/my_domain.yaml --fresh
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
            "profile_name",
            "provider",
            "model",
            "limit",
            "max_context",
            "live_dashboard",
            "fresh",
        ]
    )

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
    skip_existing = resolved_inputs.get("skip_existing", True)
    if fresh:
        skip_existing = False
    max_context = resolved_inputs.get("max_context", max_context)
    timeout_seconds = resolved_inputs.get("timeout_seconds")
    live_dashboard = resolved_inputs.get("live_dashboard", live_dashboard)

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

    config = get_config()
    path = Path(path)
    is_dir = path.is_dir()

    if is_dir:
        category = path.name
    else:
        category = path.parent.name

    def _discover_domain(p: Path) -> Optional[str]:
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
            output_dir = Path.cwd() / "extracted" / disc_domain
        else:
            parts = list(probe.parts)
            if "documents" in parts:
                idx = parts.index("documents")
                parts[idx] = "extracted"
                output_dir = Path(*parts)
            else:
                output_dir = Path.cwd() / "extracted" / probe.name

    output_dir.mkdir(parents=True, exist_ok=True)

    file_output_dirs = {}

    def _not_sidecar(p: Path) -> bool:
        return not any(part.startswith(".") for part in p.relative_to(path).parts)

    if is_dir:
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(path.glob(f"*{ext}")))
        doc_files = sorted(f for f in doc_files if _not_sidecar(f))

        if not doc_files:
            for ext in SUPPORTED_EXTENSIONS:
                doc_files.extend(sorted(path.rglob(f"*{ext}")))
            doc_files = sorted(f for f in doc_files if _not_sidecar(f))

            if doc_files and not get_verbosity().is_quiet:
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
                    for sdir in sorted(subdirs_found):
                        sdir_docs = [
                            d
                            for d in doc_files
                            if d.relative_to(path).parts[0] == sdir
                        ]
                        view.status(
                            "info", f"{sdir}: {len(sdir_docs)} document(s)"
                        )

        for doc in doc_files:
            try:
                rel_parent = doc.parent.relative_to(path)
                file_output_dirs[doc] = output_dir / rel_parent
            except ValueError:
                file_output_dirs[doc] = output_dir
            file_output_dirs[doc].mkdir(parents=True, exist_ok=True)

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
                    f"Skipping {skipped} already processed file{'s' if skipped != 1 else ''} (use --fresh to extract again)"
                )
    else:
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

    if from_index and _from_index_data.get("doc_files"):
        doc_files = _from_index_data["doc_files"]
        if limit:
            doc_files = doc_files[:limit]
        if not output:
            output_dir = Path("extracted") / _from_index_data["domain"]
            output_dir.mkdir(parents=True, exist_ok=True)
            category = _from_index_data["domain"]
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
                    f'Skipping {skipped} already processed file{"s" if skipped != 1 else ""} (use --fresh to extract again)'
                )
        if not doc_files:
            print_error(
                "No new files to process from download index",
                "All discovered files have already been processed. Use --fresh to extract again.",
            )
            sys.exit(0)
        if not get_verbosity().is_quiet:
            print_info(
                f"From-index mode: {len(doc_files)} file(s) loaded from {from_index}"
            )

    from psweep.utils.page_range import (
        load_pages_csv,
        parse_page_range,
    )

    page_range_map = {}

    if pages_csv:
        try:
            page_mappings = load_pages_csv(Path(pages_csv))
            for doc in doc_files:
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

    if not doc_files:
        if is_dir and not view.is_quiet:
            view.header("DOCUMENT EXTRACTION")
            view.success(f"All {original_count} file(s) already processed")
            view.outputs({"Output directory": str(output_dir)})
            view.next_steps(
                ["Re-extract everything with the --fresh flag"]
            )
        return

    view.header("DOCUMENT EXTRACTION")
    if not view.is_quiet:
        try:
            rel_input = path.relative_to(Path.cwd())
            input_display = str(rel_input)
        except ValueError:
            input_display = str(path)

        config_info = {
            "Input": input_display,
            "Files": f"{len(doc_files)} document{'s' if len(doc_files) != 1 else ''}",
        }

    provider_name = config.llm_config.get("provider", "unknown").title()
    if model and resolved_inputs.get("models"):
        from psweep.extraction.llm_factory import resolve_model_name

        model_display = resolve_model_name(
            model, models=resolved_inputs.get("models"),
            llm_config=config.llm_config,
        )
    else:
        model_display = config.llm_config.get("model", model)

    if not get_verbosity().is_quiet:
        config_info["Model"] = model_display
        config_info["Provider"] = provider_name
        if page_range_map:
            files_with_ranges = sum(
                1 for v in page_range_map.values() if v is not None
            )
            if files_with_ranges == 1 and len(doc_files) == 1:
                start, end = list(page_range_map.values())[0]
                if pages_csv:
                    try:
                        csv_rel = Path(pages_csv).relative_to(Path.cwd())
                        config_info["Pages"] = f"{csv_rel} ({start}-{end})"
                    except ValueError:
                        config_info["Pages"] = f"{pages_csv} ({start}-{end})"
                else:
                    config_info["Pages"] = f"{start}-{end}"
            elif files_with_ranges > 0:
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

        try:
            rel_output = output_dir.relative_to(Path.cwd())
            config_info["Output"] = str(rel_output)
        except ValueError:
            config_info["Output"] = str(output_dir)

    schema_path = _resolve_schema_ref(Path(schema))
    if not schema_path.exists():
        print_error("Schema not found", schema_path.as_posix())
        sys.exit(1)
    loaded_schema = load_schema(schema_path)
    runtime_artifact = None
    if not get_verbosity().is_quiet:
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

    if not view.is_quiet:
        view.config(config_info)

    if len(doc_files) > 10 and not view.is_quiet:
        sample_size = min(3, len(doc_files))
        total_chars = 0
        for doc in doc_files[:sample_size]:
            try:
                page_range = page_range_map.get(doc)
                text = extract_text_from_document(doc, page_range=page_range)
                total_chars += len(text)
            except Exception:
                pass

        if total_chars > 0:
            avg_chars = total_chars / sample_size
            estimated_total_chars = avg_chars * len(doc_files)
            estimated_tokens = int(estimated_total_chars / 4)

            from psweep.extraction.llm_factory import resolve_model_name
            from psweep.utils.model_pricing import get_model_pricing

            est_model = resolve_model_name(
                resolved_inputs.get("model"),
                models=resolved_inputs.get("models"),
                llm_config=config.llm_config,
            )
            input_rate, output_rate = get_model_pricing(est_model)
            input_cost = (estimated_tokens / 1_000_000) * input_rate
            output_cost = (estimated_tokens * 0.1 / 1_000_000) * output_rate
            total_est_cost = input_cost + output_cost

            if total_est_cost > 1.0:
                view.warning(
                    f"Estimated cost: ${total_est_cost:.2f}",
                    f"Processing {len(doc_files)} documents "
                    f"with ~{estimated_tokens:,} tokens",
                )
                if not ask_confirm("Proceed with extraction?", default=True):
                    view.info("Operation cancelled")
                    return

    config = get_config()
    if provider == "auto":
        provider = config.llm_config.get("provider", "openai")

    api_key = config.llm_config.get("api_key")
    if not api_key:
        print_error(
            f"{provider.upper()} API key not found",
            f"Configure {provider.upper()}_API_KEY in .env file",
        )
        return

    if "model" in resolved_inputs:
        from psweep.extraction.llm_factory import resolve_llm_kwargs

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

    if not actual_model:
        raise click.UsageError(
            "No model configured for the extraction stage. "
            "Add a 'models:' block and 'model: primary' under 'extraction:' "
            "in your run config, or set AZURE_OPENAI_MODEL in your .env file."
        )

    run_id = _generate_run_id(
        schema_path=schema_path,
        provider=provider,
        model=actual_model,
        enable_validation=False,
        doc_files=doc_files,
        artifact_id=runtime_artifact.get("artifact_id")
        if runtime_artifact
        else None,
    )
    run_started_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

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

    identifier_fields = None
    if schema_metadata is not None:
        try:
            declared = schema_metadata.get_identifier_fields() or []
            identifier_fields = [p.split(".")[-1] for p in declared] or None
        except Exception:
            identifier_fields = None

    context_windows = resolved_inputs.get("model_context_windows") or None
    enable_validation = False  # Regular extract command is single-model only
    extractor = DocumentExtractor(
        api_key=api_key,
        model=actual_model,
        max_context_chars=max_context,
        schema_metadata=schema_metadata,
        provider=provider,
        azure_endpoint=azure_endpoint,
        azure_api_version=azure_api_version,
        context_windows=context_windows,
        timeout=timeout_seconds,
    )

    view.phase("Extracting documents")
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
            enable_validation=enable_validation,
            runtime_artifact=runtime_artifact,
            run_id=run_id,
            provider=provider,
            schema_path=schema_path,
            identifier_fields=identifier_fields,
        )

    if live_dashboard and len(doc_files) > 1 and not view.is_quiet:
        # Enhanced live dashboard — available for any multi-doc run via
        # --live-dashboard (threshold gate removed).
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

    elif len(doc_files) > 1 and not view.is_quiet:
        # Progress bar for every multi-doc run, with inline phase label and
        # per-doc result rows printed above the bar as each doc finishes.
        progress = create_extraction_progress()
        task = progress.add_task(
            _document_progress_desc(doc_files[0], page_range_map),
            total=len(doc_files),
            phase="",
        )

        with progress:
            for idx, doc_path in enumerate(doc_files):
                if idx > 0:
                    progress.update(
                        task,
                        description=_document_progress_desc(
                            doc_path, page_range_map
                        ),
                        phase="",
                    )

                progress.update(task, phase=f"→ {actual_model}")
                res = _run(doc_path)
                progress.update(task, phase="", advance=1)

                results.append(res)
                if res["success"]:
                    total_cost += res["cost"]
                    total_time += res["time"]
                    console.print(
                        f"  [green]✓[/green] {doc_path.name}"
                        f"  [dim]→[/dim]  {res['items']} items"
                        f"  [dim]•[/dim]  [magenta]${res['cost']:.4f}[/magenta]"
                        f"  [dim]•[/dim]  [dim]{res['time']:.1f}s[/dim]"
                    )
                else:
                    console.print(
                        f"  [red]✗[/red] {doc_path.name}"
                        f"  [dim]→[/dim]  [red]Error:[/red] {res['error'][:60]}"
                    )
    else:
        for doc_path in doc_files:
            if not view.is_quiet:
                page_range = page_range_map.get(doc_path)
                spinner_msg = f"[cyan]Extracting {doc_path.name}"
                if page_range is not None:
                    start, end = page_range
                    spinner_msg += f" [dim](pages {start}-{end})[/dim]"
                spinner_msg += "...[/cyan]"
                with console.status(spinner_msg, spinner="dots"):
                    res = _run(doc_path)
            else:
                res = _run(doc_path)

            results.append(res)
            if res["success"]:
                total_cost += res["cost"]
                total_time += res["time"]
                if not view.is_quiet:
                    console.print(
                        f"  [green]✓[/green] {doc_path.name}"
                        f"  [dim]→[/dim]  {res['items']} items"
                        f"  [dim]•[/dim]  [magenta]${res['cost']:.4f}[/magenta]"
                        f"  [dim]•[/dim]  [dim]{res['time']:.1f}s[/dim]"
                    )
            elif not view.is_quiet:
                console.print(
                    f"  [red]✗[/red] {doc_path.name}"
                    f"  [dim]→[/dim]  [red]Error:[/red] {res['error'][:60]}"
                )

    run_finished_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    successful_output_paths = [
        Path(r["output_path"]) for r in successful if r.get("output_path")
    ]
    total_input_tokens = sum(r.get("input_tokens") or 0 for r in successful)
    total_output_tokens = sum(r.get("output_tokens") or 0 for r in successful)
    run_costs = {
        "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "model": actual_model,
        "documents_billed": len(successful),
    }
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
            costs=run_costs,
        )
        manifest_path = _write_run_manifest(output_dir, run_id, run_manifest)
        if view.verbosity.shows_detail:
            view.status("info", f"Run manifest: {manifest_path.as_posix()}")
    except Exception as exc:
        view.warning(f"Run manifest write failed: {exc}")

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

        # Display concise extraction accounting
        if successful and not view.is_quiet:
            accounting_line = _format_extraction_accounting(
                num_docs=len(successful),
                total_cost_usd=total_cost,
                total_llm_calls=len(results),  # Approximation: total calls across all docs
                total_time_sec=total_time,
                model=actual_model,
            )
            console.print(accounting_line)

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
    from psweep.cli.commands import ask_confirm as shared_ask_confirm

    qa_model_defs = registry.get_models(list(validation_models))
    qa_models = [definition.model for definition in qa_model_defs]
    seed_output_dir = seed_output_dir or output_dir
    if len(qa_model_defs) < 2:
        hint = (
            f"resolved to: {qa_models}" if qa_models else "resolved to nothing — validation.models is empty or missing"
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
        if prompt_confirm and not shared_ask_confirm(
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
    id_declared = (
        loaded_schema.get("$metadata", {})
        .get("extraction", {})
        .get("identifier_fields")
        or []
    )
    primary_identifier_fields = [p.split(".")[-1] for p in id_declared] or None

    progress = view.make_progress()
    task_id = None
    if progress is not None:
        task_id = progress.add_task(
            "Extracting QA/QC", total=len(doc_files), phase=""
        )

    def _update_phase(message: str) -> None:
        if progress is not None and task_id is not None:
            progress.update(task_id, phase=message)

    progress_ctx = progress if progress is not None else contextlib.nullcontext()
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
                text = extract_text_from_document(doc_path, page_range=page_range)
                seed_records_by_model = {}
                if skip_existing:
                    canonical_path = seed_output_dir / f"{doc_path.stem}.json"
                    if canonical_path.exists():
                        try:
                            with open(canonical_path, encoding="utf-8") as fh:
                                existing_record = json.load(fh)
                            existing_model = (
                                (existing_record.get("lineage") or {}).get("model")
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
                                qa_doc_dir / f"{sanitize_model_name(model_name)}.json"
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
                        _update_phase(
                            f"→ done {model_name} ${float(event.get('cost', 0.0)):.4f}"
                        )
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
                    _extract_and_save_result(
                        doc_path=doc_path,
                        result=primary_result.result,
                        output_dir=seed_output_dir,
                        category=seed_output_dir.name,
                        model=primary_model,
                        validation_enabled=True,
                        runtime_artifact=runtime_artifact,
                        run_id=run_id,
                        provider=qa_provider,
                        schema_id=schema_path.as_posix(),
                        identifier_fields=primary_identifier_fields,
                    )

                doc_cost = sum(r.cost for r in model_results.values())
                doc_time = sum(r.processing_time for r in model_results.values())
                successful = sum(1 for r in model_results.values() if r.success)
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
        summary_stats["Total Cost"] = f"${total_cost:.4f}"
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
            [f"Rebuild QA/QC reports with validate (no re-extraction): {validate_compare_command}"]
        )

    return output_dir / "validation"


def _extract_and_save_result(
    doc_path: Path,
    result,
    output_dir: Path,
    category: str,
    model: str,
    validation_enabled: bool,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    provider: Optional[str] = None,
    schema_id: Optional[str] = None,
    identifier_fields: Optional[List[str]] = None,
) -> int:
    """Helper to extract items count and save result to JSON."""
    num_items = 0
    identifier = "N/A"
    id_field_names = identifier_fields or ["id", "identifier", "number", "name"]

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
