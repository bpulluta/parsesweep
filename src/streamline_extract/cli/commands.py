"""CLI commands for StreamlineExtract toolkit."""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple

import click
from dotenv import load_dotenv

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


# ANSI color codes for consistent styling
BLUE = '\033[94m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
CYAN = '\033[96m'
MAGENTA = '\033[95m'
BOLD = '\033[1m'
DIM = '\033[2m'
RESET = '\033[0m'


def print_error(message: str, details: str = None, suggestions: List[str] = None):
    """Print a user-friendly error message with optional details and suggestions."""
    print(f"\n{RED}{BOLD}✗ Error:{RESET} {message}\n")
    
    if details:
        print(f"  {DIM}{details}{RESET}\n")
    
    if suggestions:
        print(f"  {BOLD}💡 Try this:{RESET}")
        for suggestion in suggestions:
            print(f"     • {suggestion}")
        print()


def print_warning(message: str, details: str = None):
    """Print a user-friendly warning message."""
    print(f"\n{YELLOW}{BOLD}⚠ Warning:{RESET} {message}")
    if details:
        print(f"  {DIM}{details}{RESET}")
    print()


def print_success(message: str):
    """Print a success message."""
    print(f"{GREEN}✓{RESET} {message}")


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
def extract(path: str, output: Optional[str], schema: Optional[str], state: Optional[str],
            model: str, enable_qa_qc: bool, use_azure: Optional[bool], limit: Optional[int], skip_existing: bool, max_context: int):
    """
    Extract structured data from PDF documents.
    
    This command reads PDF documents and extracts structured information
    based on a JSON schema. Works with any document type (permits, ordinances, regulations, etc.).
    
    The output will be saved as JSON files in a parallel folder structure.
    For example: documents/Category/ → extracted/Category/
    
    \b
    EXAMPLES:
        # Extract using default schema
        streamline-extract extract documents/Category/doc1.pdf
        
        # Extract with a specific schema (e.g., geothermal ordinances)
        streamline-extract extract documents/Ordinances --schema schemas/geothermal_ordinance_schema.json
        
        # Extract all documents in a directory
        streamline-extract extract documents/Category
        
        # Extract just the first 5 documents (useful for testing)
        streamline-extract extract documents/Category -n 5
        
        # Use Azure OpenAI (if you have Azure credits)
        streamline-extract extract documents/Category --use-azure
        
        # Reprocess files that were already extracted
        streamline-extract extract documents/Category --reprocess
    
    \b
    REQUIREMENTS:
        • PDF files in the specified directory
        • JSON schema file (uses default schema if not specified)
        • OpenAI API key in .env file (OPENAI_API_KEY=sk-...)
        • Or Azure OpenAI credentials (if using --use-azure)
    """
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
    # documents/category/ → extracted/category/
    if output:
        output_dir = Path(output)
    elif is_dir:
        # Replace 'documents' with 'extracted' at project root level
        parts = list(path.parts)
        if 'documents' in parts:
            idx = parts.index('documents')
            parts[idx] = 'extracted'
            output_dir = Path(*parts)
        else:
            # Fallback: create in project root extracted/
            project_root = Path.cwd()
            output_dir = project_root / 'extracted' / path.name
    else:
        # Single file: parent directory logic
        parts = list(path.parent.parts)
        if 'documents' in parts:
            idx = parts.index('documents')
            parts[idx] = 'extracted'
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / 'extracted' / path.parent.name
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # ANSI color codes - define early for use throughout function
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    
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
                print(f"\n{BOLD}📁 Found {len(subdirs)} subfolder(s) with documents{RESET}")
                for subdir in subdirs:
                    subdir_docs = []
                    for ext in SUPPORTED_EXTENSIONS:
                        subdir_docs.extend(subdir.glob(f"*{ext}"))
                    if subdir_docs:
                        print(f"  → {subdir.name}: {len(subdir_docs)} document(s)")
                
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
            if skipped > 0 and len(doc_files) > 0:
                print(f"\n{CYAN}ℹ{RESET}  Skipping {skipped} already processed file{'s' if skipped != 1 else ''}")
                print(f"   {DIM}(use --reprocess to extract them again){RESET}")
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
        if is_dir:
            print(f"\n{GREEN}✓{RESET} All {original_count} file(s) already processed!")
            print(f"  {DIM}Output directory: {output_dir}{RESET}")
            print(f"\n  {DIM}Use --reprocess to extract them again{RESET}\n")
        return
    
    # Header with clean visual separation
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'DOCUMENT EXTRACTION':^76}{RESET} {BOLD}{BLUE}│{RESET}")
    print(f"{BOLD}{BLUE}└{'─' * 78}┘{RESET}")
    
    # Configuration table with colors
    print(f"\n  {BOLD}Configuration{RESET}")
    print(f"  {DIM}{'─' * 76}{RESET}")
    print(f"  {DIM}State{RESET}     {CYAN}{state}{RESET}")
    print(f"  {DIM}Input{RESET}     {path}")
    
    # Make output location very prominent
    print(f"\n  {BOLD}{GREEN}📁 Output Location{RESET}")
    print(f"  {DIM}{'─' * 76}{RESET}")
    print(f"  {BOLD}{output_dir.absolute()}{RESET}")
    print(f"  {DIM}(extracted JSONs will be saved here){RESET}")
    
    # Display model info
    print(f"\n  {BOLD}Extraction Settings{RESET}")
    print(f"  {DIM}{'─' * 76}{RESET}")
    if use_azure:
        azure_model = os.environ.get('AZURE_OPENAI_MODEL')
        display_model = azure_model if azure_model else model
        print(f"  {DIM}Model{RESET}     {display_model}")
        print(f"  {DIM}Provider{RESET}  {MAGENTA}Azure OpenAI{RESET} {DIM}(auto-detected){RESET}")
    else:
        print(f"  {DIM}Model{RESET}     {model}")
        print(f"  {DIM}Provider{RESET}  OpenAI {DIM}(auto-detected){RESET}")
    
    qa_status = f"{GREEN}Enabled{RESET}" if enable_qa_qc else f"{DIM}Disabled{RESET}"
    print(f"  {DIM}QA/QC{RESET}     {qa_status}")
    print(f"  {DIM}Files{RESET}     {BOLD}{len(doc_files)}{RESET} document{'s' if len(doc_files) != 1 else ''}")
    
    # Load schema - auto-detect or use specified
    if schema:
        # User provided explicit schema path
        schema_path = Path(schema)
        loaded_schema = load_schema(schema_path)
        print(f"  {DIM}Schema{RESET}    {schema_path.name}")
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
            print(f"  {DIM}Schema{RESET}    {CYAN}{schema_path.name}{RESET} {DIM}(auto-detected){RESET}")
        else:
            # Fall back to default
            schema_path = config.default_schema
            loaded_schema = load_schema(schema_path)
            print(f"  {DIM}Schema{RESET}    {schema_path.name} {DIM}(default){RESET}")
    
    # Track the actual model being used for output
    actual_model = model
    
    if use_azure:
        from openai import AzureOpenAI
        
        azure_key = os.environ.get('AZURE_OPENAI_API_KEY')
        azure_endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
        azure_version = os.environ.get('AZURE_OPENAI_API_VERSION', '2025-04-01-preview')
        azure_model = os.environ.get('AZURE_OPENAI_MODEL')
        
        if not azure_key or not azure_endpoint:
            print("❌ Azure OpenAI credentials not found")
            print("   Required: AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT")
            return
        
        # Use Azure deployment name if configured, otherwise use the model parameter
        deployment_name = azure_model if azure_model else model
        actual_model = deployment_name  # Track actual model for output
        
        # Initialize extractor with Azure parameters
        extractor = DocumentExtractor(
            api_key=azure_key,
            model=deployment_name,
            max_context_chars=max_context,
            use_azure=True,
            azure_endpoint=azure_endpoint,
            azure_api_version=azure_version,
        )
    else:
        if not config.openai_api_key:
            print("❌ OPENAI_API_KEY not found")
            print(f"   Create .env file at: {config.project_root / '.env'}")
            return
        extractor = DocumentExtractor(api_key=config.openai_api_key, model=model, max_context_chars=max_context)
    
    # Processing section
    print(f"\n{DIM}{'─' * 80}{RESET}")
    
    results = []
    total_cost = 0.0
    total_time = 0.0
    
    for i, doc_path in enumerate(doc_files, 1):
        # Progress indicator with cleaner format
        if len(doc_files) > 1:
            pct = (i - 1) / len(doc_files)
            bar_len = 30
            filled = int(bar_len * pct)
            bar = f"{GREEN}{'█' * filled}{RESET}{DIM}{'░' * (bar_len - filled)}{RESET}"
            status = f"{CYAN}[{i}/{len(doc_files)}]{RESET}"
            print(f"\n  {status} {bar} {doc_path.name}")
        else:
            print(f"\n  {CYAN}→{RESET} {doc_path.name}")
        
        try:
            # Extract text from document (supports PDF, DOCX, TXT, XLSX, CSV)
            text = extract_text_from_document(doc_path)
            result = extractor.extract(text, loaded_schema, enable_qa_qc=enable_qa_qc)
            
            # Universal schema detection - find main array and identifier dynamically
            num_items = 0
            identifier = "N/A"
            item_label = "items"
            identifier_label = "ID"
            
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
                # Use a readable item label
                item_label = main_array_key.replace('_', ' ')
            
            # Find identifier field dynamically
            for key, value in result.data.items():
                if isinstance(value, dict):
                    # Check for common identifier fields
                    for id_field in ['id', 'identifier', 'jurisdiction', 'number', 'name']:
                        if id_field in value:
                            id_val = value[id_field]
                            if isinstance(id_val, dict):
                                # Composite identifier (e.g., jurisdiction with state/county)
                                parts = [str(v) for v in id_val.values() if v]
                                identifier = '-'.join(parts) if parts else 'N/A'
                                identifier_label = key.replace('_', ' ').title()
                            elif id_val:
                                identifier = str(id_val)
                                identifier_label = key.replace('_', ' ').title()
                            break
                elif isinstance(value, (str, int)) and value and key.lower() in ['id', 'identifier', 'jurisdiction', 'number']:
                    identifier = str(value)
                    identifier_label = key.replace('_', ' ').title()
            
            output_data = {
                'source_file': doc_path.name,
                'extraction_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'state': state,
                'model': actual_model,  # Use actual model name (Azure deployment or OpenAI model)
                'qa_qc_enabled': enable_qa_qc,
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
            
            # Track results
            results.append({
                'file': doc_path.name,
                'identifier': identifier,
                'items': num_items,
                'cost': result.cost,
                'time': result.processing_time,
                'success': True
            })
            total_cost += result.cost
            total_time += result.processing_time
            
            # Success message with compact format and colors
            item_text = f"{GREEN}{num_items}{RESET} {item_label}"
            cost_text = f"{MAGENTA}${result.cost:.4f}{RESET}"
            time_text = f"{DIM}{result.processing_time:.1f}s{RESET}"
            
            # Only show identifier if it's meaningful (not N/A)
            if identifier != "N/A":
                id_text = f"{identifier_label} {CYAN}{identifier}{RESET}"
                print(f"     {GREEN}✓{RESET} {item_text}  {DIM}•{RESET}  {id_text}  {DIM}•{RESET}  {cost_text}  {DIM}•{RESET}  {time_text}")
            else:
                print(f"     {GREEN}✓{RESET} {item_text}  {DIM}•{RESET}  {cost_text}  {DIM}•{RESET}  {time_text}")
            
        except Exception as e:
            results.append({
                'file': doc_path.name,
                'success': False,
                'error': str(e)
            })
            error_msg = str(e)[:60]
            print(f"     {YELLOW}✗{RESET} {DIM}Error: {error_msg}{RESET}")
    
    # Summary with clean table format
    print(f"\n{DIM}{'─' * 80}{RESET}")
    print(f"\n  {BOLD}{'SUMMARY':^76}{RESET}")
    print(f"\n{DIM}{'─' * 80}{RESET}")
    
    successful = [r for r in results if r.get('success')]
    failed = [r for r in results if not r.get('success')]
    
    # Results overview
    print(f"\n  {BOLD}Results{RESET}")
    print(f"    {DIM}Processed{RESET}    {len(results)} file{'s' if len(results) != 1 else ''}")
    print(f"    {DIM}Successful{RESET}   {GREEN}{len(successful)}{RESET}")
    if failed:
        print(f"    {DIM}Failed{RESET}       {YELLOW}{len(failed)}{RESET}")
    
    if successful:
        # Cost metrics
        avg_cost = total_cost / len(successful)
        print(f"\n  {BOLD}Cost{RESET}")
        print(f"    {DIM}Total{RESET}        {MAGENTA}${total_cost:.4f}{RESET}")
        print(f"    {DIM}Per file{RESET}     {MAGENTA}${avg_cost:.4f}{RESET}")
        
        # Time metrics
        avg_time = total_time / len(successful)
        print(f"\n  {BOLD}Time{RESET}")
        print(f"    {DIM}Total{RESET}        {total_time:.1f}s")
        print(f"    {DIM}Per file{RESET}     {avg_time:.1f}s")
        
        # Item count (generators or requirements depending on schema)
        total_items = sum(r.get('items', 0) or 0 for r in successful)
        if total_items > 0:
            # Determine label based on first successful result
            first_result = successful[0]
            if 'items' in first_result:
                print(f"\n  {BOLD}Items Extracted{RESET}")
                print(f"    {DIM}Total{RESET}        {GREEN}{total_items}{RESET}")
    
    # Output location reminder - make it very prominent
    print(f"\n  {BOLD}{GREEN}✓ Results saved to:{RESET}")
    print(f"    {BOLD}{output_dir.absolute()}{RESET}")
    print(f"\n{DIM}{'─' * 80}{RESET}\n")


@click.command()
@click.argument('extraction_file', type=click.Path(exists=True))
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def validate(extraction_file: str, verbose: bool):
    """
    Validate an extraction result against the schema.
    
    Example:
        streamline-extract validate data/extracted/Virginia/11790_DC_Permit.json
    """
    # Setup logging
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format='%(message)s')
    
    print(f"\n{'='*80}")
    print("VALIDATION")
    print(f"{'='*80}\n")
    
    extraction_file = Path(extraction_file)
    print(f"File: {extraction_file.name}")
    
    # Load extraction
    with open(extraction_file) as f:
        data = json.load(f)
    
    # Load schema
    config = get_config()
    schema = load_schema(config.default_schema)
    
    # Validate
    from jsonschema import validate as json_validate, ValidationError
    
    try:
        json_validate(instance=data.get('data', data), schema=schema)
        print("✓ Schema validation passed")
    except ValidationError as e:
        print(f"✗ Schema validation failed: {e.message}")
        sys.exit(1)
    
    # Check data quality
    permit_data = data.get('data', data)
    generators = permit_data.get('generatorSets', [])
    
    print(f"\n{'─'*80}")
    print("Extraction Quality")
    print(f"{'─'*80}\n")
    
    # Use metadata if available (new format)
    if 'quality' in data:
        quality = data['quality']
        print(f"Generator Count:    {quality['generator_count']}")
        print(f"Completeness Score: {quality['completeness_score']:.1%}")
        print(f"Fields Extracted:   {quality['fields_extracted']}")
        print(f"Fields Missing:     {quality['fields_missing']}")
        print(f"Has Permit Details: {'Yes' if quality['has_permit_details'] else 'No'}")
        print(f"Has Generators:     {'Yes' if quality['has_generators'] else 'No'}")
        print(f"Has Emissions Data: {'Yes' if quality['has_emissions_data'] else 'No'}")
    else:
        # Fallback for old format
        print(f"Generator Sets: {len(generators)}")
        
        if generators:
            # Check completeness
            complete_count = sum(
                1 for g in generators
                if g.get('make') and g.get('model') and g.get('fuelType')
            )
            print(f"Complete Records: {complete_count}/{len(generators)} "
                       f"({complete_count/len(generators)*100:.1f}%)")
            
            # Check for emissions data
            with_emissions = sum(
                1 for g in generators
                if any(g.get(k) for k in ['noxEmissionLimitLbsHr', 'coEmissionLimitLbsHr', 'vocEmissionLimitLbsHr'])
            )
            print(f"With Emissions: {with_emissions}/{len(generators)} "
                       f"({with_emissions/len(generators)*100:.1f}%)")
    
    # Check metadata
    if 'cost' in data:
        if isinstance(data['cost'], dict):
            print(f"\nExtraction Cost: ${data['cost']['total_usd']:.4f}")
            print(f"  OpenAI:        ${data['cost']['openai_usd']:.4f}")
            if 'langextract_usd' in data['cost']:
                print(f"  LangExtract:   ${data['cost']['langextract_usd']:.4f}")
        else:
            print(f"\nExtraction Cost: ${data['cost']:.4f}")
    
    if 'timing' in data:
        print(f"Processing Time: {data['timing']['total_sec']:.2f}s")
        print(f"  OpenAI:        {data['timing']['openai_sec']:.2f}s")
        print(f"  LangExtract:   {data['timing']['langextract_sec']:.2f}s")
    elif 'processing_time_sec' in data:
        print(f"Processing Time: {data['processing_time_sec']:.2f}s")
    
    print("\n✓ Validation complete!\n")
    print(f"{'='*80}\n")


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
        streamline-extract consolidate extracted/geothermal_ordinances
        
        # Consolidate utility tariffs
        streamline-extract consolidate extracted/tariffs
        
        # Specify custom output directory
        streamline-extract consolidate extracted/data --output my_analysis/
    
    \b
    OUTPUT:
        • Clean Excel file with auto-sized columns
        • CSV file for data analysis
        • Automatic deduplication of identical entries
    """
    input_dir = Path(extracted_dir)
    
    # Set up output directory - CLEAN structure
    # extracted/category/ → consolidated/category/
    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if 'extracted' in parts:
            idx = parts.index('extracted')
            parts[idx] = 'consolidated'
            output_dir = Path(*parts)
        else:
            project_root = Path.cwd()
            output_dir = project_root / 'consolidated' / input_dir.name
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Header
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'CONSOLIDATION':^76}{RESET} {BOLD}{BLUE}│{RESET}")
    print(f"{BOLD}{BLUE}└{'─' * 78}┘{RESET}\n")
    
    print(f"  {DIM}Input{RESET}     {input_dir}")
    print(f"  {DIM}Output{RESET}    {BOLD}{output_dir}{RESET}\n")
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    # Consolidate
    print(f"  {CYAN}→{RESET} Analyzing schema structure...")
    consolidator = Consolidator()
    
    try:
        df, schema_info = consolidator.consolidate_from_directory(input_dir)
        
        if df.empty:
            print(f"\n  {YELLOW}⚠{RESET} No data found to consolidate")
            return
        
        print(f"  {GREEN}✓{RESET} Schema detected: {CYAN}{schema_info['type']}{RESET}")
        print(f"  {DIM}  Main entity: {schema_info['main_array_key']}{RESET}")
        print(f"\n  {CYAN}→{RESET} Creating outputs...")
        
        # Generate output filename
        base_name = input_dir.name.replace('_', '-')
        
        # Save CSV
        csv_path = output_dir / f"{base_name}.csv"
        consolidator.save_csv(df, csv_path)
        print(f"  {GREEN}✓{RESET} CSV saved ({len(df)} rows)")
        
        # Save Excel
        excel_path = output_dir / f"{base_name}.xlsx"
        consolidator.save_excel(df, excel_path)
        print(f"  {GREEN}✓{RESET} Excel saved (clean formatting, auto-sized columns)")
        
        # Summary
        print(f"\n{DIM}{'─' * 80}{RESET}")
        print(f"\n  {BOLD}{'SUMMARY':^76}{RESET}\n")
        print(f"{DIM}{'─' * 80}{RESET}\n")
        
        print(f"  {BOLD}Data{RESET}")
        print(f"    {DIM}Schema Type{RESET}   {schema_info['type']}")
        print(f"    {DIM}Records{RESET}       {len(df)}")
        print(f"    {DIM}Columns{RESET}       {len(df.columns)}")
        
        # Category breakdown
        if schema_info.get('category_field'):
            category_display = ''.join([' ' + c if c.isupper() else c for c in schema_info.get('category_field', '')]).strip().title()
            if category_display and category_display in df.columns:
                top_categories = df[category_display].value_counts().head(5)
                if not top_categories.empty:
                    print(f"\n  {BOLD}Top {category_display}s{RESET}")
                    for cat, count in top_categories.items():
                        print(f"    {DIM}•{RESET} {cat}: {GREEN}{count}{RESET}")
        
        print(f"\n  {BOLD}{GREEN}✓ Output Location{RESET}")
        print(f"    {BOLD}{excel_path.absolute()}{RESET}")
        print(f"\n{DIM}{'─' * 80}{RESET}\n")
        
    except Exception as e:
        print(f"\n  {RED}✗{RESET} Error: {str(e)}")
        if '--verbose' in sys.argv or '-v' in sys.argv:
            import traceback
            traceback.print_exc()
        sys.exit(1)

