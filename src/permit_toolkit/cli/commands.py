"""CLI commands for permit toolkit."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import click

from permit_toolkit.utils.config import get_config
from permit_toolkit.utils.logger import get_logger, ExtractionMetrics
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.consolidation import PermitConsolidator


@click.command()
@click.argument('path', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output directory')
@click.option('--state', help='State name (inferred from path if not provided)')
@click.option('--model', default='gpt-4o-mini', show_default=True, help='Model to use')
@click.option('--enable-qa-qc', is_flag=True, help='Enable LangExtract QA/QC (slower but adds traceability)')
@click.option('--use-azure', is_flag=True, help='Use Azure OpenAI')
@click.option('--limit', '-n', type=int, help='Process only first N files (directory only)')
@click.option('--skip-existing/--reprocess', default=True, show_default=True, help='Skip already processed files')
def extract(path: str, output: Optional[str], state: Optional[str],
            model: str, enable_qa_qc: bool, use_azure: bool, limit: Optional[int],
            skip_existing: bool):
    """
    Extract structured data from permit PDFs (single file or directory).
    
    \b
    Examples:
        # Single file
        permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf
        
        # Directory - extract first 5 Virginia permits (fast mode)
        permit-toolkit extract data/permits/Virginia -n 5
        
        # Directory with full QA/QC validation
        permit-toolkit extract data/permits/Virginia --enable-qa-qc
        
        # Use Azure OpenAI with higher rate limits
        permit-toolkit extract data/permits/Illinois --use-azure
    """
    import time
    import json
    from permit_toolkit.extraction import PermitExtractor, load_schema
    from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
    from permit_toolkit.utils.config import get_config
    
    config = get_config()
    path = Path(path)
    
    # Determine if single file or directory
    is_dir = path.is_dir()
    
    # Infer state from path
    if is_dir:
        state = state or path.name
    else:
        state = state or path.parent.name
    
    # Setup output directory
    output_dir = Path(output) if output else (config.data_root / 'extracted' / state)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Get PDF files
    if is_dir:
        pdf_files = sorted(path.glob("*.pdf"))
        if limit:
            pdf_files = pdf_files[:limit]
        if skip_existing:
            original_count = len(pdf_files)
            pdf_files = [p for p in pdf_files if not (output_dir / f"{p.stem}.json").exists()]
            skipped = original_count - len(pdf_files)
            if skipped > 0 and len(pdf_files) > 0:
                print(f"⏭️  Skipping {skipped} already processed file{'s' if skipped != 1 else ''}")
    else:
        pdf_files = [path]
    
    if not pdf_files:
        print("✅ All files already processed!" if is_dir else f"❌ File not found: {path}")
        return
    
    # ANSI color codes
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    
    # Header with clean visual separation
    print(f"\n{BOLD}{BLUE}┌{'─' * 78}┐{RESET}")
    print(f"{BOLD}{BLUE}│{RESET} {BOLD}{'AIR QUALITY PERMIT EXTRACTION':^76}{RESET} {BOLD}{BLUE}│{RESET}")
    print(f"{BOLD}{BLUE}└{'─' * 78}┘{RESET}")
    
    # Configuration table with colors
    print(f"\n  {DIM}State{RESET}     {CYAN}{state}{RESET}")
    print(f"  {DIM}Input{RESET}     {path}")
    print(f"  {DIM}Output{RESET}    {output_dir}")
    
    # Display model info
    if use_azure:
        azure_model = os.environ.get('AZURE_OPENAI_MODEL')
        display_model = azure_model if azure_model else model
        print(f"  {DIM}Model{RESET}     {display_model}")
        print(f"  {DIM}Provider{RESET}  {MAGENTA}Azure OpenAI{RESET}")
    else:
        print(f"  {DIM}Model{RESET}     {model}")
    
    qa_status = f"{GREEN}Enabled{RESET}" if enable_qa_qc else f"{DIM}Disabled{RESET}"
    print(f"  {DIM}QA/QC{RESET}     {qa_status}")
    print(f"  {DIM}Files{RESET}     {BOLD}{len(pdf_files)}{RESET}")
    
    # Initialize extractor
    schema = load_schema(config.default_schema)
    
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
                'model': model,
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
        total_gens = sum(r['generators'] for r in successful)
        print(f"\n  {BOLD}Generators{RESET}")
        print(f"    {DIM}Extracted{RESET}    {GREEN}{total_gens}{RESET}")
    
    print(f"\n  {DIM}Output → {RESET}{output_dir}")
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
    logger = get_logger(verbose=verbose)
    
    logger.header("VALIDATION")
    
    extraction_file = Path(extraction_file)
    logger.info(f"File: {extraction_file.name}")
    
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
        logger.success("Schema validation passed")
    except ValidationError as e:
        logger.error(f"Schema validation failed: {e.message}")
        sys.exit(1)
    
    # Check data quality
    permit_data = data.get('data', data)
    generators = permit_data.get('generatorSets', [])
    
    logger.section("Extraction Quality")
    
    # Use metadata if available (new format)
    if 'quality' in data:
        quality = data['quality']
        logger.info(f"Generator Count:    {quality['generator_count']}")
        logger.info(f"Completeness Score: {quality['completeness_score']:.1%}")
        logger.info(f"Fields Extracted:   {quality['fields_extracted']}")
        logger.info(f"Fields Missing:     {quality['fields_missing']}")
        logger.info(f"Has Permit Details: {'Yes' if quality['has_permit_details'] else 'No'}")
        logger.info(f"Has Generators:     {'Yes' if quality['has_generators'] else 'No'}")
        logger.info(f"Has Emissions Data: {'Yes' if quality['has_emissions_data'] else 'No'}")
    else:
        # Fallback for old format
        logger.info(f"Generator Sets: {len(generators)}")
        
        if generators:
            # Check completeness
            complete_count = sum(
                1 for g in generators
                if g.get('make') and g.get('model') and g.get('fuelType')
            )
            logger.info(f"Complete Records: {complete_count}/{len(generators)} "
                       f"({complete_count/len(generators)*100:.1f}%)")
            
            # Check for emissions data
            with_emissions = sum(
                1 for g in generators
                if any(g.get(k) for k in ['noxEmissionLimitLbsHr', 'coEmissionLimitLbsHr', 'vocEmissionLimitLbsHr'])
            )
            logger.info(f"With Emissions: {with_emissions}/{len(generators)} "
                       f"({with_emissions/len(generators)*100:.1f}%)")
    
    # Check metadata
    if 'cost' in data:
        if isinstance(data['cost'], dict):
            logger.info(f"\nExtraction Cost: ${data['cost']['total_usd']:.4f}")
            logger.info(f"  OpenAI:        ${data['cost']['openai_usd']:.4f}")
            logger.info(f"  LangExtract:   ${data['cost']['langextract_usd']:.4f}")
        else:
            logger.info(f"\nExtraction Cost: ${data['cost']:.4f}")
    
    if 'timing' in data:
        logger.info(f"Processing Time: {data['timing']['total_sec']:.2f}s")
        logger.info(f"  OpenAI:        {data['timing']['openai_sec']:.2f}s")
        logger.info(f"  LangExtract:   {data['timing']['langextract_sec']:.2f}s")
    elif 'processing_time_sec' in data:
        logger.info(f"Processing Time: {data['processing_time_sec']:.2f}s")
    
    logger.success("\nValidation complete!")


@click.command()
@click.argument('input_dir', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output CSV file path')
@click.option('--state', help='Filter by state (e.g., Virginia, Illinois)')
@click.option('--format', type=click.Choice(['csv', 'excel', 'json'], case_sensitive=False), 
              default='csv', show_default=True, help='Output format')
def consolidate(input_dir: str, output: Optional[str], state: Optional[str], format: str):
    """
    Consolidate extracted JSON files into analysis-ready datasets.
    
    Flattens nested JSON structures into tabular format with one row per
    generator set, including all permit details and emissions data.
    
    \b
    Examples:
        # Consolidate all Virginia extractions
        permit-toolkit consolidate data/extracted/Virginia
        
        # Consolidate with custom output
        permit-toolkit consolidate data/extracted/Illinois -o il_dataset.csv
        
        # Filter by state from mixed directory
        permit-toolkit consolidate data/extracted --state Virginia
        
        # Export to Excel
        permit-toolkit consolidate data/extracted/Virginia --format excel
    """
    # ANSI color codes
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    
    input_dir = Path(input_dir)
    
    # Auto-infer state from directory name if not provided
    if not state and input_dir.name not in ['extracted', 'data']:
        state = input_dir.name
    
    # Determine output file path
    if not output:
        state_suffix = f"_{state.lower()}" if state else ""
        ext = 'xlsx' if format == 'excel' else format
        output = Path(f"data/outputs/consolidated{state_suffix}.{ext}")
    else:
        output = Path(output)
    
    output.parent.mkdir(parents=True, exist_ok=True)
    
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
    
    # Create consolidator first to let it find files
    consolidator = PermitConsolidator(
        extraction_dir=input_dir,
        state=state
    )
    
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
        df.to_excel(output, index=False, engine='openpyxl')
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
