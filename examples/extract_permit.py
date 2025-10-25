"""
Production example: Extract data from air quality permit

This example demonstrates how to use the production permit extractor
to extract structured data from PDF permits with full QA/QC traceability.
"""

import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.permit_toolkit.extraction.permit_extractor import PermitExtractor
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.utils.config import Config


def main():
    """Extract data from a sample permit"""
    
    # Load configuration
    config = Config()
    if not config.openai_api_key:
        print("❌ Error: OPENAI_API_KEY not found in environment")
        print("   Create a .env file with: OPENAI_API_KEY=your-key-here")
        return
    
    # Load schema
    schema_path = Path(__file__).parent.parent / "schemas" / "air_quality_permits_schema.json"
    with open(schema_path) as f:
        schema = json.load(f)
    
    # Example permit (update this path to your permit)
    permit_path = Path(__file__).parent.parent / "data" / "permits" / "11790.pdf"
    
    if not permit_path.exists():
        print(f"❌ Permit not found: {permit_path}")
        print("\n   Update the permit_path variable to point to your PDF file")
        return
    
    print(f"📄 Extracting data from: {permit_path.name}\n")
    
    # Extract text from PDF
    print("⏳ Extracting text from PDF...")
    text = extract_text_from_pdf(str(permit_path))
    print(f"   Extracted {len(text):,} characters\n")
    
    # Initialize extractor
    extractor = PermitExtractor(config.openai_api_key)
    
    # Extract with QA/QC enabled
    print("⏳ Extracting permit data with QA/QC validation...")
    result = extractor.extract(text, schema, enable_qa_qc=True)
    
    # Print results
    print("\n✅ Extraction complete!\n")
    print(f"📊 Results:")
    print(f"   Confidence: {result.confidence:.1%}")
    print(f"   Cost: ${result.cost:.4f}")
    print(f"   Processing time: {result.processing_time:.1f}s")
    
    # Show extracted data summary
    data = result.data
    if data.get('permitDetails'):
        permit = data['permitDetails']
        print(f"\n📋 Permit Details:")
        print(f"   Number: {permit.get('permitNumber')}")
        print(f"   Facility: {permit.get('facilityName')}")
        print(f"   Issue Date: {permit.get('issueDate')}")
    
    if data.get('generatorSets'):
        print(f"\n🏭 Generator Sets: {len(data['generatorSets'])}")
        for i, gen_set in enumerate(data['generatorSets'], 1):
            print(f"   {i}. {gen_set.get('numGenerators', 0)} units × {gen_set.get('make')} {gen_set.get('model')}")
            if gen_set.get('emissions'):
                print(f"      Emissions tracked: {', '.join(gen_set['emissions'].keys())}")
    
    # Print validation notes
    if result.validation_notes:
        print(f"\n📝 QA/QC Validation:")
        for note in result.validation_notes:
            print(f"   • {note}")
    
    # Save clean JSON output
    output_dir = Path(__file__).parent.parent / "data" / "extracted"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / f"{permit_path.stem}_extracted.json"
    with open(output_file, 'w') as f:
        json.dump({
            'data': result.data,
            'metadata': {
                'source_file': permit_path.name,
                'confidence': result.confidence,
                'cost': result.cost,
                'processing_time': result.processing_time,
                'validation_notes': result.validation_notes
            }
        }, f, indent=2)
    
    print(f"\n💾 Saved clean JSON: {output_file}")
    print(f"   Size: {output_file.stat().st_size / 1024:.1f} KB")
    
    # Optionally generate visualization for QA/QC review
    if result.langextract_result:
        viz_dir = Path(__file__).parent.parent / "data" / "visualizations"
        viz_path = extractor.generate_visualization(
            result,
            output_dir=viz_dir,
            permit_number=permit_path.stem
        )
        print(f"\n📊 Saved visualization: {viz_path}")
        print(f"   Size: {viz_path.stat().st_size / 1024:.1f} KB")
        print(f"   (Open with LangExtract viewer for interactive source highlighting)")
    
    print("\n✅ Complete!")


if __name__ == "__main__":
    main()
