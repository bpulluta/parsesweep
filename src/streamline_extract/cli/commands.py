"""
Core workflow CLI commands for StreamlineExtract.

This module contains the main data processing pipeline commands:
- process: Extract structured data from documents to JSON
- validate: Validate extraction results against schema
- consolidate: Merge JSON files into Excel/CSV

For utility commands (init, preview, estimate, etc.), see utils_commands.py
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple

import click
from dotenv import load_dotenv
from rich.progress import track

from streamline_extract.utils.config import get_config
from streamline_extract.extraction import DocumentExtractor, load_schema
from streamline_extract.extraction.pdf_utils import extract_text_from_pdf
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
from streamline_extract.cli.cost_tracker import CostTracker


# Global verbosity level (set by CLI flags)
VERBOSITY = 'normal'  # 'quiet', 'normal', 'verbose', 'debug'


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


def validate_path_structure(path: Path, expected_content: str = "PDFs") -> Tuple[bool, str]:
    """
    Validate that a path exists and contains expected content.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not path.exists():
        return False, f"Path does not exist: {path}"
    
    if not path.is_dir():
        # Single file is okay for some operations
        return True, None
    
    # Check if directory is empty
    contents = list(path.iterdir())
    if not contents:
        return False, f"Directory is empty: {path}"
    
    return True, None


