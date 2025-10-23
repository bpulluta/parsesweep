"""
Example: Basic usage of the permit toolkit
"""

from pathlib import Path
from permit_toolkit.scrapers.virginia import VirginiaScraper
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator
from permit_toolkit.utils import get_config

def main():
    # Initialize configuration
    config = get_config()
    config.setup_directories()
    
    print("="*80)
    print("PERMIT TOOLKIT - BASIC EXAMPLE")
    print("="*80)
    
    # Step 1: Scrape permits (Virginia example)
    print("\n1. SCRAPING PERMITS")
    print("-" * 80)
    
    scraper = VirginiaScraper(
        output_dir=config.get_permits_dir("Virginia"),
        test_mode=True,
        test_count=3,
        latest_only=True
    )
    # scraper.run()  # Uncomment to actually run scraper
    print("Scraper configured (run() commented out for demo)")
    
    # Step 2: Extract data
    print("\n2. EXTRACTING DATA")
    print("-" * 80)
    
    if not config.openai_api_key:
        print("⚠️  No OpenAI API key found. Set OPENAI_API_KEY in .env file")
        print("   Skipping extraction step...")
    else:
        schema = load_schema(config.default_schema)
        extractor = PermitExtractor(
            api_key=config.openai_api_key,
            schema=schema,
            model_id="gpt-4o"
        )
        
        permits_dir = config.get_permits_dir("Virginia")
        output_dir = config.get_extracted_dir("Virginia")
        
        if permits_dir.exists():
            pdf_files = list(permits_dir.glob("*.pdf"))[:3]  # First 3 for demo
            
            if pdf_files:
                print(f"Found {len(pdf_files)} PDF files")
                for pdf_path in pdf_files:
                    print(f"Processing: {pdf_path.name}")
                    # result = extractor.extract(pdf_path)  # Uncomment to extract
                    # extractor.save_result(result, pdf_path, output_dir)
                print("Extraction configured (commented out for demo)")
            else:
                print("No PDF files found in permits directory")
        else:
            print(f"Permits directory not found: {permits_dir}")
    
    # Step 3: Consolidate data
    print("\n3. CONSOLIDATING DATA")
    print("-" * 80)
    
    extracted_dir = config.get_extracted_dir("Virginia")
    
    if extracted_dir.exists() and list(extracted_dir.glob("*.json")):
        consolidator = PermitConsolidator(
            extraction_dir=extracted_dir,
            state="Virginia"
        )
        
        output_path = config.outputs_dir / "virginia_generators.csv"
        df = consolidator.consolidate(output_path=output_path)
        
        if not df.empty:
            summary = consolidator.generate_summary(df)
            
            print(f"\n✓ Consolidated {len(df)} generator records")
            print(f"  Facilities: {summary['total_facilities']}")
            print(f"  Total Capacity: {summary['total_capacity_mw']:.2f} MW")
            print(f"  Output: {output_path}")
    else:
        print(f"No extracted JSON files found in: {extracted_dir}")
        print("Run extraction first to generate data")
    
    print("\n" + "="*80)
    print("EXAMPLE COMPLETE")
    print("="*80)
    print("\nTo run the full pipeline:")
    print("1. Set OPENAI_API_KEY in .env file")
    print("2. Uncomment the scraper.run() and extraction lines")
    print("3. Run this script: python examples/basic_usage.py")


if __name__ == "__main__":
    main()
