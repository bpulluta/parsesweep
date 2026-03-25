"""
Core workflow CLI commands for StreamlineExtract.

This module contains the main data processing pipeline commands:
- process: Extract structured data from documents to JSON
- validate: Validate extraction results against schema
- consolidate: Merge JSON files into Excel/CSV

For utility commands (init, preview, estimate, etc.), see utils_commands.py
"""

import json
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import click
from dotenv import load_dotenv
from rich.logging import RichHandler

from streamline_extract.utils.config import get_config
from streamline_extract.extraction import DocumentExtractor, load_schema
from streamline_extract.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS
)
from streamline_extract.consolidation.cleaner import ExtractionCleaner
from streamline_extract.consolidation.consolidator import Consolidator
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
from streamline_extract.core import compile_runtime_artifact, ArtifactCompilerError


# Global verbosity level (set by CLI flags)
VERBOSITY = 'normal'  # 'quiet', 'normal', 'verbose', 'debug'


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
    if verbosity == 'quiet':
        level = logging.ERROR
        show_level = False
        show_path = False
    elif verbosity == 'debug':
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
        markup=True
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
    azure_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    
    if azure_key and azure_endpoint:
        return 'azure', True, None
    
    # Fall back to OpenAI
    openai_key = os.getenv('OPENAI_API_KEY')
    if openai_key:
        return 'openai', True, None
    
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


