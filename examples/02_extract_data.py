"""
Example: Extracting data from permit PDFs

This example demonstrates how to use the LLM-based extractor
to extract structured data from air quality permits.
"""

import os
from pathlib import Path
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.utils import get_config

def main():
    # Load configuration
    config = get_config()
    
    # Check for API key
    if not config.openai_api_key:
        print("ERROR: OPENAI_API_KEY not found!")
        print("Please create a .env file with: OPENAI_API_KEY=your_key_here")
        return
    
    # Load schema
    schema_path = Path("schemas/air_quality_permits_schema.json")
    schema = load_schema(schema_path)
    
    # Create extractor
    extractor = PermitExtractor(
        api_key=config.openai_api_key,
        schema=schema,
        model_id="gpt-4o"
    )
    
    # Get permits directory
    permits_dir = Path("data/permits/Virginia")
    output_dir = Path("data/extracted/Virginia")
    
    if not permits_dir.exists():
        print(f"ERROR: Permits directory not found: {permits_dir}")
        print("Please run the scraping example first!")
        return
    
    # Get all PDFs
    pdf_files = sorted(permits_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found in {permits_dir}")
        return
    
    # Process first 5 for demonstration
    pdf_files = pdf_files[:5]
    
    print(f"Extracting data from {len(pdf_files)} permits...")
    print(f"Model: gpt-4o")
    print(f"Output: {output_dir}\n")
    
    # Process each PDF
    for i, pdf_path in enumerate(pdf_files, 1):
        print(f"\n[{i}/{len(pdf_files)}]")
        
        try:
            # Extract data
            result = extractor.extract(pdf_path)
            
            # Save result
            extractor.save_result(result, pdf_path, output_dir)
            
        except Exception as e:
            print(f"✗ Error: {e}")
    
    print("\n✓ Extraction complete!")
    print(f"Check {output_dir} for extracted JSON files")


if __name__ == "__main__":
    main()
