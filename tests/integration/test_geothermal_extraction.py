#!/usr/bin/env python3
"""
Test script to extract geothermal ordinance data from PDFs using the new schema.
"""
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamline_extract.extraction import DocumentExtractor, load_schema
from streamline_extract.extraction.pdf_utils import extract_text_from_pdf

def main():
    load_dotenv()
    
    # Paths
    schema_path = Path("schemas/geothermal_ordinance_schema.json")
    pdf_path = Path("04_jurisdiction_validated/California/Imperial/title-9-division-17-geothermal-ordinance-10-6-15.pdf")
    output_dir = Path("data/extracted/geothermal_ordinances")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load schema
    print(f"Loading schema from {schema_path}")
    schema = load_schema(schema_path)
    
    # Extract text from PDF
    print(f"\nExtracting text from {pdf_path.name}")
    pdf_text = extract_text_from_pdf(pdf_path)
    print(f"Extracted {len(pdf_text)} characters")
    
    # Initialize extractor
    print("\nInitializing extractor...")
    extractor = DocumentExtractor(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-mini"
    )
    
    # Perform extraction
    print("\nPerforming extraction...")
    extraction_result = extractor.extract(pdf_text, schema, enable_qa_qc=False)
    
    # Get the data from the result
    result = extraction_result.data if hasattr(extraction_result, 'data') else extraction_result
    
    # Save result
    output_file = output_dir / f"{pdf_path.stem}.json"
    print(f"\nSaving result to {output_file}")
    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2)
    
    # Display summary
    print("\n" + "="*80)
    print("EXTRACTION SUMMARY")
    print("="*80)
    
    if "jurisdictionDetails" in result:
        print("\nJurisdiction:")
        for key, value in result["jurisdictionDetails"].items():
            if value:
                print(f"  {key}: {value}")
    
    if "ordinanceRequirements" in result:
        print(f"\nExtracted {len(result['ordinanceRequirements'])} requirements:")
        for req in result["ordinanceRequirements"]:
            feature = req.get("feature", "Unknown")
            value = req.get("value", "N/A")
            unit = req.get("unit", "")
            print(f"  • {feature}: {value} {unit}")
    
    print("\n" + "="*80)
    print(f"Full results saved to: {output_file}")
    print("="*80)

if __name__ == "__main__":
    main()
