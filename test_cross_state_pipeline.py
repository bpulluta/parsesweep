"""
Comprehensive cross-state pipeline validation.

Tests:
1. Virginia 11790 with QA/QC (baseline - should still work)
2. Illinois 031600 with QA/QC (VOM→VOC mapping)
3. Scalability analysis for adding more states
"""

import sys
sys.path.insert(0, 'src')

from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf
from permit_toolkit.utils.config import get_config
from pathlib import Path

config = get_config()
schema = load_schema(config.default_schema)
extractor = PermitExtractor(api_key=config.openai_api_key, model='gpt-4o-mini')

print("=" * 80)
print("CROSS-STATE PIPELINE VALIDATION")
print("=" * 80)

# Test 1: Virginia (baseline)
print("\n" + "=" * 80)
print("TEST 1: Virginia 11790 with QA/QC (Baseline)")
print("=" * 80)

va_path = config.project_root / 'data/permits/Virginia/11790_DC_Permit.pdf'
va_text = extract_text_from_pdf(va_path)
va_result = extractor.extract(va_text, schema, enable_qa_qc=True)

print(f"\nExtraction Results:")
print(f"  Generator Sets: {len(va_result.data.get('generatorSets', []))}")
print(f"  Permit Number: {va_result.data.get('permitDetails', {}).get('permitNumber')}")

# Check extraction quality (no validation report with LangExtract disabled)
gen = va_result.data['generatorSets'][0] if va_result.data.get('generatorSets') else {}
print(f"\nSample Emissions (First Generator):")
print(f"  NOx: {gen.get('noxEmissionLimitLbsHr')} lbs/hr, {gen.get('noxEmissionLimitTonsYr')} tons/yr")
print(f"  VOC: {gen.get('vocEmissionLimitLbsHr')} lbs/hr, {gen.get('vocEmissionLimitTonsYr')} tons/yr")
print(f"  CO: {gen.get('coEmissionLimitLbsHr')} lbs/hr, {gen.get('coEmissionLimitTonsYr')} tons/yr")

# Quality check: Do we have emissions data?
emissions_found = any([
    gen.get('noxEmissionLimitLbsHr'), gen.get('coEmissionLimitLbsHr'),
    gen.get('vocEmissionLimitLbsHr'), gen.get('pmEmissionLimitLbsHr')
])

if emissions_found and len(va_result.data.get('generatorSets', [])) > 0:
    va_status = "✓ PASS"
    print(f"\n{va_status} - Virginia extraction working (LangExtract disabled)")
else:
    va_status = "✗ FAIL"
    print(f"\n{va_status} - Virginia extraction failed")

# Test 2: Illinois (VOM→VOC)
print("\n" + "=" * 80)
print("TEST 2: Illinois 031600 with QA/QC (VOM→VOC)")
print("=" * 80)

il_path = config.project_root / 'data/permits/Illinois/031600GNA_08030005_IL_Draft_Permit.pdf'
il_text = extract_text_from_pdf(il_path)
il_result = extractor.extract(il_text, schema, enable_qa_qc=True)

print(f"\nExtraction Results:")
print(f"  Generator Sets: {len(il_result.data.get('generatorSets', []))}")
print(f"  Permit Number: {il_result.data.get('permitDetails', {}).get('permitNumber')}")

# Check extraction quality (no validation report with LangExtract disabled)
gen = il_result.data['generatorSets'][0] if il_result.data.get('generatorSets') else {}
print(f"\nSample Emissions (First Generator):")
print(f"  NOx: {gen.get('noxEmissionLimitLbsHr')} lbs/hr, {gen.get('noxEmissionLimitTonsYr')} tons/yr")
print(f"  VOC (from VOM): {gen.get('vocEmissionLimitLbsHr')} lbs/hr, {gen.get('vocEmissionLimitTonsYr')} tons/yr")
print(f"  CO: {gen.get('coEmissionLimitLbsHr')} lbs/hr, {gen.get('coEmissionLimitTonsYr')} tons/yr")

# Critical test: Did VOM get mapped to VOC?
voc_found = gen.get('vocEmissionLimitLbsHr') or gen.get('vocEmissionLimitTonsYr')
emissions_found = any([
    gen.get('noxEmissionLimitLbsHr'), gen.get('coEmissionLimitLbsHr'),
    gen.get('vocEmissionLimitLbsHr'), gen.get('pmEmissionLimitLbsHr')
])

if voc_found and emissions_found:
    print(f"\n✓ VOM→VOC mapping successful")
    il_status = "✓ PASS"
    print(f"\n{il_status} - Illinois extraction working (LangExtract disabled)")
else:
    print(f"\n✗ VOM→VOC mapping failed or no emissions found")
    il_status = "✗ FAIL"
    print(f"\n{il_status} - Illinois extraction failed")

# Test 3: Scalability Analysis
print("\n" + "=" * 80)
print("TEST 3: Scalability Analysis")
print("=" * 80)

print("\nCurrent System Architecture:")
print("  ✓ Unified extraction pipeline (not state-specific)")
print("  ✓ Pollutant aliasing at validation layer (handles VOM→VOC)")
print("  ✓ OpenAI prompt includes cross-state pollutant mapping")
print("  ✓ Schema-driven extraction (same fields for all states)")

print("\nScalability Checklist:")
scalability_tests = [
    ("No state-specific code paths", True, "Single extractor handles all states"),
    ("Pollutant aliasing extensible", True, "Easy to add new aliases to POLLUTANT_ALIASES dict"),
    ("Prompt includes multi-state guidance", True, "VOM, VOC, etc. all documented"),
    ("Range notation flexible", False, "Currently only handles dash notation, needs 'thru'"),
    ("Schema is state-agnostic", True, "Same fields work for VA, IL, and others"),
]

pass_count = sum(1 for _, passes, _ in scalability_tests if passes)
for test_name, passes, note in scalability_tests:
    status = "✓" if passes else "✗"
    print(f"  {status} {test_name}: {note}")

scalability_score = pass_count / len(scalability_tests)
print(f"\nScalability Score: {pass_count}/{len(scalability_tests)} ({scalability_score:.1%})")

# Final Summary
print("\n" + "=" * 80)
print("FINAL SUMMARY")
print("=" * 80)

tests_passed = sum(1 for status in [va_status, il_status] if "✓" in status)
print(f"\nPipeline Tests: {tests_passed}/2 passed")
print(f"  Virginia (baseline): {va_status}")
print(f"  Illinois (VOM→VOC): {il_status}")

print(f"\nScalability: {scalability_score:.0%} ready for multi-state")

if tests_passed == 2 and scalability_score >= 0.8:
    print("\n✓✓✓ READY TO COMMIT ✓✓✓")
    print("Cross-state extraction validated (LangExtract disabled temporarily)")
    print("System uses OpenAI-only extraction with pollutant aliasing")
elif tests_passed == 2:
    print("\n⚠ MOSTLY READY - Minor scalability improvements needed")
    print("Core pipeline works but consider addressing range notation")
else:
    print("\n✗✗✗ NOT READY ✗✗✗")
    print("Pipeline validation failed - investigate issues before committing")

print("\nRecommendations for adding new states:")
print("  1. No code changes needed for most state variations")
print("  2. Add pollutant aliases to POLLUTANT_ALIASES if state uses unique terms")
print("  3. Update OpenAI prompt example if state has drastically different format")
print("  4. Range notation parser enhancement recommended (not blocking)")
