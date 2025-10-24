"""
Batch Extraction with Quality Assurance Visualizations

Production script for processing multiple permit PDFs with the hybrid extraction approach.
Generates both structured JSON outputs and interactive HTML visualizations for QA review.

Features:
- Hybrid extraction (OpenAI + LangExtract)
- Range notation expansion (EG01-EG06 → 6 units)
- Example contamination filtering
- Interactive HTML visualizations showing entities in context

Usage:
    python batch_extract_with_visualization.py

Outputs:
    - JSON: data/extracted/{state}/
    - HTML: data/comparison/{state}/visualizations/
"""

import sys
from pathlib import Path

sys.path.insert(0, 'src')

from permit_toolkit.extraction import ExtractorFactory, load_schema
from permit_toolkit.utils import get_config

# Define permits to process with expected generator counts (for validation)
# Format: (filename, expected_count)
VIRGINIA_PERMITS = [
    ('11790_DC_Permit.pdf', 7),  # 7 generators (EG01-EG06 + EG07)
    ('11541_DC_Permit.pdf', 3),  # 3 generators 
    ('52173_DC_Permit.pdf', 3),  # 3 generators (G-1, G-2, G-3) - image-based PDF
]

# Setup
config = get_config()
schema = load_schema(Path('schemas/air_quality_permits_schema.json'))

# Output directories
OUTPUT_JSON_DIR = Path('data/extracted/Virginia')
OUTPUT_VISUALIZATION_DIR = Path('data/comparison/Virginia/visualizations')
OUTPUT_JSON_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_VISUALIZATION_DIR.mkdir(parents=True, exist_ok=True)

print("="*80)
print("HYBRID EXTRACTION WITH VISUALIZATIONS")
print("="*80)
print()

extraction_results = []

for pdf_name, expected_generator_count in VIRGINIA_PERMITS:
    print(f"\n{'='*80}")
    print(f"Processing: {pdf_name}")
    print(f"Expected generators: {expected_generator_count}")
    print('='*80)
    
    pdf_path = Path(f'data/permits/Virginia/{pdf_name}')
    
    if not pdf_path.exists():
        print(f"❌ PDF not found: {pdf_path}")
        continue
    
    # Create extractor
    extractor = ExtractorFactory.create_extractor(
        pdf_path=pdf_path,
        api_key=config.openai_api_key,
        schema=schema,
        model_id='gpt-4o-mini',
        enable_text_optimization=False,
    )
    
    # Extract data
    json_output_path = OUTPUT_JSON_DIR / f"{Path(pdf_name).stem}.json"
    extraction_result = extractor.extract_from_pdf(
        pdf_path, 
        output_json_path=json_output_path
    )
    
    actual_generator_count = len(extraction_result['generatorSets'])
    validation_passed = (actual_generator_count == expected_generator_count) if expected_generator_count is not None else None
    
    print("\n📊 Results:")
    print(f"   Generators extracted: {actual_generator_count}")
    
    # Warning if no generators found
    if actual_generator_count == 0:
        print("   ⚠️  WARNING: No generators found!")
    
    if expected_generator_count is not None:
        print(f"   Expected: {expected_generator_count}")
        print(f"   Status: {'✅ PASS' if validation_passed else '❌ FAIL'}")
    else:
        print("   Status: ℹ️  No validation (expected count unknown)")
    
    # Show generator details
    if extraction_result['generatorSets']:
        print("\n   Generator Details:")
        for i, gen in enumerate(extraction_result['generatorSets'], 1):
            ref = gen.get('referenceNumber', 'N/A')
            make = gen.get('make') or 'N/A'
            model = gen.get('model') or 'N/A'
            kw = gen.get('ratedCapacityKW')
            print(f"     {i}. Ref: {ref:8s} | {str(make):12s} {str(model):15s} | {kw} kW")
    
    # Generate visualization
    try:
        html_path = extractor.generate_visualization(
            output_dir=OUTPUT_VISUALIZATION_DIR,
            pdf_name=pdf_name
        )
        print(f"\n🎨 Visualization: {html_path}")
    except Exception as e:
        print(f"\n⚠️  Visualization failed: {e}")
    
    extraction_results.append({
        'pdf': pdf_name,
        'expected': expected_generator_count,
        'actual': actual_generator_count,
        'passed': validation_passed,
        'json_path': json_output_path,
    })

# Summary
print(f"\n\n{'='*80}")
print("SUMMARY")
print('='*80)

for result in extraction_results:
    if result['expected'] is not None:
        status = '✅ PASS' if result['passed'] else '❌ FAIL'
        print(f"{result['pdf']:30s} | Expected: {result['expected']} | Got: {result['actual']} | {status}")
    else:
        print(f"{result['pdf']:30s} | Got: {result['actual']} generators")

validated_results = [r for r in extraction_results if r['expected'] is not None]
if validated_results:
    all_passed = all(r['passed'] for r in validated_results)
    print('='*80)
    print(f"Overall: {'✅ ALL TESTS PASSED' if all_passed else '❌ SOME TESTS FAILED'}")
else:
    print('='*80)
    print("Overall: ℹ️  No validation performed")

print()
print(f"📁 JSON outputs: {OUTPUT_JSON_DIR.absolute()}")
print(f"🎨 Visualizations: {OUTPUT_VISUALIZATION_DIR.absolute()}")
print()
print("💡 Tip: Open the HTML files in a browser to review extracted entities in context!")
print(f"   Example: open {OUTPUT_VISUALIZATION_DIR.absolute()}/11790_DC_Permit_visualization.html")
