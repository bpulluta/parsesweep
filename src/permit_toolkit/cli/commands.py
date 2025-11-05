"""CLI commands for permit toolkit."""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple

import click
from dotenv import load_dotenv

from permit_toolkit.utils.config import get_config
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.consolidation import PermitConsolidator
from permit_toolkit.consolidation.cleaner import ExtractionCleaner


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


def check_api_keys(use_azure: bool = False) -> Tuple[bool, str]:
    """
    Check if required API keys are configured.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if use_azure:
        required_keys = ['AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT']
        missing = [key for key in required_keys if not os.getenv(key)]
        
        if missing:
            error = f"Azure OpenAI credentials not found"
            return False, error
    else:
        if not os.getenv('OPENAI_API_KEY'):
            error = f"OpenAI API key not found"
            return False, error
    
    return True, None


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
@click.option('--state', help='State name (auto-detected from path if not specified)')
@click.option('--model', default='gpt-4o-mini', show_default=True, help='AI model: gpt-4o-mini (fast) or gpt-4o (accurate)')
@click.option('--enable-qa-qc', is_flag=True, help='Enable detailed validation (slower, adds traceability)')
@click.option('--use-azure', is_flag=True, help='Use Azure OpenAI (requires AZURE_OPENAI_API_KEY)')
@click.option('--limit', '-n', type=int, help='Process only first N files')
@click.option('--skip-existing/--reprocess', default=True, show_default=True, help='Skip files already processed')
def extract(path: str, output: Optional[str], state: Optional[str],
            model: str, enable_qa_qc: bool, use_azure: bool, limit: Optional[int],
            skip_existing: bool):
    """
    Extract structured data from permit PDF files.
    
    This command reads air quality permits (PDF format) and extracts structured
    information about backup generators, emissions limits, and facility details.
    
    The output will be saved as JSON files in a parallel folder structure.
    For example: permits/Virginia/ → extracted/Virginia/
    
    \b
    EXAMPLES:
        # Extract a single permit file
        permit-toolkit extract permits/Virginia/11790_DC_Permit.pdf
        
        # Extract all permits in a directory
        permit-toolkit extract permits/Virginia
        
        # Extract just the first 5 permits (useful for testing)
        permit-toolkit extract permits/Virginia -n 5
        
        # Use Azure OpenAI (if you have Azure credits)
        permit-toolkit extract permits/Illinois --use-azure
        
        # Reprocess files that were already extracted
        permit-toolkit extract permits/Virginia --reprocess
    
    \b
    REQUIREMENTS:
        • PDF files in the specified directory
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
    
    # Check API keys before starting
    is_valid, error_msg = check_api_keys(use_azure)
    if not is_valid:
        provider = "Azure OpenAI" if use_azure else "OpenAI"
        print_error(
            error_msg,
            f"API credentials are required to extract data using {provider}.",
            [
                "Create a .env file in your project root if you don't have one",
                f"Add your API key: {'AZURE_OPENAI_API_KEY' if use_azure else 'OPENAI_API_KEY'}=your-key-here",
                f"{'Also add AZURE_OPENAI_ENDPOINT=your-endpoint-url' if use_azure else ''}",
                "Get an API key from: https://platform.openai.com/api-keys" if not use_azure else "Get Azure credentials from: https://portal.azure.com"
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
    
    # Setup output directory - parallel folder structure for clean pipeline
    # Examples: 
    #   validation/permits/Virginia/ → validation/extracted/Virginia/
    #   data/permits/Illinois/ → data/extracted/Illinois/
    #   validation/permits/ → validation/extracted/permits/
    if output:
        # User specified output - use it
        output_dir = Path(output)
    elif is_dir:
        # Directory input: replace 'permits' with 'extracted' in path
        # This maintains the same depth and structure
        parts = list(path.parts)
        if 'permits' in parts:
            # Replace first occurrence of 'permits' with 'extracted'
            idx = parts.index('permits')
            parts[idx] = 'extracted'
            output_dir = Path(*parts)
        else:
            # Fallback: create parallel 'extracted' folder
            parent = path.parent
            output_dir = parent / 'extracted' / path.name
    else:
        # Single file: same logic but for parent directory
        parts = list(path.parent.parts)
        if 'permits' in parts:
            idx = parts.index('permits')
            parts[idx] = 'extracted'
            output_dir = Path(*parts)
        else:
            # Fallback
            grandparent = path.parent.parent
            folder_name = path.parent.name
            output_dir = grandparent / 'extracted' / folder_name
    
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
    
    # Get PDF files - support recursive discovery
    if is_dir:
        # First try direct PDFs in this directory
        pdf_files = sorted(path.glob("*.pdf"))
        
        # If no PDFs found, look for subdirectories (e.g., state folders)
        if not pdf_files:
            subdirs = [d for d in path.iterdir() if d.is_dir() and not d.name.startswith('.')]
            if subdirs:
                # Found subdirectories - process each one recursively
                print(f"\n{BOLD}📁 Found {len(subdirs)} subfolder(s) with permits{RESET}")
                for subdir in subdirs:
                    subdir_pdfs = sorted(subdir.glob("*.pdf"))
                    if subdir_pdfs:
                        print(f"  → {subdir.name}: {len(subdir_pdfs)} PDF(s)")
                
                # Call extract for each subdirectory
                for subdir in subdirs:
                    subdir_pdfs = sorted(subdir.glob("*.pdf"))
                    if subdir_pdfs:
                        import subprocess
                        cmd = ['pixi', 'run', 'permit-toolkit', 'extract', str(subdir)]
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
        
        # Check if we found any PDFs at all
        if not pdf_files and not subdirs:
            print_error(
                f"No PDF files found in: {path}",
                "The directory exists but doesn't contain any PDF files.",
                [
                    "Make sure your PDF files have the .pdf extension",
                    "Check if PDFs are in a subdirectory",
                    f"Use 'ls {path}' to see what's in this folder",
                    "PDFs should be air quality permits for backup generators"
                ]
            )
            sys.exit(1)
        
        if limit:
            pdf_files = pdf_files[:limit]
        if skip_existing:
            original_count = len(pdf_files)
            pdf_files = [p for p in pdf_files if not (output_dir / f"{p.stem}.json").exists()]
            skipped = original_count - len(pdf_files)
            if skipped > 0 and len(pdf_files) > 0:
                print(f"\n{CYAN}ℹ{RESET}  Skipping {skipped} already processed file{'s' if skipped != 1 else ''}")
                print(f"   {DIM}(use --reprocess to extract them again){RESET}")
    else:
        # Single file
        if not path.suffix.lower() == '.pdf':
            print_error(
                f"File is not a PDF: {path.name}",
                "This tool only works with PDF files containing air quality permits.",
                [
                    "Make sure the file has a .pdf extension",
                    "Check if you specified the correct file path"
                ]
            )
            sys.exit(1)
        pdf_files = [path]
    
    # Final validation - check if we have files to process
    if not pdf_files:
        if is_dir:
            print(f"\n{GREEN}✓{RESET} All {original_count} file(s) already processed!")
            print(f"  {DIM}Output directory: {output_dir}{RESET}")
            print(f"\n  {DIM}Use --reprocess to extract them again{RESET}\n")
        return
    
    # Header with clean visual separation
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'AIR QUALITY PERMIT EXTRACTION':^76}{RESET} {BOLD}{BLUE}│{RESET}")
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
        print(f"  {DIM}Provider{RESET}  {MAGENTA}Azure OpenAI{RESET}")
    else:
        print(f"  {DIM}Model{RESET}     {model}")
    
    qa_status = f"{GREEN}Enabled{RESET}" if enable_qa_qc else f"{DIM}Disabled{RESET}"
    print(f"  {DIM}QA/QC{RESET}     {qa_status}")
    print(f"  {DIM}Files{RESET}     {BOLD}{len(pdf_files)}{RESET} PDF{'s' if len(pdf_files) != 1 else ''}")
    
    # Initialize extractor
    schema = load_schema(config.default_schema)
    
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
        
        azure_client = AzureOpenAI(
            api_key=azure_key,
            api_version=azure_version,
            azure_endpoint=azure_endpoint
        )
        extractor = PermitExtractor(api_key=azure_key, model=deployment_name)
        extractor.client = azure_client
    else:
        if not config.openai_api_key:
            print("❌ OPENAI_API_KEY not found")
            print(f"   Create .env file at: {config.project_root / '.env'}")
            return
        extractor = PermitExtractor(api_key=config.openai_api_key, model=model)
    
    # Processing section
    print(f"\n{DIM}{'─' * 80}{RESET}")
    
    results = []
    total_cost = 0.0
    total_time = 0.0
    
    for i, pdf_path in enumerate(pdf_files, 1):
        # Progress indicator with cleaner format
        if len(pdf_files) > 1:
            pct = (i - 1) / len(pdf_files)
            bar_len = 30
            filled = int(bar_len * pct)
            bar = f"{GREEN}{'█' * filled}{RESET}{DIM}{'░' * (bar_len - filled)}{RESET}"
            status = f"{CYAN}[{i}/{len(pdf_files)}]{RESET}"
            print(f"\n  {status} {bar} {pdf_path.name}")
        else:
            print(f"\n  {CYAN}→{RESET} {pdf_path.name}")
        
        try:
            # Extract
            text = extract_text_from_pdf(pdf_path)
            result = extractor.extract(text, schema, enable_qa_qc=enable_qa_qc)
            
            # Count total generators (sum across all generator sets)
            generator_sets = result.data.get('generatorSets', [])
            num_gens = sum(gen_set.get('numGenerators', 0) or 0 for gen_set in generator_sets)
            permit_num = result.data.get('permitDetails', {}).get('permitNumber', 'N/A')
            
            output_data = {
                'source_file': pdf_path.name,
                'extraction_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'state': state,
                'model': actual_model,  # Use actual model name (Azure deployment or OpenAI model)
                'qa_qc_enabled': enable_qa_qc,
                'cost_usd': result.cost,
                'processing_time_sec': result.processing_time,
                'completeness_score': result.completeness_score,
                'generator_count': num_gens,
                'permit_number': permit_num,
                'data': result.data,
                'validation_notes': result.validation_notes
            }
            
            output_file = output_dir / f"{pdf_path.stem}.json"
            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            # Track results
            results.append({
                'file': pdf_path.name,
                'permit': permit_num,
                'generators': num_gens,
                'cost': result.cost,
                'time': result.processing_time,
                'success': True
            })
            total_cost += result.cost
            total_time += result.processing_time
            
            # Success message with compact format and colors
            gen_text = f"{GREEN}{num_gens}{RESET} generators"
            permit_text = f"Permit {CYAN}{permit_num}{RESET}"
            cost_text = f"{MAGENTA}${result.cost:.4f}{RESET}"
            time_text = f"{DIM}{result.processing_time:.1f}s{RESET}"
            print(f"     {GREEN}✓{RESET} {gen_text}  {DIM}•{RESET}  {permit_text}  {DIM}•{RESET}  {cost_text}  {DIM}•{RESET}  {time_text}")
            
        except Exception as e:
            results.append({
                'file': pdf_path.name,
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
        
        # Generator count
        total_gens = sum(r.get('generators', 0) or 0 for r in successful)
        print(f"\n  {BOLD}Generators{RESET}")
        print(f"    {DIM}Extracted{RESET}    {GREEN}{total_gens}{RESET}")
    
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
        permit-toolkit validate data/extracted/Virginia/11790_DC_Permit.json
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
@click.argument('input_dir', type=click.Path())
@click.option('--output', '-o', type=click.Path(), help='Output file path (auto-detected if not specified)')
@click.option('--state', help='Filter by specific state (e.g., Virginia, Illinois)')
@click.option('--format', type=click.Choice(['csv', 'excel', 'json'], case_sensitive=False), 
              default='csv', show_default=True, help='Output format: csv, excel, or json')
def consolidate(input_dir: str, output: Optional[str], state: Optional[str], format: str):
    """
    Consolidate extracted JSON files into analysis-ready datasets.
    
    This command takes the JSON files created by the extract command and combines
    them into a single spreadsheet or data file. Each row represents one backup
    generator set with all its details.
    
    The output will be saved in a parallel "outputs" folder.
    For example: extracted/Virginia/ → outputs/Virginia/
    
    \b
    EXAMPLES:
        # Consolidate all extractions in a directory
        permit-toolkit consolidate extracted/Virginia
        
        # Consolidate and save to Excel
        permit-toolkit consolidate extracted/Illinois --format excel
        
        # Custom output location
        permit-toolkit consolidate extracted/Virginia -o my_data.csv
        
        # Consolidate mixed states, filter for one state
        permit-toolkit consolidate extracted --state Virginia
    
    \b
    REQUIREMENTS:
        • JSON files from the extract command
        • At least one extracted permit file
    
    \b
    OUTPUT:
        The tool creates a table with columns for:
        • Permit details (number, dates, facility info)
        • Generator specs (make, model, HP, fuel type)
        • Emissions limits (NOx, CO, VOC, PM)
        • Operating restrictions and hours
    """
    # Load environment for any config needs
    load_dotenv()
    
    # Validate input directory exists
    input_dir = Path(input_dir)
    if not input_dir.exists():
        print_error(
            f"Directory not found: {input_dir}",
            "The extraction directory you specified doesn't exist.",
            [
                "Run 'extract' command first to create extracted JSON files",
                "Check the path spelling and try again",
                f"Current directory: {Path.cwd()}",
                "Example: permit-toolkit consolidate extracted/Virginia"
            ]
        )
        sys.exit(1)
    
    if not input_dir.is_dir():
        print_error(
            f"Not a directory: {input_dir}",
            "Please provide a directory containing extracted JSON files.",
            [
                "Use the path to a folder, not a single file",
                "Example: permit-toolkit consolidate extracted/Virginia"
            ]
        )
        sys.exit(1)
    
    # Check if input_dir contains JSON files directly or has subdirectories
    has_direct_jsons = bool(list(input_dir.glob("*.json")))
    subdirs = [d for d in input_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]
    
    # Auto-infer state from directory name if not provided
    # Only treat as state if directory contains JSON files directly
    if not state and has_direct_jsons and input_dir.name not in ['extracted', 'data', 'aqtoolkit']:
        state = input_dir.name
    
    # Determine output file path - parallel folder structure like extraction
    # Examples:
    #   validation/aqtoolkit/Virginia/ → validation/outputs/virginia_consolidated.csv
    #   validation/aqtoolkit/ → validation/outputs/consolidated.csv
    #   data/extracted/Illinois/ → data/outputs/illinois_consolidated.csv
    if output:
        # User specified output - use it
        output = Path(output)
    else:
        # Auto-determine output location based on input path
        parts = list(input_dir.parts)
        
        # Replace extraction/cleaning folder names with output folder
        replaced = False
        for extract_folder in ['extracted', 'cleaned', 'aqtoolkit', 'llamaextract', 'decisiontree']:
            if extract_folder in parts:
                idx = parts.index(extract_folder)
                # Use 'compiled' for cleaned data, 'outputs' for others
                parts[idx] = 'compiled' if extract_folder == 'cleaned' else 'outputs'
                replaced = True
                break
        
        if replaced:
            output_dir = Path(*parts)
        else:
            # Fallback: create parallel 'compiled' or 'outputs' folder
            parent = input_dir.parent
            # Use 'compiled' if input suggests it's cleaned data
            folder_name = 'compiled' if 'clean' in input_dir.name.lower() else 'outputs'
            output_dir = parent / folder_name
        
        # Create filename with optional state suffix
        state_suffix = f"{state.lower()}_" if state else ""
        ext = 'xlsx' if format == 'excel' else format
        output = output_dir / f"{state_suffix}consolidated.{ext}"
    
    output.parent.mkdir(parents=True, exist_ok=True)
    
    # Header with clean visual separation
    # Header with clean visual separation
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'DATA CONSOLIDATION':^76}{RESET} {BOLD}{BLUE}│{RESET}")
    print(f"{BOLD}{BLUE}└{'─' * 78}┘{RESET}\n")
    
    # Configuration display
    print(f"  {DIM}Input{RESET}     {input_dir}")
    print(f"  {DIM}Output{RESET}    {output}")
    if state:
        print(f"  {DIM}State{RESET}     {CYAN}{state}{RESET}")
    print(f"  {DIM}Format{RESET}    {format.upper()}")
    
    # Find JSON files - use consolidator to check
    print(f"  {DIM}Files{RESET}     ", end='', flush=True)
    
    # Find JSON files - check if input_dir itself contains JSONs or has subdirectories
    if state and (input_dir / state).exists():
        # State subdirectory exists
        json_files = list((input_dir / state).glob("*.json"))
    elif state:
        # input_dir IS the state directory
        json_files = list(input_dir.glob("*.json"))
    else:
        # No state filter, search recursively
        json_files = list(input_dir.glob("**/*.json"))
    
    if not json_files:
        print(f"0\n")
        print_error(
            f"No JSON files found in: {input_dir}",
            "This directory doesn't contain any extracted permit data." + (f" (filtering for state: {state})" if state else ""),
            [
                "Run the 'extract' command first to process PDF permits",
                "Example: permit-toolkit extract permits/Virginia",
                "Then consolidate: permit-toolkit consolidate extracted/Virginia",
                "Check that you're using the correct directory path"
            ]
        )
        sys.exit(1)
    
    print(f"{len(json_files)}\n")
    
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    # Create consolidator and process
    start_time = time.time()
    
    print(f"  {CYAN}→{RESET} Processing JSON files...")
    
    consolidator = PermitConsolidator(
        extraction_dir=input_dir,
        state=state
    )
    if not json_files:
        print(f"\n\n{YELLOW}⚠{RESET}  No JSON files found in {input_dir}")
        if state:
            print(f"   (filtering for state: {state})")
        return
    
    print(f"{len(json_files)}\n")
    
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    # Create consolidator and process
    start_time = time.time()
    
    print(f"  {CYAN}→{RESET} Processing JSON files...")
    df = consolidator.consolidate()
    
    if len(df) == 0:
        print(f"  {YELLOW}⚠{RESET}  No records extracted\n")
        print(f"{DIM}{'─' * 80}{RESET}\n")
        return
    
    print(f"  {GREEN}✓{RESET} Consolidated {len(df)} records\n")
    
    # Save output in requested format
    print(f"  {CYAN}→{RESET} Saving to {format.upper()}...")
    if format == 'excel':
        consolidator.save_styled_excel(df, output)
    elif format == 'json':
        df.to_json(output, orient='records', indent=2)
    else:  # csv
        df.to_csv(output, index=False)
    
    processing_time = time.time() - start_time
    print(f"  {GREEN}✓{RESET} Saved {output.name}\n")
    
    # Summary section
    print(f"{DIM}{'─' * 80}{RESET}\n")
    print(f"{BOLD}{'SUMMARY':^80}{RESET}\n")
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    # Calculate statistics
    num_facilities = df['facility_name'].nunique() if 'facility_name' in df.columns else 0
    num_permits = df['permit_number'].nunique() if 'permit_number' in df.columns else 0
    
    # Sum generators (handle the num_generators column correctly)
    if 'num_generators' in df.columns:
        # Each row represents a generator set with num_generators count
        total_generators = int(df['num_generators'].sum())
    else:
        total_generators = len(df)  # Fallback to row count
    
    # Completeness analysis
    avg_completeness = None
    if 'completeness_score' in df.columns:
        avg_completeness = df['completeness_score'].mean()
    
    # Data quality metrics
    records_with_emissions = 0
    if 'nox_limit_tons_yr' in df.columns:
        records_with_emissions = df['nox_limit_tons_yr'].notna().sum()
    
    # Dataset stats
    print(f"  {BOLD}Dataset{RESET}")
    print(f"    Generator Sets   {CYAN}{len(df)}{RESET}")
    print(f"    Facilities       {num_facilities}")
    print(f"    Permits          {num_permits}")
    print(f"    Total Generators {MAGENTA}{total_generators}{RESET}")
    
    # Quality metrics
    if avg_completeness is not None:
        color = GREEN if avg_completeness >= 0.8 else YELLOW if avg_completeness >= 0.6 else '\033[91m'
        print(f"\n  {BOLD}Quality{RESET}")
        print(f"    Avg Completeness {color}{avg_completeness:.1%}{RESET}")
    
    if records_with_emissions > 0:
        pct = (records_with_emissions / len(df)) * 100
        print(f"    With Emissions   {records_with_emissions} ({pct:.0f}%)")
    
    # File info
    file_size = output.stat().st_size
    size_kb = file_size / 1024
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
    
    print(f"\n  {BOLD}Output{RESET}")
    print(f"    Columns          {len(df.columns)}")
    print(f"    File Size        {size_str}")
    print(f"    Processing Time  {processing_time:.1f}s")
    
    print(f"\n  Output → {output}")
    
    print(f"\n{DIM}{'─' * 80}{RESET}\n")


@click.command()
@click.argument('input_dir', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output directory (default: data/cleaned)')
@click.option('--dry-run', is_flag=True, help='Preview what would be done without writing files')
@click.option('--deduplicate', is_flag=True, help='Remove true duplicates (same generators, keep newest)')
def clean(input_dir: str, output: Optional[str], dry_run: bool, deduplicate: bool):
    """
    Clean extracted permit data by removing zero-generator files and duplicates.
    
    This command processes extracted JSON files from data/extracted/ and creates
    cleaned output with:
      • State-organized JSON files (only files with generators)
      • reports/ - Detailed reports on what was removed/kept
    
    By default, all versions of permits are preserved (they may represent different
    amendments). Use --deduplicate to intelligently remove true duplicates (files
    with identical generator reference numbers).
    
    \b
    EXAMPLES:
        # Clean extracted data (preserve all versions)
        permit-toolkit clean data/extracted
        
        # Clean and remove true duplicates
        permit-toolkit clean data/extracted --deduplicate
        
        # Preview without making changes
        permit-toolkit clean data/extracted --dry-run
        
        # Specify custom output directory
        permit-toolkit clean data/extracted --output data/my_clean_data
    
    \b
    OUTPUT STRUCTURE:
        data/cleaned/
          ├── Illinois/         # Cleaned IL permits
          ├── Maryland/         # Cleaned MD permits
          ├── ...
          └── reports/          # JSON reports + summary
    
    \b
    WHAT GETS REMOVED:
        • Files with 0 generators
        • True duplicates (with --deduplicate flag)
    
    \b
    WHAT GETS KEPT:
        • All files with generators (by default, all versions preserved)
        • With --deduplicate: only newest version of true duplicates
        • Original files in data/extracted/ remain untouched
    """
    # Setup paths
    input_dir = Path(input_dir)
    output_dir = Path(output) if output else Path("data/cleaned")
    
    # Header
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'DATA CLEANING':^76}{RESET} {BOLD}{BLUE}│{RESET}")
    print(f"{BOLD}{BLUE}└{'─' * 78}┘{RESET}\n")
    
    print(f"  {DIM}Mode{RESET}      {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  {DIM}Input{RESET}     {input_dir}")
    print(f"  {DIM}Output{RESET}    {output_dir}\n")
    
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    # Create cleaner
    cleaner = ExtractionCleaner(input_dir, output_dir)
    
    # Collect files
    print(f"  {CYAN}→{RESET} Collecting extraction files...")
    permit_groups = cleaner.collect_all_files()
    print(f"  {GREEN}✓{RESET} Found {cleaner.stats['total_files']} files across {len(permit_groups)} permit numbers\n")
    
    # Identify versions
    print(f"  {CYAN}→{RESET} Identifying permit versions...")
    cleaner.permit_versions = cleaner.identify_permit_versions(permit_groups, deduplicate=deduplicate)
    print(f"  {GREEN}✓{RESET} Found {cleaner.stats['permit_version_groups']} permits with multiple versions")
    print(f"  {GREEN}✓{RESET} Found {cleaner.stats['zero_generator_files']} files with 0 generators")
    if deduplicate and cleaner.stats.get('true_duplicates_found', 0) > 0:
        print(f"  {GREEN}✓{RESET} Identified {cleaner.stats['true_duplicates_found']} true duplicate(s)")
    print()
    
    if dry_run:
        files_with_gens = sum(
            1 for files in permit_groups.values()
            for f in files if f["generator_count"] > 0
        )
        removed_text = ""
        if deduplicate and cleaner.stats.get('duplicate_files_removed', 0) > 0:
            removed = cleaner.stats['duplicate_files_removed']
            removed_text = f" ({removed} duplicates would be removed)"
        print(f"  {YELLOW}⚠{RESET}  DRY RUN: Would create:")
        print(f"     • cleaned/: {files_with_gens}{removed_text} files")
        print(f"     • reports/: 4 report files\n")
        print(f"{DIM}{'─' * 80}{RESET}\n")
        return
    
    # Process
    start_time = time.time()
    
    print(f"  {CYAN}→{RESET} Setting up directories...")
    cleaner.setup_directories()
    print_success("Created output directories")
    print()
    
    print(f"  {CYAN}→{RESET} Creating cleaned dataset...")
    cleaner.create_cleaned_dataset(permit_groups, deduplicate=deduplicate)
    print_success(f"Created cleaned dataset with {cleaner.stats['final_cleaned_files']} files")
    if deduplicate and cleaner.stats.get('duplicate_files_removed', 0) > 0:
        print(f"  {DIM}Removed {cleaner.stats['duplicate_files_removed']} duplicate file(s){RESET}")
    print()
    
    print(f"  {CYAN}→{RESET} Generating reports...")
    cleaner.generate_reports()
    print_success("Generated detailed reports")
    print()
    
    processing_time = time.time() - start_time
    
    # Summary
    print(f"{DIM}{'─' * 80}{RESET}\n")
    print(f"{BOLD}{'SUMMARY':^80}{RESET}\n")
    print(f"{DIM}{'─' * 80}{RESET}\n")
    
    print(f"  {BOLD}Input{RESET}")
    print(f"    Total Files      {cleaner.stats['total_files']}")
    print(f"    With Generators  {cleaner.stats['files_with_generators']}")
    print(f"    Zero Generators  {cleaner.stats['zero_generator_files']}")
    print(f"    Version Groups   {cleaner.stats['permit_version_groups']}")
    
    print(f"  {BOLD}Output{RESET}")
    print(f"    Cleaned Files    {cleaner.stats['final_cleaned_files']}")
    if deduplicate:
        dup_removed = cleaner.stats.get('duplicate_files_removed', 0)
        if dup_removed > 0:
            print(f"    Duplicates       {dup_removed} removed")
    print(f"    Reports          {len(list((output_dir / 'reports').glob('*.json'))) + 1}")
    
    print(f"\n  {BOLD}Performance{RESET}")
    print(f"    Processing Time  {processing_time:.1f}s")
    
    print(f"\n  Output → {output_dir}")
    print(f"\n  {DIM}💡 Review reports/permit_versions.json for version details{RESET}")
    print(f"\n{DIM}{'─' * 80}{RESET}\n")

