#!/usr/bin/env python3
"""
Simple test: Extract validation permits using the standard CLI pipeline.
Results will go to data/extracted/ and data/visualizations/ as normal.
"""

import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.utils.config import get_config


def main():
    """Extract validation permits using new schema."""
    print("="*80)
    print("EXTRACTING VALIDATION PERMITS WITH NEW SCHEMA")
    print("="*80)
    
    config = get_config()
    
    if not config.openai_api_key:
        print("\n❌ ERROR: OPENAI_API_KEY not found")
        sys.exit(1)
    
    schema = load_schema(config.default_schema)
    print(f"\n📋 Using schema: {config.default_schema}")
    
    extractor = PermitExtractor(api_key=config.openai_api_key, model="gpt-4o-mini")
    
    # Validation permits with ground truth
    permits = {
        "11790": config.project_root / "data/permits/Virginia/11790_DC_Permit.pdf",
        "11541": config.project_root / "data/permits/Virginia/11541_DC_Permit.pdf",
        "73757": config.project_root / "data/permits/Virginia/73757_DC_Permit.pdf",
    }
    
    # Create output directories
    extracted_dir = config.project_root / "data/extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📂 Outputs will be saved to: {extracted_dir}/")
    print(f"📊 Compare with ground truth at: data/validation/ground_truth.json\n")
    
    for permit_id, pdf_path in permits.items():
        if not pdf_path.exists():
            print(f"⚠️  Permit {permit_id} not found at {pdf_path}")
            continue
        
        print(f"{'='*80}")
        print(f"Extracting: {permit_id}")
        print(f"{'='*80}")
        
        # Extract text
        print(f"⏳ Reading PDF...")
        text = extract_text_from_pdf(pdf_path)
        print(f"  Extracted {len(text):,} characters")
        
        # Run extraction
        print(f"🔍 Extracting data...")
        result = extractor.extract(text, schema, enable_qa_qc=False)
        
        # Save to standard location
        output_file = extracted_dir / f"{permit_id}_extracted.json"
        with open(output_file, 'w') as f:
            json.dump(result.data, f, indent=2)
        
        # Print summary
        generators = result.data.get('generatorSets', [])
        permit_details = result.data.get('permitDetails', {})
        
        print(f"✅ Extraction completed:")
        print(f"  Permit: {permit_details.get('permitNumber')}")
        print(f"  Facility: {permit_details.get('facilityName')}")
        print(f"  Generators: {len(generators)} entries")
        print(f"  Total Units: {sum(g.get('numGenerators') or 1 for g in generators)}")
        print(f"  Cost: ${result.cost:.4f}")
        print(f"  Saved to: {output_file}")
        print()
    
    print("="*80)
    print("✅ ALL EXTRACTIONS COMPLETE")
    print("="*80)
    print(f"\n📂 Extracted files are in: {extracted_dir}/")
    print(f"📊 Ground truth is in: data/validation/ground_truth.json")
    print(f"\nCompare the extracted JSONs with ground truth to validate!")


if __name__ == "__main__":
    main()
