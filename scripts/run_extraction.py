"""
Test the production permit extractor on validation permits
"""
import json
import sys
import warnings
from pathlib import Path

# Suppress LangExtract deprecation warnings for cleaner output
warnings.filterwarnings('ignore', category=FutureWarning, module='langextract')

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.permit_toolkit.extraction.permit_extractor import PermitExtractor
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.utils.config import Config

# Load schema
with open('schemas/air_quality_permits_schema.json') as f:
    schema = json.load(f)

config = Config()


def test_permit(permit_path: Path, extractor: PermitExtractor):
    """Test extraction on one permit."""
    print(f"\n{'='*80}")
    print(f"TESTING: {permit_path.name}")
    print(f"{'='*80}\n")
    
    # Extract text
    text = extract_text_from_pdf(permit_path)
    print(f"📄 Text length: {len(text):,} characters\n")
    
    # Run extraction with LangExtract QA/QC for traceability
    result = extractor.extract(text, schema, enable_qa_qc=True)
    
    # Display results
    print(f"✅ Extraction complete:")
    print(f"   Confidence: {result.confidence:.2f}")
    print(f"   Cost: ${result.cost:.4f}")
    print(f"   Time: {result.processing_time:.1f}s")
    
    # Show generator sets
    generators = result.data.get('generatorSets', [])
    print(f"\n⚙️  Generator Sets: {len(generators)}")
    for i, gen in enumerate(generators, 1):
        ref = gen.get('referenceNumber', 'Unknown')
        num = gen.get('numGenerators', 1)
        make = gen.get('make', 'Unknown')
        model = gen.get('model', 'Unknown')
        kw = gen.get('ratedCapacityKW', 0)
        nox = gen.get('noxEmissionLimitLbsHr', 'N/A')
        print(f"   {i}. {ref} ({num} units): {make} {model}, {kw}kW, NOx: {nox} lbs/hr")
    
    # Validation notes
    if result.validation_notes:
        print(f"\n📝 QA/QC Notes:")
        for note in result.validation_notes:
            print(f"   - {note}")
    
    # Save clean JSON (data only, no verbose citations)
    permit_number = permit_path.stem.split('_')[0]
    output_path = Path('data/extracted') / f"{permit_number}_simplified.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        json.dump({
            'data': result.data,
            'metadata': {
                'confidence': result.confidence,
                'cost': result.cost,
                'processing_time': result.processing_time,
                'validation_notes': result.validation_notes
            }
        }, f, indent=2)
    
    print(f"\n💾 Saved clean JSON: {output_path}")
    
    # Generate interactive visualization (if LangExtract was used)
    if result.langextract_result:
        viz_dir = Path('data/visualizations')
        viz_path = extractor.generate_visualization(result, viz_dir, permit_number)
        if viz_path:
            print(f"📊 Saved QA/QC visualization: {viz_path}")
            print(f"   (Open with LangExtract viewer for interactive source highlighting)")
    
    return result


def main():
    print("\n" + "="*80)
    print("AIR QUALITY PERMIT EXTRACTOR - Production Test")
    print("="*80)
    print("\nArchitecture:")
    print("  Stage 1: OpenAI (gpt-4o-mini) - Structured data extraction")
    print("  Stage 2: LangExtract - QA/QC validation + source citations")
    print("="*80)
    
    extractor = PermitExtractor(config.openai_api_key)
    
    permits = [
        'data/permits/Virginia/11790_DC_Permit.pdf',
        'data/permits/Virginia/11541_DC_Permit.pdf',
        'data/permits/Virginia/73757_DC_Permit.pdf'
    ]
    
    results = []
    for permit_path in permits:
        result = test_permit(Path(permit_path), extractor)
        results.append(result)
    
    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    
    avg_confidence = sum(r.confidence for r in results) / len(results)
    total_cost = sum(r.cost for r in results)
    avg_time = sum(r.processing_time for r in results) / len(results)
    
    print(f"\n✅ Permits Processed: {len(results)}")
    print(f"🎯 Average Confidence: {avg_confidence:.1%}")
    print(f"💰 Total Cost: ${total_cost:.4f} (avg ${total_cost/len(results):.4f} per permit)")
    print(f"⏱️  Average Time: {avg_time:.1f}s per permit")
    print(f"\n📊 Outputs:")
    print(f"   • Clean JSON: data/extracted/{{permit}}_simplified.json")
    print(f"   • Visualization: data/visualizations/{{permit}}_visualization.html")
    
    print("\n✅ Production extraction test complete!")
    print("="*80 + "\n")


if __name__ == '__main__':
    main()
