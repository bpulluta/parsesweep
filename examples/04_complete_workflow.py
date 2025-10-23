"""
Example: Complete end-to-end workflow

This example demonstrates the complete workflow:
1. Scrape permits
2. Extract data
3. Consolidate to CSV

This is a simplified version showing the core concepts.
For production use, add error handling, logging, and progress tracking.
"""

from pathlib import Path
from permit_toolkit.scrapers.virginia import VirginiaScraper
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator
from permit_toolkit.utils import get_config

def main():
    # Load configuration
    config = get_config()
    
    # Check API key
    if not config.openai_api_key:
        print("ERROR: OPENAI_API_KEY not found in environment or .env file")
        return
    
    # Define paths
    state = "Virginia"
    permits_dir = Path(f"data/permits/{state}")
    extracted_dir = Path(f"data/extracted/{state}")
    output_csv = Path(f"data/outputs/{state.lower()}_generators.csv")
    
    print("="*80)
    print("AIR QUALITY PERMIT TOOLKIT - END-TO-END WORKFLOW")
    print("="*80)
    print(f"State: {state}")
    print(f"Mode: Test (5 permits)")
    print("="*80)
    
    # =========================================================================
    # STEP 1: SCRAPE PERMITS
    # =========================================================================
    print("\n[STEP 1/3] SCRAPING PERMITS")
    print("-"*80)
    
    scraper = VirginiaScraper(
        output_dir=permits_dir,
        test_mode=True,
        test_count=5,
        latest_only=True,
        resume=True
    )
    
    scraper.run()
    
    # =========================================================================
    # STEP 2: EXTRACT DATA
    # =========================================================================
    print("\n[STEP 2/3] EXTRACTING DATA")
    print("-"*80)
    
    # Load schema
    schema = load_schema(Path("schemas/air_quality_permits_schema.json"))
    
    # Create extractor
    extractor = PermitExtractor(
        api_key=config.openai_api_key,
        schema=schema,
        model_id="gpt-4o"
    )
    
    # Get PDFs
    pdf_files = sorted(permits_dir.glob("*.pdf"))[:5]
    
    print(f"Processing {len(pdf_files)} permits...")
    
    for i, pdf_path in enumerate(pdf_files, 1):
        print(f"\n[{i}/{len(pdf_files)}]")
        try:
            result = extractor.extract(pdf_path)
            extractor.save_result(result, pdf_path, extracted_dir)
        except Exception as e:
            print(f"✗ Error: {e}")
    
    # =========================================================================
    # STEP 3: CONSOLIDATE DATA
    # =========================================================================
    print("\n[STEP 3/3] CONSOLIDATING DATA")
    print("-"*80)
    
    consolidator = PermitConsolidator(
        extraction_dir=extracted_dir,
        state=state
    )
    
    df = consolidator.consolidate(output_path=output_csv)
    
    if not df.empty:
        summary = consolidator.generate_summary(df)
        
        print(f"\n{'='*80}")
        print("WORKFLOW COMPLETE!")
        print(f"{'='*80}")
        print(f"✓ Scraped: {len(pdf_files)} permits")
        print(f"✓ Extracted: {summary['total_facilities']} facilities")
        print(f"✓ Total generators: {summary['total_generators']}")
        print(f"✓ Total capacity: {summary['total_capacity_mw']:.2f} MW")
        print(f"✓ Output: {output_csv}")
        print(f"{'='*80}")
    else:
        print("No data extracted")


if __name__ == "__main__":
    main()