@click.command()
@click.argument('path', type=click.Path())
@click.option('--output', '-o', type=click.Path(), help='Output directory (auto-detected if not specified)')
@click.option('--schema', '-s', type=click.Path(exists=True), help='Path to JSON schema file (auto-detects from path if not specified)')
@click.option('--state', help='State/category name (auto-detected from path if not specified)')
@click.option('--model', default='gpt-4o-mini', show_default=True, help='AI model: gpt-4o-mini (fast) or gpt-4o (accurate)')
@click.option('--enable-qa-qc', is_flag=True, help='Enable detailed validation (slower, adds traceability)')
@click.option('--use-azure', is_flag=True, default=None, help='Force Azure OpenAI (auto-detects from .env if not specified)')
@click.option('--limit', '-n', type=int, help='Process only first N files')
@click.option('--skip-existing/--reprocess', default=True, show_default=True, help='Skip files already processed')
@click.option('--max-context', type=int, default=400000, show_default=True, help='Max document characters to process (400k proven reliable)')
@click.option('--quiet', '-q', is_flag=True, help='Minimal output (machine-readable)')
@click.option('--verbose', '-v', is_flag=True, help='Detailed output with statistics')
@click.option('--debug', is_flag=True, help='Debug mode with full logs')
@click.option('--live-dashboard', is_flag=True, help='Show live dashboard during extraction')
def process(path: str, output: Optional[str], schema: Optional[str], state: Optional[str],
            model: str, enable_qa_qc: bool, use_azure: Optional[bool], limit: Optional[int], 
            skip_existing: bool, max_context: int, quiet: bool, verbose: bool, debug: bool, live_dashboard: bool):
    """
    Process documents and extract structured data.
    
    This command reads PDF documents and extracts structured information
    based on a JSON schema. Works with any document type (permits, ordinances, regulations, etc.).
    
    The output will be saved as JSON files in a parallel folder structure.
    For example: documents/Category/ → processed/Category/
    
    \b
    EXAMPLES:
        # Process using default schema
        streamline-extract process documents/Category/doc1.pdf
        
        # Process with a specific schema (e.g., geothermal ordinances)
        streamline-extract process documents/Ordinances --schema schemas/geothermal_ordinance_schema.json
        
        # Process all documents in a directory
        streamline-extract process documents/Category
        
        # Process just the first 5 documents (useful for testing)
        streamline-extract process documents/Category -n 5
        
        # Use Azure OpenAI (if you have Azure credits)
        streamline-extract process documents/Category --use-azure
        
        # Reprocess files that were already processed
        streamline-extract process documents/Category --reprocess
    
    \b
    REQUIREMENTS:
        • PDF files in the specified directory
        • JSON schema file (uses default schema if not specified)
        • OpenAI API key in .env file (OPENAI_API_KEY=sk-...)
        • Or Azure OpenAI credentials (if using --use-azure)
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
    
    # Auto-detect API provider or use forced option
    if use_azure is None:
        # Auto-detect from .env
        provider, is_valid, error_msg = detect_api_provider()
        if not is_valid:
            print_error(
                "No API credentials found",
                "API credentials are required to extract data.",
                [
                    "Create a .env file in your project root",
                    "Add one of the following:",
                    "",
                    "Azure OpenAI (recommended):",
                    "  AZURE_OPENAI_API_KEY=your-key",
                    "  AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/",
                    "  AZURE_OPENAI_MODEL=your-model-name",
                    "",
                    "OR OpenAI:",
                    "  OPENAI_API_KEY=sk-your-key-here"
                ]
            )
            sys.exit(1)
        use_azure = (provider == 'azure')
    else:
        # User explicitly requested a provider
        if use_azure:
            required_keys = ['AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT']
            missing = [key for key in required_keys if not os.getenv(key)]
            if missing:
                print_error(
                    "Azure OpenAI credentials not found",
                    f"You specified --use-azure but missing: {', '.join(missing)}",
                    [
                        "Add to your .env file:",
                        "  AZURE_OPENAI_API_KEY=your-key",
                        "  AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/"
                    ]
                )
                sys.exit(1)
        else:
            if not os.getenv('OPENAI_API_KEY'):
                print_error(
                    "OpenAI API key not found",
                    "OPENAI_API_KEY is required",
                    [
                        "Add to your .env file:",
                        "  OPENAI_API_KEY=sk-your-key-here",
                        "Get a key from: https://platform.openai.com/api-keys"
                    ]
                )
                sys.exit(1)
    
    config = get_config()
    path = Path(path)
    
    # Determine if single file or directory
    is_dir = path.is_dir()
    
    # Infer state from path
    if is_dir:
        state = state or path.name
    else:
        state = state or path.parent.name
    
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
    if is_dir:
        # Find all supported document types in this directory
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(path.glob(f"*{ext}")))
        doc_files = sorted(doc_files)  # Sort all files together
        
        # If no documents found, look for subdirectories (e.g., state folders)
        if not doc_files:
            subdirs = [d for d in path.iterdir() if d.is_dir() and not d.name.startswith('.')]
            if subdirs:
                # Found subdirectories - process each one recursively
                if VERBOSITY != 'quiet':
                    console.print(f"\n[bold]📁 Found {len(subdirs)} subfolder(s) with documents[/bold]")
                    for subdir in subdirs:
                        subdir_docs = []
                        for ext in SUPPORTED_EXTENSIONS:
                            subdir_docs.extend(subdir.glob(f"*{ext}"))
                        if subdir_docs:
                            console.print(f"  → {subdir.name}: {len(subdir_docs)} document(s)")
                
                # Call extract for each subdirectory
                for subdir in subdirs:
                    subdir_docs = []
                    for ext in SUPPORTED_EXTENSIONS:
                        subdir_docs.extend(subdir.glob(f"*{ext}"))
                    if subdir_docs:
                        import subprocess
                        cmd = ['pixi', 'run', 'streamline-extract', 'extract', str(subdir)]
                        if use_azure:
                            cmd.append('--use-azure')
                        if enable_qa_qc:
                            cmd.append('--enable-qa-qc')
                        if not skip_existing:
                            cmd.append('--reprocess')
                        if limit:
                            cmd.extend(['-n', str(limit)])
                        if model != 'gpt-4o-mini':
                            cmd.extend(['--model', model])
                        
                        subprocess.run(cmd)
                return
        
        # Check if we found any documents at all
        if not doc_files and not subdirs:
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
            doc_files = [p for p in doc_files if not (output_dir / f"{p.stem}.json").exists()]
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
        
        # Build configuration display
        config_info = {
            "State": state,
            "Input": str(path),
            "Files": f"{len(doc_files)} document{'s' if len(doc_files) != 1 else ''}",
        }
        
    # Determine provider and model info
    if use_azure:
        azure_model = os.environ.get('AZURE_OPENAI_MODEL')
        display_model = azure_model if azure_model else model
        provider_name = "Azure OpenAI"
        model_display = display_model
    else:
        provider_name = "OpenAI"
        model_display = model
    
    # Add model/provider info to config
    if VERBOSITY != 'quiet':
        config_info["Model"] = model_display
        config_info["Provider"] = provider_name
        config_info["QA/QC"] = "Enabled" if enable_qa_qc else "Disabled"
        config_info["Output"] = str(output_dir.absolute())
    
    # Load schema - auto-detect or use specified
    if schema:
        # User provided explicit schema path
        schema_path = Path(schema)
        loaded_schema = load_schema(schema_path)
        if VERBOSITY != 'quiet':
            config_info["Schema"] = schema_path.name
    else:
        # Auto-detect schema based on folder name/path
        schema_path = None
        schema_candidates = config.schema_dir.glob("*.json")
        
        # Try to match schema name with path keywords
        path_lower = str(path).lower()
        matched_schema = None
        
        for candidate in schema_candidates:
            candidate_name_lower = candidate.stem.lower()
            # Check if path contains schema keywords
            if any(keyword in path_lower for keyword in ['geothermal', 'ordinance']) and 'geothermal' in candidate_name_lower:
                matched_schema = candidate
                break
            elif any(keyword in path_lower for keyword in ['tariff', 'rate', 'electric']) and 'tariff' in candidate_name_lower:
                matched_schema = candidate
                break
        
        if matched_schema:
            schema_path = matched_schema
            loaded_schema = load_schema(schema_path)
            if VERBOSITY != 'quiet':
                config_info["Schema"] = f"{schema_path.name} (auto-detected)"
        else:
            # Fall back to default
            schema_path = config.default_schema
            loaded_schema = load_schema(schema_path)
            if VERBOSITY != 'quiet':
                config_info["Schema"] = f"{schema_path.name} (default)"
    
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
                text = extract_text_from_document(doc)
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
    
    # Track the actual model being used for output
    actual_model = model
    
    if use_azure:
        from openai import AzureOpenAI
        
        azure_key = os.environ.get('AZURE_OPENAI_API_KEY')
        azure_endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
        azure_version = os.environ.get('AZURE_OPENAI_API_VERSION', '2025-04-01-preview')
        azure_model = os.environ.get('AZURE_OPENAI_MODEL')
        
        if not azure_key or not azure_endpoint:
            print_error(
                "Azure OpenAI credentials not found",
                "Required: AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT"
            )
            return
        
        # Use Azure deployment name if configured, otherwise use the model parameter
        deployment_name = azure_model if azure_model else model
        actual_model = deployment_name  # Track actual model for output
        
        # Create schema metadata if available
        schema_metadata = None
        if schema_path:
            try:
                from streamline_extract.utils.schema_metadata import SchemaMetadata
                schema_metadata = SchemaMetadata(schema_path)
            except Exception as e:
                if VERBOSITY == 'verbose':
                    console.print(f"[dim yellow]Could not load schema metadata: {e}[/dim yellow]")
        
        # Initialize extractor with Azure parameters
        extractor = DocumentExtractor(
            api_key=azure_key,
            model=deployment_name,
            max_context_chars=max_context,
            use_azure=True,
            azure_endpoint=azure_endpoint,
            azure_api_version=azure_version,
            schema_metadata=schema_metadata,
        )
    else:
        if not config.openai_api_key:
            print_error(
                "OPENAI_API_KEY not found",
                f"Create .env file at: {config.project_root / '.env'}"
            )
            print(f"   Create .env file at: {config.project_root / '.env'}")
            return
        
        # Create schema metadata if available
        schema_metadata = None
        if schema_path:
            try:
                from streamline_extract.utils.schema_metadata import SchemaMetadata
                schema_metadata = SchemaMetadata(schema_path)
            except Exception as e:
                if VERBOSITY == 'verbose':
                    console.print(f"[dim yellow]Could not load schema metadata: {e}[/dim yellow]")
        
        extractor = DocumentExtractor(
            api_key=config.openai_api_key,
            model=model,
            max_context_chars=max_context,
            schema_metadata=schema_metadata
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
                    result = extractor.extract(text, loaded_schema, enable_qa_qc=enable_qa_qc)
                    
                    # Save result
                    num_items = _extract_and_save_result(doc_path, result, output_dir, state, actual_model, enable_qa_qc)
                    
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
        task = progress.add_task(f"Extracting {len(doc_files)} documents...", total=len(doc_files))
        
        with progress:
            for doc_path in doc_files:
                try:
                    # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                    text = extract_text_from_document(doc_path)
                    result = extractor.extract(text, loaded_schema, enable_qa_qc=enable_qa_qc)
                    
                    # Save result
                    num_items = _extract_and_save_result(doc_path, result, output_dir, state, actual_model, enable_qa_qc)
                    
                    # Track results
                    results.append({
                        'file': doc_path.name,
                        'items': num_items,
                        'cost': result.cost,
                        'time': result.processing_time,
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
                console.print(f"[cyan]→[/cyan] {doc_path.name}")
            
            try:
                # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
                text = extract_text_from_document(doc_path)
                result = extractor.extract(text, loaded_schema, enable_qa_qc=enable_qa_qc)
                
                # Save result
                num_items = _extract_and_save_result(doc_path, result, output_dir, state, actual_model, enable_qa_qc)
                
                # Track results
                results.append({
                    'file': doc_path.name,
                    'items': num_items,
                    'cost': result.cost,
                    'time': result.processing_time,
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
    
    # Summary
    if VERBOSITY != 'quiet':
        successful = [r for r in results if r.get('success')]
        failed = [r for r in results if not r.get('success')]
        
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


def _extract_and_save_result(doc_path: Path, result, output_dir: Path, state: str, model: str, qa_qc_enabled: bool) -> int:
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
    
    output_data = {
        'source_file': doc_path.name,
        'extraction_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        'state': state,
        'model': model,
        'qa_qc_enabled': qa_qc_enabled,
        'cost_usd': result.cost,
        'processing_time_sec': result.processing_time,
        'completeness_score': result.completeness_score,
        'item_count': num_items,
        'identifier': identifier,
        'data': result.data,
        'validation_notes': result.validation_notes
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
        json_validate(instance=data.get('data', data), schema=schema)
        print_success("Schema validation passed")
    except ValidationError as e:
        print_error("Schema validation failed", e.message)
        sys.exit(1)
    
    # Display metadata
    if verbose or show_data:
        metadata = {
            "Source File": data.get('source_file', 'N/A'),
            "Extraction Date": data.get('extraction_date', 'N/A'),
            "Model": data.get('model', 'N/A'),
            "Cost": f"${data.get('cost_usd', 0):.4f}",
            "Processing Time": f"{data.get('processing_time_sec', 0):.1f}s",
            "Item Count": str(data.get('item_count', 0)),
        }
        
        console.print()
        table = create_config_table("Extraction Metadata", metadata)
        console.print(table)
    
    # Show extracted data with syntax highlighting
    if show_data and 'data' in data:
        console.print()
        from streamline_extract.cli.ui import display_json
        display_json(data['data'], title="Extracted Data")
    
    console.print()


@click.command()
@click.argument('extracted_dir', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output directory (auto-detected if not specified)')
def consolidate(extracted_dir: str, output: Optional[str]):
    """
    Consolidate extracted JSON files into clean Excel/CSV output.
    
    Works with ANY schema type - automatically detects structure and creates
    clean, readable output with intelligent deduplication.
    
    \b
    EXAMPLES:
        # Consolidate geothermal ordinances
        streamline-extract consolidate processed/geothermal_ordinances
        
        # Consolidate utility tariffs
        streamline-extract consolidate processed/tariffs
        
        # Specify custom output directory
        streamline-extract consolidate processed/data --output my_analysis/
    
    \b
    OUTPUT:
        • Clean Excel file with auto-sized columns
        • CSV file for data analysis
        • Automatic deduplication of identical entries
    """
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
    print_header("CONSOLIDATION")
    
    config_info = {
        "Input": str(input_dir),
        "Output": str(output_dir)
    }
    table = create_config_table("", config_info)
    console.print(table)
    console.print()
    
    # Try to find and load schema metadata (required in v2.0+)
    schema_metadata = None
    # Auto-detect schema based on folder name/path
    schema_candidates = config.schema_dir.glob("*.json")
    path_lower = str(input_dir).lower()
    matched_schema = None
    
    for candidate in schema_candidates:
        candidate_name_lower = candidate.stem.lower()
        # Check if path contains schema keywords
        if any(keyword in path_lower for keyword in ['geothermal', 'ordinance']) and 'geothermal' in candidate_name_lower:
            matched_schema = candidate
            break
        elif any(keyword in path_lower for keyword in ['tariff', 'rate', 'electric']) and 'tariff' in candidate_name_lower:
            matched_schema = candidate
            break
        elif any(keyword in path_lower for keyword in ['permit', 'air_quality', 'aq']) and 'permit' in candidate_name_lower:
            matched_schema = candidate
            break
    
    if matched_schema:
        try:
            from streamline_extract.utils.schema_metadata import SchemaMetadata
            schema_metadata = SchemaMetadata(matched_schema)
            console.print(f"[dim]Using schema metadata from {matched_schema.name}[/dim]")
        except Exception as e:
            print_error(
                "Schema validation failed",
                f"Schema {matched_schema.name} is missing required metadata: {e}"
            )
            return
    else:
        print_error(
            "No schema found for consolidation",
            "StreamlineExtract v2.0+ requires a schema with $metadata.\\n"
            f"Could not auto-detect schema for: {input_dir}\\n"
            f"Available schemas in {config.schema_dir}: {[s.name for s in config.schema_dir.glob('*.json')]}"
        )
        return
    
    # Consolidate
    console.print("[cyan]→[/cyan] Analyzing schema structure...")
    consolidator = Consolidator(schema_metadata=schema_metadata)
    
    try:
        df, schema_info = consolidator.consolidate_from_directory(input_dir)
        
        if df.empty:
            print_warning("No data found to consolidate")
            return
        
        print_success(f"Schema detected: [cyan]{schema_info['type']}[/cyan]")
        console.print(f"  [dim]Main entity: {schema_info['main_array_key']}[/dim]\n")
        
        console.print("[cyan]→[/cyan] Creating outputs...")
        
        # Generate output filename
        base_name = input_dir.name.replace('_', '-')
        
        # Save CSV
        csv_path = output_dir / f"{base_name}.csv"
        consolidator.save_csv(df, csv_path)
        print_success(f"CSV saved ({len(df)} rows)")
        
        # Save Excel
        excel_path = output_dir / f"{base_name}.xlsx"
        consolidator.save_excel(df, excel_path)
        print_success("Excel saved (clean formatting, auto-sized columns)")
        
        # Summary
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
        
    except Exception as e:
        print_error("Consolidation failed", str(e))
        if '--verbose' in sys.argv or '-v' in sys.argv:
            import traceback
            traceback.print_exc()
        sys.exit(1)

