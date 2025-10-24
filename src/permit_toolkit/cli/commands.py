"""CLI commands for permit toolkit."""

import json
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
@click.argument('pdf_path', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output directory for results')
@click.option('--model', default='gpt-4o-mini', help='OpenAI model to use')
@click.option('--no-viz', is_flag=True, help='Skip visualization generation')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def extract(pdf_path: str, output: Optional[str], model: str, no_viz: bool, verbose: bool):
    """
    Extract structured data from a single permit PDF.
    
    Example:
        permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf
    """
    config = get_config()
    
    # Setup logger
    log_file = Path(output) / 'extraction.log' if output else None
    logger = get_logger(log_file=log_file, verbose=verbose)
    
    logger.header("PERMIT EXTRACTION")
    
    # Validate API key
    if not config.openai_api_key:
        logger.error("OPENAI_API_KEY not found in environment")
        logger.info(f"Create .env file at: {config.project_root / '.env'}")
        sys.exit(1)
    
    pdf_path = Path(pdf_path)
    
    # Setup output directory
    if output:
        output_dir = Path(output)
    else:
        output_dir = config.data_root / 'extracted' / pdf_path.parent.name
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load schema
    schema = load_schema(config.default_schema)
    
    logger.section("Configuration")
    logger.info(f"PDF:        {pdf_path.name}")
    logger.info(f"Model:      {model}")
    logger.info(f"Output:     {output_dir}")
    
    # Extract text
    logger.section("Processing")
    logger.info("Extracting text from PDF...")
    try:
        text = extract_text_from_pdf(pdf_path)
        logger.success(f"Extracted {len(text):,} characters")
    except Exception as e:
        logger.error(f"Failed to extract text: {e}")
        sys.exit(1)
    
    # Create extractor
    extractor = PermitExtractor(api_key=config.openai_api_key, model=model)
    
    # Extract data
    logger.info("Extracting structured data...")
    
    try:
        result = extractor.extract(text, schema, enable_validation=True)
        
        # Display clear metrics
        logger.section("Extraction Metrics")
        logger.info(f"Total Cost:      ${result.metadata.total_cost_usd:.4f}")
        logger.info(f"  OpenAI:        ${result.metadata.openai_cost_usd:.4f} ({result.metadata.openai_time_sec:.2f}s)")
        logger.info(f"  LangExtract:   ${result.metadata.langextract_cost_usd:.4f} ({result.metadata.langextract_time_sec:.2f}s)")
        
        logger.section("Data Quality")
        logger.info(f"Generator Entries:   {result.metadata.generator_count}")
        logger.info(f"Total Units:         {result.metadata.total_units}")
        logger.info(f"Fields Populated:    {result.metadata.fields_with_data}")
        logger.info(f"Critical Complete:   {'✓ Yes' if result.metadata.critical_fields_complete else '✗ No'}")
        
        logger.section("Validation Results")
        logger.info(f"OpenAI Found:        {result.metadata.openai_generator_count} generators")
        logger.info(f"LangExtract Found:   {result.metadata.langextract_generator_count} generators")
        logger.info(f"Counts Match:        {'✓ Yes' if result.metadata.generators_match else '⚠️  No'}")
        
        if result.metadata.conflicts_found > 0:
            logger.info(f"Conflicts Detected:  {result.metadata.conflicts_found}")
            logger.info(f"Conflicts Resolved:  {result.metadata.conflicts_resolved} (using LangExtract evidence)")
        if result.metadata.fields_augmented > 0:
            logger.info(f"Fields Augmented:    {result.metadata.fields_augmented} (from LangExtract)")
        
        if not result.metadata.conflicts_found and result.metadata.generators_match:
            logger.success("All data validated - OpenAI and LangExtract agree ✓")
        
        # Show validation actions if verbose
        if verbose and result.metadata.validation_actions:
            logger.section("Validation Actions")
            for action in result.metadata.validation_actions:
                logger.info(f"  {action}")
        
        # Save JSON result with clear metadata
        output_file = output_dir / f"{pdf_path.stem}.json"
        output_data = {
            'source_file': pdf_path.name,
            'extraction_date': time.strftime('%Y-%m-%d %H:%M:%S'),
            'model': model,
            
            # Cost breakdown
            'cost': {
                'total_usd': result.metadata.total_cost_usd,
                'openai_usd': result.metadata.openai_cost_usd,
                'langextract_usd': result.metadata.langextract_cost_usd
            },
            
            # Timing
            'timing': {
                'total_sec': result.metadata.total_time_sec,
                'openai_sec': result.metadata.openai_time_sec,
                'langextract_sec': result.metadata.langextract_time_sec
            },
            
            # Quality metrics
            'quality': {
                'generator_count': result.metadata.generator_count,
                'total_units': result.metadata.total_units,
                'fields_populated': result.metadata.fields_with_data,
                'critical_fields_complete': result.metadata.critical_fields_complete,
                'has_permit_details': result.metadata.has_permit_details,
                'has_facility_info': result.metadata.has_facility_info,
                'has_generators': result.metadata.has_generators,
                'has_emissions_data': result.metadata.has_emissions_data,
                'has_operating_restrictions': result.metadata.has_operating_restrictions
            },
            
            # Validation results
            'validation': {
                'openai_generator_count': result.metadata.openai_generator_count,
                'langextract_generator_count': result.metadata.langextract_generator_count,
                'generators_match': result.metadata.generators_match,
                'conflicts_found': result.metadata.conflicts_found,
                'conflicts_resolved': result.metadata.conflicts_resolved,
                'fields_augmented': result.metadata.fields_augmented,
                'actions': result.metadata.validation_actions
            },
            
            # Extracted data
            'data': result.data
        }
        
        with open(output_file, 'w') as f:
            json.dump(output_data, f, indent=2)
        
        logger.success(f"Saved JSON: {output_file}")
        
        # Generate visualization if LangExtract result available
        if not no_viz and result.langextract_result:
            logger.info("Generating visualization...")
            try:
                # Generate HTML visualization showing source citations
                viz_dir = output_dir / 'visualizations'
                viz_dir.mkdir(exist_ok=True)
                viz_file = viz_dir / f"{pdf_path.stem}.html"
                
                # Simple HTML generation with LangExtract extractions
                html_content = result.langextract_result.to_html() if hasattr(result.langextract_result, 'to_html') else None
                if html_content:
                    with open(viz_file, 'w') as f:
                        f.write(html_content)
                    logger.success(f"Saved visualization: {viz_file}")
                else:
                    logger.warning("LangExtract result does not support HTML visualization")
            except Exception as e:
                logger.warning(f"Visualization generation failed: {e}")
        
        # Summary
        logger.section("Extraction Summary")
        facility = result.data.get('permitDetails', {})
        if facility.get('facilityName'):
            logger.info(f"Facility:        {facility['facilityName']}")
        if facility.get('permitNumber'):
            logger.info(f"Permit:         {facility['permitNumber']}")
        
        logger.success("\nExtraction successful!")
        
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        if verbose:
            import traceback
            logger.debug(traceback.format_exc())
        sys.exit(1)


@click.command()
@click.argument('input_dir', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output directory for results')
@click.option('--state', help='State name (for organization)')
@click.option('--model', default='gpt-4o-mini', help='OpenAI model to use')
@click.option('--limit', '-n', type=int, help='Process only N files')
@click.option('--skip-existing', is_flag=True, default=True, help='Skip already processed files')
@click.option('--no-viz', is_flag=True, help='Skip visualization generation')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def batch_extract(input_dir: str, output: Optional[str], state: Optional[str], 
                  model: str, limit: Optional[int], skip_existing: bool, 
                  no_viz: bool, verbose: bool):
    """
    Extract data from multiple permit PDFs in a directory.
    
    Example:
        permit-toolkit batch-extract data/permits/Virginia --state Virginia
    """
    config = get_config()
    
    input_dir = Path(input_dir)
    
    # Infer state from path if not provided
    if not state:
        state = input_dir.name
    
    # Setup output directory
    if output:
        output_dir = Path(output)
    else:
        output_dir = config.data_root / state / 'extracted'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup logger
    log_file = output_dir / 'batch_extraction.log'
    logger = get_logger(log_file=log_file, verbose=verbose)
    
    logger.header(f"BATCH EXTRACTION - {state.upper()}")
    
    # Validate API key
    if not config.openai_api_key:
        logger.error("OPENAI_API_KEY not found in environment")
        logger.info(f"Create .env file at: {config.project_root / '.env'}")
        sys.exit(1)
    
    # Find PDF files
    pdf_files = sorted(input_dir.glob("*.pdf"))
    
    if not pdf_files:
        logger.error(f"No PDF files found in {input_dir}")
        sys.exit(1)
    
    # Apply limit
    if limit:
        pdf_files = pdf_files[:limit]
    
    # Filter already processed
    if skip_existing:
        original_count = len(pdf_files)
        pdf_files = [
            pdf for pdf in pdf_files
            if not (output_dir / f"{pdf.stem}.json").exists()
        ]
        skipped = original_count - len(pdf_files)
        if skipped > 0:
            logger.info(f"Skipping {skipped} already processed files")
        
        if not pdf_files:
            logger.success("All files already processed!")
            return
    
    logger.section("Configuration")
    logger.info(f"Input:      {input_dir}")
    logger.info(f"Output:     {output_dir}")
    logger.info(f"State:      {state}")
    logger.info(f"Model:      {model}")
    logger.info(f"Files:      {len(pdf_files)}")
    
    # Load schema
    schema = load_schema(config.default_schema)
    
    # Create extractor
    extractor = PermitExtractor(api_key=config.openai_api_key, model=model)
    
    # Initialize metrics
    metrics = ExtractionMetrics(total_files=len(pdf_files))
    
    logger.section("Processing")
    
    # Process each file
    for i, pdf_path in enumerate(pdf_files, 1):
        logger.progress(i, len(pdf_files), pdf_path.name)
        
        try:
            # Extract text
            text = extract_text_from_pdf(pdf_path)
            
            # Extract data
            result = extractor.extract(text, schema, enable_validation=True)
            
            # Save JSON with new metadata structure
            output_file = output_dir / f"{pdf_path.stem}.json"
            output_data = {
                'source_file': pdf_path.name,
                'extraction_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'state': state,
                'model': model,
                
                # Cost breakdown
                'cost': {
                    'total_usd': result.metadata.total_cost_usd,
                    'openai_usd': result.metadata.openai_cost_usd,
                    'langextract_usd': result.metadata.langextract_cost_usd
                },
                
                # Timing
                'timing': {
                    'total_sec': result.metadata.total_time_sec,
                    'openai_sec': result.metadata.openai_time_sec,
                    'langextract_sec': result.metadata.langextract_time_sec
                },
                
                # Quality metrics
                'quality': {
                    'generator_count': result.metadata.generator_count,
                    'total_units': result.metadata.total_units,
                    'fields_populated': result.metadata.fields_with_data,
                    'critical_fields_complete': result.metadata.critical_fields_complete,
                    'has_permit_details': result.metadata.has_permit_details,
                    'has_facility_info': result.metadata.has_facility_info,
                    'has_generators': result.metadata.has_generators,
                    'has_emissions_data': result.metadata.has_emissions_data,
                    'has_operating_restrictions': result.metadata.has_operating_restrictions
                },
                
                # Validation results
                'validation': {
                    'openai_generator_count': result.metadata.openai_generator_count,
                    'langextract_generator_count': result.metadata.langextract_generator_count,
                    'generators_match': result.metadata.generators_match,
                    'conflicts_found': result.metadata.conflicts_found,
                    'conflicts_resolved': result.metadata.conflicts_resolved,
                    'fields_augmented': result.metadata.fields_augmented,
                    'actions': result.metadata.validation_actions if verbose else []
                },
                
                # Extracted data
                'data': result.data
            }
            
            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            # Generate visualization if available
            if not no_viz and result.langextract_result:
                try:
                    viz_dir = output_dir / 'visualizations'
                    viz_dir.mkdir(exist_ok=True)
                    viz_file = viz_dir / f"{pdf_path.stem}.html"
                    
                    html_content = result.langextract_result.to_html() if hasattr(result.langextract_result, 'to_html') else None
                    if html_content:
                        with open(viz_file, 'w') as f:
                            f.write(html_content)
                except Exception as viz_error:
                    if verbose:
                        logger.warning(f"Visualization failed: {viz_error}")
            
            # Record metrics
            metrics.add_success(
                cost=result.metadata.total_cost_usd,
                duration=result.metadata.total_time_sec
            )
            
            # Log result with validation info
            status_msg = f"{result.metadata.generator_count} gens"
            if result.metadata.conflicts_resolved > 0:
                status_msg += f", {result.metadata.conflicts_resolved} corrections"
            if result.metadata.fields_augmented > 0:
                status_msg += f", +{result.metadata.fields_augmented} fields"
            
            logger.file_result(
                pdf_path.name, 
                "success",
                cost=result.metadata.total_cost_usd,
                duration=result.metadata.total_time_sec,
                details=status_msg
            )
            
        except Exception as e:
            metrics.add_failure()
            logger.file_result(pdf_path.name, "failed", details=str(e))
            if verbose:
                import traceback
                logger.debug(traceback.format_exc())
    
    # Print summary
    logger.metrics_summary(metrics)
    logger.info(f"\nLog file: {log_file}")


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
@click.argument('output_file', type=click.Path())
@click.option('--state', help='Filter by state')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def consolidate(input_dir: str, output_file: str, state: Optional[str], verbose: bool):
    """
    Consolidate multiple extraction JSON files into a CSV dataset.
    
    Example:
        permit-toolkit consolidate data/Virginia/extracted data/Virginia/dataset.csv --state Virginia
    """
    logger = get_logger(verbose=verbose)
    
    logger.header("CONSOLIDATION")
    
    input_dir = Path(input_dir)
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Input:  {input_dir}")
    logger.info(f"Output: {output_file}")
    if state:
        logger.info(f"State:  {state}")
    
    # Create consolidator
    consolidator = PermitConsolidator(
        extraction_dir=input_dir,
        state=state
    )
    
    # Consolidate
    logger.info("\nConsolidating extractions...")
    df = consolidator.consolidate_to_dataframe()
    
    # Save
    df.to_csv(output_file, index=False)
    
    logger.success(f"Created dataset with {len(df)} records")
    logger.info(f"Columns: {', '.join(df.columns[:5])}...")
    logger.info(f"\nSaved to: {output_file}")