def _resolve_runtime_artifact(
    category: Optional[str],
    schema_path: Path,
    profile_name: str = 'default',
    *,
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a compiled runtime artifact for lineage emission when available."""
    root = repo_root or Path(__file__).resolve().parents[3]
    packs_root = domain_packs_dir or (root / 'schemas/domain_packs')
    profiles_root = profiles_dir or (root / 'schemas/profiles')

    if not packs_root.exists() or not profiles_root.exists():
        return None

    candidate_pack_refs: List[str] = []
    if category:
        candidate_pack_refs.append(category)
    candidate_pack_refs.append(schema_path.stem)

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
        'schema': schema_path.as_posix(),
        'provider': provider,
        'model': model,
        'mode': 'qa_qc' if enable_qa_qc else 'single_model',
        'documents': sorted(path.as_posix() for path in doc_files),
        'artifact_id': artifact_id,
    }
    canonical = json.dumps(seed, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return f"run://{digest[:16]}"


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
) -> Dict[str, Any]:
    """Build a deterministic run manifest payload for process executions."""
    lineage = (runtime_artifact or {}).get('lineage') or {}
    return {
        'manifest_version': '1.0.0',
        'run_id': run_id,
        'mode': mode,
        'lineage': {
            'artifact_id': (runtime_artifact or {}).get('artifact_id') or 'artifact://runtime/unresolved',
            'profile_id': lineage.get('profile_id') or 'default',
            'schema_id': schema_path.as_posix(),
            'provider': provider,
            'model': model,
        },
        'documents': sorted(path.as_posix() for path in doc_files),
        'outputs': {
            'records': sorted(path.as_posix() for path in successful_output_paths),
        },
        'timing': {
            'started_at': started_at,
            'finished_at': finished_at,
        },
        'status': {
            'total_processed': total_processed,
            'successful': successful_count,
            'failed': failed_count,
            'result': 'success' if failed_count == 0 else 'partial_failure',
        },
    }


def _write_run_manifest(output_dir: Path, run_id: str, manifest: Dict[str, Any]) -> Path:
    """Persist a run manifest in the run_manifests output folder."""
    manifest_dir = output_dir / 'run_manifests'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    run_suffix = run_id.replace('run://', '')
    manifest_path = manifest_dir / f"{run_suffix}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding='utf-8',
    )
    return manifest_path


@click.command()
@click.argument('path', type=click.Path())
@click.option('--output', '-o', type=click.Path(), help='Output directory (auto-detected if not specified)')
@click.option('--schema', '-s', type=click.Path(exists=True), required=True, help='Path to JSON schema file (REQUIRED)')
@click.option('--category', help='Category name (auto-detected from path if not specified)')
@click.option('--model', default='gpt-4o-mini', show_default=True, help='AI model (e.g., gpt-4o-mini, claude-3.5-sonnet, gemini-1.5-pro)')
@click.option('--provider', type=click.Choice(['openai', 'azure', 'anthropic', 'gemini', 'auto'], case_sensitive=False), default='auto', show_default=True, help='LLM provider (auto-detects from .env)')
@click.option('--enable-qa-qc', is_flag=True, help='Enable multi-model QA/QC validation')
@click.option('--limit', '-n', type=int, help='Process only first N files')
@click.option('--skip-existing/--reprocess', default=True, show_default=True, help='Skip files already processed')
@click.option('--max-context', type=int, default=400000, show_default=True, help='Max document characters to process')
@click.option('--quiet', '-q', is_flag=True, help='Minimal output (machine-readable)')
@click.option('--verbose', '-v', is_flag=True, help='Detailed output with statistics')
@click.option('--debug', is_flag=True, help='Debug mode with full logs')
@click.option('--live-dashboard', is_flag=True, help='Show live dashboard during extraction')
@click.option('--pages', type=str, default=None, help='Page range to extract (e.g., "615-759"). Only for single PDF files.')
@click.option('--pages-csv', type=click.Path(exists=True), default=None, help='CSV file mapping documents to page ranges')
def process(path: str, output: Optional[str], schema: Optional[str], category: Optional[str],
            model: str, provider: str, enable_qa_qc: bool, limit: Optional[int], 
            skip_existing: bool, max_context: int, quiet: bool, verbose: bool, debug: bool, live_dashboard: bool,
            pages: Optional[str], pages_csv: Optional[str]):
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
        # Process with default model (gpt-4o-mini)
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json
        
        # Use Claude for high-quality extraction
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json --model claude-3.5-sonnet
        
        # Use Gemini for budget-friendly processing
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json --model gemini-1.5-flash
        
        # Use Azure OpenAI
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json --provider azure
        
        # Process with page ranges
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json --pages-csv config/tariffs/page_ranges.csv
        
        # Test with first 5 documents
        streamline-extract process documents/tariffs/ --schema schemas/proprietary/electricity_tariff_schema.json -n 5
    
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
        VERBOSITY = 'quiet'
    elif debug:
        VERBOSITY = 'debug'
    elif verbose:
        VERBOSITY = 'verbose'
    else:
        VERBOSITY = 'normal'
    
    # Configure logging with RichHandler for clean integration with progress bars
    configure_logging(VERBOSITY)
    
    # Load environment variables from .env file
    load_dotenv()
    
    # Validate path exists
    path = Path(path)
    if not path.exists():
        print_error(
            f"Path not found: {path}",
            f"The file or directory you specified doesn't exist.",
            [
                "Check the path spelling and try again",
                f"Current directory: {Path.cwd()}",
                "Use 'ls' or 'dir' to see available files and folders"
            ]
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
        if 'documents' in parts:
            idx = parts.index('documents')
            parts[idx] = 'processed'
            output_dir = Path(*parts)
        else:
            # Fallback: create in project root processed/
            project_root = Path.cwd()
            output_dir = project_root / 'processed' / path.name
    else:
        # Single file: parent directory logic
        parts = list(path.parent.parts)
        if 'documents' in parts:
            idx = parts.index('documents')
            parts[idx] = 'processed'
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / 'processed' / path.parent.name
    
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
            
            if doc_files and VERBOSITY != 'quiet':
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
                    console.print(f"\n[bold]📁 Found {len(doc_files)} document(s) across {len(subdirs_found)} subfolder(s)[/bold]")
                    # Show per-subfolder counts
                    for sdir in sorted(subdirs_found):
                        sdir_docs = [d for d in doc_files if d.relative_to(path).parts[0] == sdir]
                        console.print(f"  → {sdir}: {len(sdir_docs)} document(s)")
        
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
            supported_exts = ', '.join(sorted(SUPPORTED_EXTENSIONS))
            print_error(
                f"No supported documents found in: {path}",
                f"The directory exists but doesn't contain any supported document files.",
                [
                    f"Supported formats: {supported_exts}",
                    "Check if documents are in a subdirectory",
                    f"Use 'ls {path}' to see what's in this folder"
                ]
            )
            sys.exit(1)
        
        if limit:
            doc_files = doc_files[:limit]
        if skip_existing:
            original_count = len(doc_files)
            doc_files = [p for p in doc_files if not (file_output_dirs.get(p, output_dir) / f"{p.stem}.json").exists()]
            skipped = original_count - len(doc_files)
            if skipped > 0 and len(doc_files) > 0 and VERBOSITY != 'quiet':
                print_info(f"Skipping {skipped} already processed file{'s' if skipped != 1 else ''} (use --reprocess to extract again)")
    else:
        # Single file
        if not is_supported_document(path):
            supported_exts = ', '.join(sorted(SUPPORTED_EXTENSIONS))
            print_error(
                f"Unsupported file format: {path.name}",
                f"This tool works with: {supported_exts}",
                [
                    "Make sure the file has a supported extension",
                    "Check if you specified the correct file path"
                ]
            )
            sys.exit(1)
        doc_files = [path]
    
    # Handle page range specifications
    from streamline_extract.utils.page_range import parse_page_range, load_pages_csv
    
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
            
            if VERBOSITY != 'quiet':
                mapped_count = sum(1 for v in page_range_map.values() if v is not None)
                console.print(f"[dim]Loaded page ranges for {mapped_count} file(s) from CSV[/dim]")
        
        except Exception as e:
            print_error(
                "Invalid page ranges CSV",
                str(e),
                [
                    "CSV format should be:",
                    "  file_path,start_page,end_page",
                    "  tariff1.pdf,615,759",
                    "  tariff2.pdf,400,550"
                ]
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
                    "For multiple files, use --pages-csv instead"
                ]
            )
            sys.exit(1)
        
        if doc_files[0].suffix.lower() != '.pdf':
            print_error(
                "--pages only works with PDF files",
                f"File {doc_files[0].name} is not a PDF",
                ["Page ranges are only supported for PDF documents"]
            )
            sys.exit(1)
        
        try:
            page_range_tuple = parse_page_range(pages)
            page_range_map[doc_files[0]] = page_range_tuple
            
            if VERBOSITY != 'quiet':
                console.print(f"[dim]Extracting pages {page_range_tuple[0]}-{page_range_tuple[1]} only[/dim]")
        
        except ValueError as e:
            print_error(
                "Invalid page range format",
                str(e),
                [
                    "Use format like: --pages 615-759",
                    "Or with colons: --pages 100:200"
                ]
            )
            sys.exit(1)
    
    # Final validation - check if we have files to process
    if not doc_files:
        if is_dir and VERBOSITY != 'quiet':
            print_success(f"All {original_count} file(s) already processed!")
            console.print(f"[dim]Output directory: {output_dir}[/dim]")
            console.print(f"[dim]Use --reprocess to extract them again[/dim]\n")
        return
    
    # Display header and configuration
    if VERBOSITY != 'quiet':
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
    provider_name = config.llm_config.get('provider', 'unknown').title()
    model_display = config.llm_config.get('model', model)
    
    # Add model/provider info to config
    if VERBOSITY != 'quiet':
        config_info["Model"] = model_display
        config_info["Provider"] = provider_name
        config_info["QA/QC"] = "Enabled" if enable_qa_qc else "Disabled"
        
        # Show page range status
        if page_range_map:
            # Count how many files have page ranges
            files_with_ranges = sum(1 for v in page_range_map.values() if v is not None)
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
    
    # Load schema (enforced as required by Click)
    schema_path = Path(schema)
    loaded_schema = load_schema(schema_path)
    runtime_artifact = _resolve_runtime_artifact(category, schema_path)
    if VERBOSITY != 'quiet':
        # Show relative path for clarity
        try:
            schema_rel = schema_path.relative_to(Path.cwd())
            config_info["Schema"] = str(schema_rel)
        except ValueError:
            config_info["Schema"] = str(schema_path)

        if runtime_artifact:
            config_info["Artifact"] = runtime_artifact['artifact_id']
    
    # Display configuration table
    if VERBOSITY != 'quiet':
        table = create_config_table("Configuration", config_info)
        console.print(table)
        console.print()
    
    # Cost estimation and confirmation for large batches
    if len(doc_files) > 10 and VERBOSITY != 'quiet':
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
                console.print(f"\n[yellow]⚠ Cost Estimate:[/yellow] ${total_est_cost:.2f}")
                console.print(f"[dim]  Processing {len(doc_files)} documents with ~{estimated_tokens:,} tokens[/dim]\n")
                
                if not ask_confirm("Proceed with extraction?", default=True):
                    console.print("[yellow]Operation cancelled[/yellow]\n")
                    return
    
    # Load configuration
    config = get_config()
    
    # Determine provider from CLI flag or auto-detect from environment
    if provider == 'auto':
        provider = config.llm_config.get('provider', 'openai')
    
    # Get API credentials
    api_key = config.llm_config.get('api_key')
    if not api_key:
        print_error(
            f"{provider.upper()} API key not found",
            f"Configure {provider.upper()}_API_KEY in .env file"
        )
        return
    
    # Track the actual model being used for output
    actual_model = config.llm_config.get('model', model) if provider != 'openai' else model
    run_id = _generate_run_id(
        schema_path=schema_path,
        provider=provider,
        model=actual_model,
        enable_qa_qc=enable_qa_qc,
        doc_files=doc_files,
        artifact_id=runtime_artifact.get('artifact_id') if runtime_artifact else None,
    )
    run_started_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    
    # ========================================================================
    # QA/QC Multi-Model Extraction Mode
    # ========================================================================
    if enable_qa_qc:
        _run_qa_qc_extraction(
            doc_files=doc_files,
            loaded_schema=loaded_schema,
            output_dir=output_dir,
            api_key=api_key,
            provider=provider,
            config=config,
            max_context=max_context,
            page_range_map=page_range_map,
            verbosity=VERBOSITY,
            runtime_artifact=runtime_artifact,
            run_id=run_id,
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
            if VERBOSITY == 'verbose':
                console.print(f"[dim yellow]Could not load schema metadata: {e}[/dim yellow]")
    
    # Initialize extractor with clean configuration
    extractor = DocumentExtractor(
        api_key=api_key,
        model=actual_model,
        max_context_chars=max_context,
        schema_metadata=schema_metadata,
        provider=provider,
        azure_endpoint=config.llm_config.get('azure_endpoint'),
        azure_api_version=config.llm_config.get('azure_api_version'),
    )
    
    # Processing section
    if VERBOSITY == 'normal' or VERBOSITY == 'verbose':
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()
    
    results = []
    total_cost = 0.0
    total_time = 0.0
    
    # Use live dashboard for multiple files if requested
    if len(doc_files) > 3 and live_dashboard and VERBOSITY != 'quiet':
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
                        schema_id=loaded_schema.get('$id'),
                    )
                    
                    # Update dashboard
                    dashboard.complete_document(
                        doc_path.name,
                        success=True,
                        cost=result.cost,
                        input_tokens=0,  # Would need to track from extractor
                        output_tokens=0
                    )
                    
                    # Track results
                    results.append({
                        'file': doc_path.name,
                        'items': num_items,
                        'cost': result.cost,
                        'time': result.processing_time,
                        'output_path': (doc_output_dir / f"{doc_path.stem}.json").as_posix(),
                        'success': True
                    })
                    total_cost += result.cost
                    total_time += result.processing_time
                    
                except Exception as e:
                    dashboard.complete_document(doc_path.name, success=False)
                    results.append({
                        'file': doc_path.name,
                        'success': False,
                        'error': str(e)
                    })
    
    # Use progress bar for multiple files, simple output for single file
    elif len(doc_files) > 1 and VERBOSITY != 'quiet':
        progress = create_extraction_progress()
        # Start with first document name instead of generic "Extracting..." message
        first_doc = doc_files[0]
        first_desc = first_doc.name
        # Show page range in progress bar if specified
        if first_doc in page_range_map and page_range_map[first_doc] is not None:
            start, end = page_range_map[first_doc]
            first_desc += f" [dim](pages {start}-{end})[/dim]"
        
        task = progress.add_task(first_desc, total=len(doc_files))
        
        with progress:
            for idx, doc_path in enumerate(doc_files):
                # Update progress description to show current document (skip first since already set)
                if idx > 0:
                    desc = doc_path.name
                    if doc_path in page_range_map and page_range_map[doc_path] is not None:
                        start, end = page_range_map[doc_path]
                        desc += f" [dim](pages {start}-{end})[/dim]"
                    progress.update(task, description=desc)
                
                try:
                    # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                    # Use page range if specified for this file
                    page_range = page_range_map.get(doc_path)
                    text = extract_text_from_document(doc_path, page_range=page_range)
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
                        schema_id=loaded_schema.get('$id'),
                    )
                    
                    # Track results
                    results.append({
                        'file': doc_path.name,
                        'items': num_items,
                        'cost': result.cost,
                        'time': result.processing_time,
                        'output_path': (doc_output_dir / f"{doc_path.stem}.json").as_posix(),
                        'success': True
                    })
                    total_cost += result.cost
                    total_time += result.processing_time
                    
                except Exception as e:
                    results.append({
                        'file': doc_path.name,
                        'success': False,
                        'error': str(e)
                    })
                
                progress.update(task, advance=1)
    else:
        # Single file or quiet mode
        for doc_path in doc_files:
            if VERBOSITY == 'verbose' or VERBOSITY == 'normal':
                console.print()
                # Show page range if specified
                page_range = page_range_map.get(doc_path)
                if page_range is not None:
                    start, end = page_range
                    console.print(f"[cyan]→[/cyan] {doc_path.name} [dim](pages {start}-{end})[/dim]")
                else:
                    console.print(f"[cyan]→[/cyan] {doc_path.name}")
            
            try:
                # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                # Use page range if specified for this file
                page_range = page_range_map.get(doc_path)
                text = extract_text_from_document(doc_path, page_range=page_range)
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
                    schema_id=loaded_schema.get('$id'),
                )
                
                # Track results
                results.append({
                    'file': doc_path.name,
                    'items': num_items,
                    'cost': result.cost,
                    'time': result.processing_time,
                    'output_path': (doc_output_dir / f"{doc_path.stem}.json").as_posix(),
                    'success': True
                })
                total_cost += result.cost
                total_time += result.processing_time
                
                if VERBOSITY == 'verbose' or VERBOSITY == 'normal':
                    console.print(f"  [green]✓[/green] {num_items} items • [magenta]${result.cost:.4f}[/magenta] • [dim]{result.processing_time:.1f}s[/dim]")
                
            except Exception as e:
                results.append({
                    'file': doc_path.name,
                    'success': False,
                    'error': str(e)
                })
                if VERBOSITY != 'quiet':
                    console.print(f"  [yellow]✗[/yellow] [dim]Error: {str(e)[:60]}[/dim]")

    run_finished_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    successful = [r for r in results if r.get('success')]
    failed = [r for r in results if not r.get('success')]
    successful_output_paths = [Path(r['output_path']) for r in successful if r.get('output_path')]
    try:
        run_manifest = _build_run_manifest(
            run_id=run_id,
            mode='single_model',
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
        )
        manifest_path = _write_run_manifest(output_dir, run_id, run_manifest)
        if VERBOSITY == 'verbose':
            console.print(f"[dim]Run manifest: {manifest_path.as_posix()}[/dim]")
    except Exception as exc:
        if VERBOSITY != 'quiet':
            print_warning(f"Run manifest write failed: {exc}")
    
    # Summary
    if VERBOSITY != 'quiet':
        summary_stats = {
            "Processed": f"{len(results)} file{'s' if len(results) != 1 else ''}",
            "Successful": f"[green]{len(successful)}[/green]",
        }
        
        if failed:
            summary_stats["Failed"] = f"[yellow]{len(failed)}[/yellow]"
        
        if successful:
            avg_cost = total_cost / len(successful)
            avg_time = total_time / len(successful)
            summary_stats["Total Cost"] = f"[magenta]${total_cost:.4f}[/magenta]"
            summary_stats["Avg Cost/File"] = f"[magenta]${avg_cost:.4f}[/magenta]"
            summary_stats["Total Time"] = f"{total_time:.1f}s"
            summary_stats["Avg Time/File"] = f"{avg_time:.1f}s"
            
            total_items = sum(r.get('items', 0) or 0 for r in successful)
            if total_items > 0:
                summary_stats["Total Items"] = f"[green]{total_items}[/green]"
        
        console.print()
        table = create_summary_table("Extraction Summary", summary_stats)
        console.print(table)
        
        console.print()
        console.print(f"[bold green]✓ Results saved to:[/bold green]")
        console.print(f"  [bold]{output_dir.absolute()}[/bold]")
        console.print()


def _run_qa_qc_extraction(
    doc_files: List[Path],
    loaded_schema: dict,
    output_dir: Path,
    api_key: str,
    provider: str,
    config,
    max_context: int,
    page_range_map: dict,
    verbosity: str,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """
    Run QA/QC multi-model extraction for documents.
    
    This function handles the --enable-qa-qc flag by:
    1. Auto-detecting models from environment
    2. Confirming with user (Nx cost warning)
    3. Running extraction with multiple models
    4. Saving outputs to processed/qa_qc/{doc_name}/
    """
    from streamline_extract.qa_qc import ModelDetector, run_multi_model_extraction
    from streamline_extract.extraction.document_utils import extract_text_from_document
    
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
                "Or leave empty to use default models for your provider"
            ]
        )
        return
    
    # Show QA/QC configuration
    if verbosity != 'quiet':
        console.print()
        console.print("[bold cyan]━━━ QA/QC Multi-Model Validation Mode ━━━[/bold cyan]")
        console.print(f"  Provider: [bold]{qa_provider.upper()}[/bold]")
        console.print(f"  Models: [bold]{', '.join(qa_models)}[/bold]")
        console.print(f"  Documents: [bold]{len(doc_files)}[/bold]")
        console.print()
        console.print(f"[yellow]⚠ Cost Warning:[/yellow] This will run [bold]{len(qa_models)}x[/bold] extractions per document")
        console.print(f"[dim]  Total API calls: {len(doc_files)} docs × {len(qa_models)} models = {len(doc_files) * len(qa_models)} extractions[/dim]")
        console.print()
        
        if not ask_confirm("Proceed with QA/QC extraction?", default=True):
            console.print("[yellow]Operation cancelled[/yellow]\n")
            return
    
    # Process each document with multi-model extraction
    results = []
    total_cost = 0.0
    total_time = 0.0
    
    if verbosity != 'quiet':
        console.print()
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()
    
    for doc_idx, doc_path in enumerate(doc_files, 1):
        if verbosity != 'quiet':
            console.print(f"[cyan][{doc_idx}/{len(doc_files)}][/cyan] {doc_path.name}")
        
        try:
            # Extract text from document (respecting page ranges)
            page_range = page_range_map.get(doc_path)
            text = extract_text_from_document(doc_path, page_range=page_range)
            
            if verbosity == 'verbose':
                console.print(f"  [dim]Extracted {len(text):,} characters[/dim]")
            
            # Run multi-model extraction
            model_results = run_multi_model_extraction(
                doc_text=text,
                doc_name=doc_path.stem,
                schema=loaded_schema,
                models=qa_models,
                output_dir=output_dir,
                api_key=api_key,
                provider=qa_provider,
                azure_endpoint=config.llm_config.get('azure_endpoint'),
                azure_api_version=config.llm_config.get('azure_api_version'),
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
            
            results.append({
                'file': doc_path.name,
                'success': True,
                'models_successful': successful,
                'models_total': len(qa_models),
                'cost': doc_cost,
                'time': doc_time,
            })
            
            if verbosity != 'quiet':
                status = "[green]✓[/green]" if successful == len(qa_models) else "[yellow]⚠[/yellow]"
                console.print(f"  {status} {successful}/{len(qa_models)} models • [magenta]${doc_cost:.4f}[/magenta] • [dim]{doc_time:.1f}s[/dim]")
            
        except Exception as e:
            results.append({
                'file': doc_path.name,
                'success': False,
                'error': str(e),
            })
            if verbosity != 'quiet':
                console.print(f"  [red]✗[/red] Error: {str(e)[:60]}")
    
    # Summary
    if verbosity != 'quiet':
        console.print()
        console.print("[dim]" + "─" * 80 + "[/dim]")
        console.print()
        
        successful_docs = [r for r in results if r.get('success')]
        failed_docs = [r for r in results if not r.get('success')]
        
        summary_stats = {
            "Documents Processed": f"{len(results)}",
            "Successful": f"[green]{len(successful_docs)}[/green]",
        }
        
        if failed_docs:
            summary_stats["Failed"] = f"[red]{len(failed_docs)}[/red]"
        
        summary_stats["Models Used"] = f"{len(qa_models)} ({', '.join(qa_models[:3])}{'...' if len(qa_models) > 3 else ''})"
        summary_stats["Total API Calls"] = f"{len(successful_docs) * len(qa_models)}"
        summary_stats["Total Cost"] = f"[magenta]${total_cost:.4f}[/magenta]"
        summary_stats["Total Time"] = f"{total_time:.1f}s"
        
        table = create_summary_table("QA/QC Extraction Summary", summary_stats)
        console.print(table)
        
        # Output location
        qa_qc_output = output_dir / "qa_qc"
        console.print()
        console.print(f"[bold green]✓ QA/QC outputs saved to:[/bold green]")
        console.print(f"  [bold]{qa_qc_output.absolute()}[/bold]")
        console.print()
        console.print("[dim]Next step: Run comparison engine to analyze model differences (Phase 3)[/dim]")
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
            for id_field in ['id', 'identifier', 'jurisdiction', 'number', 'name']:
                if id_field in value:
                    id_val = value[id_field]
                    if isinstance(id_val, dict):
                        parts = [str(v) for v in id_val.values() if v]
                        identifier = '-'.join(parts) if parts else 'N/A'
                    elif id_val:
                        identifier = str(id_val)
                    break
        elif isinstance(value, (str, int)) and value and key.lower() in ['id', 'identifier', 'jurisdiction', 'number']:
            identifier = str(value)
    
    extracted_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    lineage = {
        'artifact_id': (runtime_artifact or {}).get('artifact_id') or 'artifact://runtime/unresolved',
        'profile_id': ((runtime_artifact or {}).get('lineage') or {}).get('profile_id') or 'default',
        'run_id': run_id or f"run://{doc_path.stem}",
        'model': model,
        'provider': provider or 'unknown',
        'schema_id': schema_id,
        'extracted_at': extracted_at,
    }

    output_data = {
        'record_id': f"record://{lineage['run_id'].replace('run://', '')}/{doc_path.stem}",
        'contract_version': '1.0.0',
        'document': {
            'source_document_id': identifier if identifier != 'N/A' else doc_path.stem,
            'source_path': doc_path.as_posix(),
            'source_filename': doc_path.name,
        },
        'lineage': lineage,
        'payload': result.data,
        'quality': {
            'overall_confidence': result.completeness_score,
            'warnings': result.validation_notes or [],
        },
        'processing_metrics': {
            'duration_seconds': result.processing_time,
            'cost_usd': result.cost,
            'input_tokens': None,
            'output_tokens': None,
        },
    }
    
    output_file = output_dir / f"{doc_path.stem}.json"
    with open(output_file, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    return num_items


@click.command()
@click.argument('extraction_file', type=click.Path(exists=True))
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.option('--show-data', is_flag=True, help='Display extracted data with syntax highlighting')
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
        payload = data.get('payload')
        if not isinstance(payload, dict):
            raise ValidationError("Extraction file must use canonical extraction-record format with a 'payload' object")
        json_validate(instance=payload, schema=schema)
        print_success("Schema validation passed")
    except ValidationError as e:
        print_error("Schema validation failed", e.message)
        sys.exit(1)
    
    # Display metadata
    if verbose or show_data:
        payload = data.get('payload', {})
        if not isinstance(payload, dict):
            payload = {}

        if 'item_count' in data:
            item_count = data.get('item_count', 0)
        else:
            item_count = 0
            for value in payload.values():
                if isinstance(value, list):
                    item_count = max(item_count, len(value))

        metadata = {
            "Source File": data.get('document', {}).get('source_filename', 'N/A'),
            "Extraction Date": data.get('lineage', {}).get('extracted_at', 'N/A'),
            "Model": data.get('lineage', {}).get('model', 'N/A'),
            "Cost": f"${data.get('processing_metrics', {}).get('cost_usd', 0):.4f}",
            "Processing Time": f"{data.get('processing_metrics', {}).get('duration_seconds', 0):.1f}s",
            "Item Count": str(item_count),
        }
        
        console.print()
        table = create_config_table("Extraction Metadata", metadata)
        console.print(table)
    
    # Show extracted data with syntax highlighting
    if show_data and 'payload' in data:
        console.print()
        from streamline_extract.cli.ui import display_json
        display_json(data.get('payload'), title="Extracted Data")
    
    console.print()


@click.command()
@click.argument('extracted_dir', type=click.Path(exists=True))
@click.option('--schema', '-s', type=click.Path(exists=True), required=True, help='Path to JSON schema file (REQUIRED - same as used for extraction)')
@click.option('--output', '-o', type=click.Path(), help='Output directory (auto-detected if not specified)')
@click.option('--quiet', '-q', is_flag=True, help='Minimal output (machine-readable)')
@click.option('--verbose', '-v', is_flag=True, help='Detailed output with statistics')
@click.option('--debug', is_flag=True, help='Debug mode with full logs')
def consolidate(extracted_dir: str, schema: Optional[str], output: Optional[str], quiet: bool, verbose: bool, debug: bool):
    """
    Consolidate extracted JSON files into clean Excel/CSV output.
    
    Works with ANY schema type - automatically detects structure and creates
    clean, readable output with intelligent deduplication.
    
    \b
    EXAMPLES:
        # Consolidate utility tariffs (specify same schema used for extraction)
        streamline-extract consolidate processed/tariffs --schema schemas/proprietary/electricity_tariff_schema.json
        
        # Consolidate geothermal ordinances
        streamline-extract consolidate processed/geothermal_ordinances --schema schemas/proprietary/geothermal_ordinance_schema.json
        
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
        VERBOSITY = 'quiet'
    elif debug:
        VERBOSITY = 'debug'
    elif verbose:
        VERBOSITY = 'verbose'
    else:
        VERBOSITY = 'normal'
    
    # Configure logging with RichHandler for clean integration with UI
    configure_logging(VERBOSITY)
    
    input_dir = Path(extracted_dir)
    
    # Load config
    config = get_config()
    
    # Set up output directory - CLEAN structure
    # processed/category/ → consolidated/category/
    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if 'processed' in parts:
            idx = parts.index('processed')
            parts[idx] = 'consolidated'
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / 'consolidated' / input_dir.name
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Header
    if VERBOSITY != 'quiet':
        print_header("CONSOLIDATION")
        
        config_info = {
            "Input": str(input_dir),
            "Output": str(output_dir)
        }
        
        # Add schema to config after it's determined (will add after schema loading)
        table = create_config_table("", config_info)
        console.print(table)
    
    # Load schema (enforced as required by Click)
    matched_schema = Path(schema)
    
    try:
        from streamline_extract.utils.schema_metadata import SchemaMetadata
        schema_metadata = SchemaMetadata(matched_schema)
        
        # Add schema to config display after successful load
        if VERBOSITY != 'quiet':
            # Show relative path for schema
            try:
                schema_rel = matched_schema.relative_to(Path.cwd())
                schema_display = str(schema_rel)
            except ValueError:
                schema_display = str(matched_schema)
            
            console.print(f"  [bold]Schema[/bold]      {schema_display}")
            console.print()
    except Exception as e:
        print_error(
            "Schema validation failed",
            f"Schema {matched_schema.name} is missing required $metadata section: {e}\\n"
            "StreamlineExtract v2.0+ requires schemas with $metadata.\\n"
            "See schemas/SCHEMA_BEST_PRACTICES.md for examples."
        )
        return
    
    # Consolidate
    if VERBOSITY != 'quiet':
        console.print("[cyan]→[/cyan] Analyzing schema structure...")
    consolidator = Consolidator(
        schema_metadata=schema_metadata, 
        verbose=(VERBOSITY == 'verbose' or VERBOSITY == 'debug'),
        debug=(VERBOSITY == 'debug')
    )
    
    try:
        df, schema_info = consolidator.consolidate_from_directory(input_dir)
        
        if df.empty:
            print_warning("No data found to consolidate")
            return
        
        if VERBOSITY != 'quiet':
            print_success(f"Schema detected: [cyan]{schema_info['type']}[/cyan]")
            console.print(f"  [dim]Main entity: {schema_info['main_array_key']}[/dim]\n")
            
            console.print("[cyan]→[/cyan] Creating outputs...")
        
        # Generate output filename
        base_name = input_dir.name.replace('_', '-')
        
        # Save CSV
        csv_path = output_dir / f"{base_name}.csv"
        consolidator.save_csv(df, csv_path)
        csv_size_mb = csv_path.stat().st_size / (1024 * 1024)
        
        if VERBOSITY == 'verbose' or VERBOSITY == 'debug':
            print_success(f"CSV saved: {csv_path.name} ({csv_size_mb:.2f} MB, {len(df)} rows)")
        elif VERBOSITY != 'quiet':
            print_success(f"CSV saved ({len(df)} rows)")
        
        # Save Excel
        excel_path = output_dir / f"{base_name}.xlsx"
        consolidator.save_excel(df, excel_path)
        excel_size_mb = excel_path.stat().st_size / (1024 * 1024)
        
        if VERBOSITY == 'verbose' or VERBOSITY == 'debug':
            print_success(f"Excel saved: {excel_path.name} ({excel_size_mb:.2f} MB, {len(df)} rows)")
        elif VERBOSITY != 'quiet':
            print_success("Excel saved (clean formatting, auto-sized columns)")
        
        # Summary
        if VERBOSITY != 'quiet':
            summary_stats = {
                "Schema Type": schema_info['type'],
                "Records": str(len(df)),
                "Columns": str(len(df.columns)),
            }
            
            # Category breakdown
            if schema_info.get('category_field'):
                category_display = ''.join([' ' + c if c.isupper() else c for c in schema_info.get('category_field', '')]).strip().title()
                if category_display and category_display in df.columns:
                    top_categories = df[category_display].value_counts().head(5)
                    if not top_categories.empty:
                        top_cat_str = ", ".join([f"{cat} ({count})" for cat, count in list(top_categories.items())[:3]])
                        summary_stats[f"Top {category_display}s"] = top_cat_str
            
            console.print()
            table = create_summary_table("Consolidation Summary", summary_stats)
            console.print(table)
            
            console.print(f"\n[bold green]✓ Output Location[/bold green]")
            console.print(f"  [bold]{excel_path.absolute()}[/bold]\n")
        else:
            # Quiet mode - just print the path
            console.print(str(excel_path.absolute()))
        
    except Exception as e:
        print_error("Consolidation failed", str(e))
        if VERBOSITY == 'debug':
            import traceback
            traceback.print_exc()
        sys.exit(1)


@click.command()
@click.argument('qa_qc_path', type=click.Path(exists=True))
@click.option('--schema', '-s', type=click.Path(exists=True), required=True, help='Path to QA/QC schema file (REQUIRED)')
@click.option('--quiet', '-q', is_flag=True, help='Minimal output')
@click.option('--verbose', '-v', is_flag=True, help='Detailed output')
def compare(qa_qc_path: str, schema: str, quiet: bool, verbose: bool):
    """
    Generate comparison reports from existing QA/QC extractions.
    
    This command compares outputs from multiple models that were previously
    extracted with --enable-qa-qc, without re-running the expensive extractions.
    
    \b
    EXAMPLES:
        # Generate comparison reports for all documents
        streamline-extract compare processed/qa_qc_test/qa_qc --schema schemas/qaqc/geothermal_qaqc.json
        
        # Compare specific document folder
        streamline-extract compare processed/qa_qc_test/qa_qc/Chaffee\\ County --schema schemas/qaqc/geothermal_qaqc.json
    
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
    from streamline_extract.utils.schema_metadata import SchemaMetadata
    
    qa_qc_path = Path(qa_qc_path)
    schema_path = Path(schema)
    
    # Set verbosity
    if quiet:
        verbosity = 'quiet'
    elif verbose:
        verbosity = 'verbose'
    else:
        verbosity = 'normal'
    
    # Load schema metadata
    try:
        schema_metadata = SchemaMetadata(schema_path)
    except Exception as e:
        print_error("Failed to load schema", str(e))
        sys.exit(1)
    
    # Display header
    if verbosity != 'quiet':
        print_header("QA/QC COMPARISON REPORT")
        
        config_info = {
            "Input": str(qa_qc_path),
            "Schema": str(schema_path),
            "Match Fields": ", ".join(schema_metadata.get_qa_qc_match_fields()),
            "Compare Fields": ", ".join(schema_metadata.get_qa_qc_compare_fields()),
        }
        table = create_config_table("Configuration", config_info)
        console.print(table)
        console.print()
    
    # Create comparison engine and report generator
    engine = ComparisonEngine(schema_metadata)
    report_gen = ReportGenerator()
    
    # Find document directories to process
    # If path is a document directory (contains .json files), process just that one
    # Otherwise, process all subdirectories
    doc_dirs = []
    json_files_in_path = list(qa_qc_path.glob('*.json'))
    
    if json_files_in_path and any(f.name != 'metadata.json' for f in json_files_in_path):
        # This is a single document directory
        doc_dirs = [qa_qc_path]
    else:
        # This is a parent directory containing document subdirectories
        doc_dirs = [d for d in sorted(qa_qc_path.iterdir()) if d.is_dir()]
    
    if not doc_dirs:
        print_error(
            "No QA/QC outputs found",
            f"No document directories found in {qa_qc_path}",
            ["Run extraction with --enable-qa-qc first", "Check the path is correct"]
        )
        sys.exit(1)
    
    # Process each document directory
    results = []
    
    if verbosity != 'quiet':
        console.print("[dim]" + "─" * 60 + "[/dim]")
        console.print()
    
    for doc_dir in doc_dirs:
        # Find model output files
        model_files = {}
        for f in doc_dir.glob('*.json'):
            if f.name == 'metadata.json':
                continue
            model_name = f.stem
            model_files[model_name] = f
        
        if len(model_files) < 2:
            if verbosity != 'quiet':
                console.print(f"[yellow]⚠[/yellow] Skipping {doc_dir.name}: needs at least 2 model outputs")
            continue
        
        # Run comparison
        try:
            result = engine.compare_outputs(model_files, doc_dir.name)
            
            # Generate reports
            excel_path, csv_path = report_gen.generate_report(result, doc_dir)
            
            results.append({
                'name': doc_dir.name,
                'success': True,
                'models': result.models,
                'items_per_model': result.summary.get('items_per_model', {}),
                'full_agreement_pct': result.summary.get('full_agreement_pct', 0),
                'needs_review_count': result.summary.get('needs_review_count', 0),
                'total_comparisons': result.summary.get('total_comparisons', 0),
            })
            
            if verbosity != 'quiet':
                agreement_pct = result.summary.get('full_agreement_pct', 0)
                needs_review = result.summary.get('needs_review_count', 0)
                total = result.summary.get('total_comparisons', 0)
                
                # Color code based on agreement
                if agreement_pct >= 80:
                    status = "[green]✓[/green]"
                elif agreement_pct >= 50:
                    status = "[yellow]⚠[/yellow]"
                else:
                    status = "[red]![/red]"
                
                console.print(f"{status} {doc_dir.name}")
                console.print(f"    Agreement: [bold]{agreement_pct:.1f}%[/bold] ({total - needs_review}/{total} fields)")
                console.print(f"    Needs review: {needs_review} field(s)")
                console.print()
                
        except Exception as e:
            results.append({
                'name': doc_dir.name,
                'success': False,
                'error': str(e),
            })
            if verbosity != 'quiet':
                console.print(f"[red]✗[/red] {doc_dir.name}: {str(e)[:50]}")
    
    # Summary
    if verbosity != 'quiet':
        console.print("[dim]" + "─" * 60 + "[/dim]")
        console.print()
        
        successful = [r for r in results if r.get('success')]
        failed = [r for r in results if not r.get('success')]
        
        if successful:
            avg_agreement = sum(r['full_agreement_pct'] for r in successful) / len(successful)
            total_reviews = sum(r['needs_review_count'] for r in successful)
            
            summary_stats = {
                "Documents Compared": str(len(successful)),
                "Average Agreement": f"{avg_agreement:.1f}%",
                "Total Fields Needing Review": str(total_reviews),
            }
            
            if failed:
                summary_stats["Failed"] = f"[red]{len(failed)}[/red]"
            
            table = create_summary_table("Comparison Summary", summary_stats)
            console.print(table)
            console.print()
            
            print_success(f"Reports saved to: {qa_qc_path}")
            console.print("[dim]  Files: comparison_report.xlsx, comparison_report.csv[/dim]")
            console.print()
        else:
            print_warning("No documents were successfully compared")
    else:
        # Quiet mode - just print success count
        successful = len([r for r in results if r.get('success')])
        console.print(f"{successful} documents compared")
