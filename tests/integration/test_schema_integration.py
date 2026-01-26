#!/usr/bin/env python3
"""Integration tests for schema extraction on real documents."""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

from streamline_extract.extraction.document_extractor import DocumentExtractor
from streamline_extract.extraction.pdf_utils import extract_text_from_pdf

# Load environment variables
load_dotenv()

# Set up paths
BASE_DIR = Path(__file__).parent
SCHEMA_PATH = BASE_DIR / "schemas" / "geothermal_ordinance_schema.json"
OUTPUT_DIR = BASE_DIR / "data" / "extracted" / "geothermal_ordinances"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Test PDFs - starting with two clear examples
TEST_PDFS = [
    {
        "path": BASE_DIR / "04_jurisdiction_validated" / "California" / "Imperial" / "title-9-division-17-geothermal-ordinance-10-6-15.pdf",
        "state": "California",
        "county": "Imperial"
    },
    {
        "path": BASE_DIR / "04_jurisdiction_validated" / "Colorado" / "Chaffee" / "53007.pdf",
        "state": "Colorado",
        "county": "Chaffee"
    }
]

def main():
    """Extract geothermal ordinance data from test PDFs."""
    
    # Load schema
    with open(SCHEMA_PATH, 'r') as f:
        schema = json.load(f)
    
    print(f"Loaded schema from: {SCHEMA_PATH}")
    print(f"Output directory: {OUTPUT_DIR}\n")
    
    # Initialize extractor with API key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ Error: OPENAI_API_KEY not found in environment variables")
        print("   Please set it in your .env file")
        return
    
    extractor = DocumentExtractor(
        api_key=api_key,
        model="gpt-4o-mini"
    )
    
    # Process each test PDF
    for i, pdf_info in enumerate(TEST_PDFS, 1):
        pdf_path = pdf_info["path"]
        
        if not pdf_path.exists():
            print(f"❌ PDF not found: {pdf_path}")
            continue
        
        print(f"{'='*80}")
        print(f"Test {i}/{len(TEST_PDFS)}: {pdf_info['state']} - {pdf_info['county']}")
        print(f"PDF: {pdf_path.name}")
        print(f"{'='*80}\n")
        
        try:
            # Extract text from PDF
            print("Extracting text from PDF...")
            pdf_text = extract_text_from_pdf(pdf_path)
            print(f"   Extracted {len(pdf_text):,} characters")
            
            # Extract data using schema
            print("Performing structured extraction...")
            extraction_result = extractor.extract(
                text=pdf_text,
                schema=schema,
                enable_qa_qc=False  # Disable QA/QC for geothermal ordinances
            )
            
            # Get result data
            result = extraction_result.data if hasattr(extraction_result, 'data') else extraction_result
            
            # Save result
            output_file = OUTPUT_DIR / f"{pdf_info['state']}_{pdf_info['county']}.json"
            with open(output_file, 'w') as f:
                json.dump(result, f, indent=2)
            
            print(f"✅ Extraction successful!")
            print(f"   Output: {output_file}")
            
            # Display summary
            if result:
                jurisdiction = result.get("jurisdictionDetails", {})
                requirements = result.get("ordinanceRequirements", [])
                
                print(f"\n📊 Summary:")
                print(f"   State: {jurisdiction.get('state', 'N/A')}")
                print(f"   County: {jurisdiction.get('county', 'N/A')}")
                print(f"   Ordinance: {jurisdiction.get('ordinanceTitle', 'N/A')}")
                print(f"   Date: {jurisdiction.get('ordinanceDate', 'N/A')}")
                print(f"   Requirements extracted: {len(requirements)}")
                
                if requirements:
                    print(f"\n   Feature types found:")
                    features = {}
                    for req in requirements:
                        feature = req.get("feature", "Unknown")
                        features[feature] = features.get(feature, 0) + 1
                    
                    for feature, count in sorted(features.items()):
                        print(f"     • {feature}: {count}")
            
            print()
            
        except Exception as e:
            print(f"❌ Error during extraction: {e}")
            print(f"   Type: {type(e).__name__}")
            import traceback
            traceback.print_exc()
        
        print()
    
    print(f"{'='*80}")
    print("Test complete!")
    print(f"Check output files in: {OUTPUT_DIR}")
    print(f"{'='*80}")

if __name__ == "__main__":
    main()
