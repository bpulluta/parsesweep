"""Command-line interface for the permit toolkit."""

import logging
import sys
from pathlib import Path

import click

from permit_toolkit.utils.config import get_config
from permit_toolkit.scrapers.virginia import VirginiaScraper
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def cli(verbose):
    """Air Quality Permit Toolkit - Extract backup generator data from permits."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


@cli.command()
@click.option('--state', required=True, type=click.Choice(['virginia', 'illinois']),
              help='State to scrape permits from')
@click.option('--output', '-o', type=click.Path(), help='Output directory for PDFs')
@click.option('--test', type=int, metavar='N', help='Test mode: download only N permits')
@click.option('--latest-only', is_flag=True, help='Download only latest versions (Virginia only)')
def scrape(state, output, test, latest_only):
    """Scrape permit PDFs from state environmental agency websites."""
    config = get_config()
    
    if output:
        output_dir = Path(output)
    else:
        output_dir = config.get_permits_dir(state.capitalize())
    
    logger.info(f"Scraping permits for {state.upper()}")
    logger.info(f"Output directory: {output_dir}")
    
    if state == 'virginia':
        scraper = VirginiaScraper(
            output_dir=output_dir,
            test_mode=bool(test),
            test_count=test or 5,
            latest_only=latest_only,
            resume=True
        )
        scraper.run()
    elif state == 'illinois':
        click.echo("Illinois scraper not yet implemented. Use manual download.")
        sys.exit(1)
    else:
        click.echo(f"Scraper for {state} not implemented")
        sys.exit(1)


@cli.command()
@click.option('--state', required=True, help='State name (e.g., Virginia, Illinois)')
@click.option('--input', '-i', type=click.Path(exists=True), 
              help='Input directory containing PDF permits')
@click.option('--output', '-o', type=click.Path(), 
              help='Output directory for extracted JSON files')
@click.option('--test', type=int, metavar='N', help='Test mode: process only N permits')
@click.option('--permits', multiple=True, help='Specific permit numbers to process')
@click.option('--model', default='gpt-4o', help='OpenAI model to use')
@click.option('--reprocess', is_flag=True, help='Reprocess existing extractions')
def extract(state, input, output, test, permits, model, reprocess):
    """Extract structured data from permit PDFs using LLM."""
    config = get_config()
    
    # Check API key
    if not config.openai_api_key:
        click.echo("ERROR: OPENAI_API_KEY not found in environment or .env file", err=True)
        click.echo(f"Expected .env location: {config.project_root / '.env'}", err=True)
        sys.exit(1)
    
    # Setup directories
    if input:
        permits_dir = Path(input)
    else:
        permits_dir = config.get_permits_dir(state)
    
    if output:
        output_dir = Path(output)
    else:
        output_dir = config.get_extracted_dir(state)
    
    if not permits_dir.exists():
        click.echo(f"ERROR: Permits directory not found: {permits_dir}", err=True)
        sys.exit(1)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load schema
    if not config.default_schema.exists():
        click.echo(f"ERROR: Schema not found: {config.default_schema}", err=True)
        sys.exit(1)
    
    schema = load_schema(config.default_schema)
    
    # Get PDF files
    all_pdfs = sorted(permits_dir.glob("*.pdf"))
    
    if permits:
        # Filter by specific permit numbers
        pdf_files = [
            pdf for pdf in all_pdfs 
            if any(permit_num in pdf.stem for permit_num in permits)
        ]
    elif test:
        pdf_files = all_pdfs[:test]
    else:
        pdf_files = all_pdfs
    
    if not pdf_files:
        click.echo("No PDF files found to process", err=True)
        sys.exit(1)
    
    # Filter already processed files
    if not reprocess:
        original_count = len(pdf_files)
        pdf_files = [
            pdf for pdf in pdf_files
            if not (output_dir / f"langextract-{pdf.stem.replace('_DC_Permit', '').replace('_DC_TV_Permit', '')}.json").exists()
        ]
        skipped = original_count - len(pdf_files)
        if skipped > 0:
            click.echo(f"Skipping {skipped} already processed files (use --reprocess to override)")
        
        if not pdf_files:
            click.echo("✓ All files already processed!")
            return
    
    click.echo(f"Processing {len(pdf_files)} permits from {state}")
    click.echo(f"Model: {model}")
    click.echo(f"Output: {output_dir}\n")
    
    # Initialize extractor
    extractor = PermitExtractor(
        api_key=config.openai_api_key,
        schema=schema,
        model_id=model
    )
    
    # Process files
    successful = 0
    failed = 0
    
    for i, pdf_path in enumerate(pdf_files, 1):
        click.echo(f"\n[{i}/{len(pdf_files)}]")
        
        try:
            result = extractor.extract(pdf_path)
            extractor.save_result(result, pdf_path, output_dir)
            successful += 1
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {e}")
            failed += 1
    
    # Summary
    click.echo(f"\n{'='*80}")
    click.echo("EXTRACTION COMPLETE")
    click.echo(f"{'='*80}")
    click.echo(f"State: {state}")
    click.echo(f"Total: {len(pdf_files)}")
    click.echo(f"Successful: {successful}")
    click.echo(f"Failed: {failed}")
    click.echo(f"Output: {output_dir}")


@cli.command()
@click.option('--input', '-i', required=True, type=click.Path(exists=True),
              help='Input directory containing extracted JSON files')
@click.option('--output', '-o', required=True, type=click.Path(),
              help='Output CSV file path')
@click.option('--state', help='Filter by state name')
def consolidate(input, output, state):
    """Consolidate extracted JSON files into a CSV dataset."""
    input_dir = Path(input)
    output_path = Path(output)
    
    click.echo(f"Consolidating extractions from: {input_dir}")
    if state:
        click.echo(f"Filtering for state: {state}")
    
    # Create consolidator
    consolidator = PermitConsolidator(
        extraction_dir=input_dir,
        state=state
    )
    
    # Consolidate
    df = consolidator.consolidate(output_path=output_path)
    
    if df.empty:
        click.echo("No data to consolidate", err=True)
        sys.exit(1)
    
    # Generate summary
    summary = consolidator.generate_summary(df)
    
    click.echo(f"\n{'='*80}")
    click.echo("CONSOLIDATION SUMMARY")
    click.echo(f"{'='*80}")
    click.echo(f"Total facilities: {summary['total_facilities']}")
    click.echo(f"Total generators: {summary['total_generators']}")
    click.echo(f"Total capacity: {summary['total_capacity_mw']:.2f} MW")
    click.echo(f"Counties: {summary['counties']}")
    click.echo(f"\nFuel type distribution:")
    for fuel, count in summary['fuel_type_distribution'].items():
        click.echo(f"  {fuel}: {count}")
    click.echo(f"\nOutput saved to: {output_path}")


if __name__ == '__main__':
    cli()
