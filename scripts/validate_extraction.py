"""
Validate production extractor results against ground truth
"""
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple


def load_json(filepath: Path) -> Dict:
    """Load JSON file"""
    with open(filepath) as f:
        return json.load(f)


def compare_permit_details(extracted: Dict, truth: Dict) -> Tuple[int, int, List[str]]:
    """Compare permit details, return (matches, total, differences)"""
    matches = 0
    total = 0
    diffs = []
    
    fields_to_check = [
        ('permitNumber', 'permitNumber'),
        ('issueDate', 'permitIssuanceDate'),
        ('facilityName', 'facilityName'),
        ('county', 'facilityCounty'),
    ]
    
    for extracted_field, truth_field in fields_to_check:
        total += 1
        extracted_val = str(extracted.get(extracted_field, '')).lower().strip()
        truth_val = str(truth.get(truth_field, '')).lower().strip()
        
        # Normalize date formats
        if 'date' in extracted_field.lower() or 'date' in truth_field.lower():
            if extracted_val and ' ' in extracted_val:
                extracted_val = extracted_val.split()[0]  # Take just date part
            if truth_val and ' ' in truth_val:
                truth_val = truth_val.split()[0]
        
        if extracted_val == truth_val or extracted_val in truth_val or truth_val in extracted_val:
            matches += 1
        else:
            diffs.append(f"  {extracted_field}: '{extracted.get(extracted_field)}' vs '{truth.get(truth_field)}'")
    
    return matches, total, diffs


def compare_generator_sets(extracted: List[Dict], truth: List[Dict]) -> Tuple[int, int, List[str]]:
    """Compare generator sets by matching reference numbers"""
    matches = 0
    total = 0
    diffs = []
    
    if len(extracted) != len(truth):
        diffs.append(f"  Generator set count: {len(extracted)} vs {len(truth)}")
    
    # Create lookup by reference number for truth
    truth_by_ref = {g.get('referenceNumber'): g for g in truth}
    
    # Match extracted generators to truth by reference number
    for ext_gen in extracted:
        ext_ref = ext_gen.get('referenceNumber', '')
        gen_id = ext_ref or 'Unknown'
        
        # Find matching truth generator
        truth_gen = truth_by_ref.get(ext_ref)
        if not truth_gen:
            diffs.append(f"  {gen_id}: No matching reference in ground truth")
            continue
        
        # Check key fields
        fields = [
            ('numGenerators', 'numGenerators'),
            ('make', 'make'),
            ('model', 'model'),
            ('ratedCapacityKW', 'ratedCapacityKW'),
        ]
        
        for ext_field, truth_field in fields:
            total += 1
            ext_val = ext_gen.get(ext_field)
            truth_val = truth_gen.get(truth_field)
            
            # Normalize for comparison
            if ext_val is not None and truth_val is not None:
                ext_str = str(ext_val).lower().strip()
                truth_str = str(truth_val).lower().strip()
                
                # Treat "N/A" and "none" as equivalent to null
                if ext_str in ['n/a', 'none', 'null'] and truth_str in ['n/a', 'none', 'null']:
                    matches += 1
                elif ext_str == truth_str or ext_str in truth_str or truth_str in ext_str:
                    matches += 1
                else:
                    diffs.append(f"  {gen_id} {ext_field}: '{ext_val}' vs '{truth_val}'")
            elif (ext_val is None or str(ext_val).lower() in ['n/a', 'none']) and \
                 (truth_val is None or str(truth_val).lower() in ['n/a', 'none']):
                matches += 1
            else:
                diffs.append(f"  {gen_id} {ext_field}: '{ext_val}' vs '{truth_val}'")
        
        # Check emissions (flat structure)
        emission_fields = [
            ('noxEmissionLimitLbsHr', 'noxEmissionLimitLbsHr'),
            ('coEmissionLimitLbsHr', 'coEmissionLimitLbsHr'),
            ('vocEmissionLimitLbsHr', 'vocEmissionLimitLbsHr'),
        ]
        
        for ext_field, truth_field in emission_fields:
            total += 1
            ext_val = ext_gen.get(ext_field)
            truth_val = truth_gen.get(truth_field)
            
            if ext_val is not None and truth_val is not None:
                # Allow small floating point differences
                try:
                    ext_float = float(ext_val)
                    truth_float = float(truth_val)
                    if abs(ext_float - truth_float) < 0.1:
                        matches += 1
                    else:
                        diffs.append(f"  {gen_id} {ext_field}: {ext_val} vs {truth_val}")
                except ValueError:
                    if str(ext_val) == str(truth_val):
                        matches += 1
                    else:
                        diffs.append(f"  {gen_id} {ext_field}: {ext_val} vs {truth_val}")
            elif ext_val is None and truth_val is None:
                matches += 1
            else:
                diffs.append(f"  {gen_id} {ext_field}: {ext_val} vs {truth_val}")
    
    return matches, total, diffs


def validate_permit(permit_number: str, extracted_file: Path, ground_truth: Dict) -> Dict[str, Any]:
    """Validate a single permit"""
    print(f"\n{'='*80}")
    print(f"VALIDATING PERMIT {permit_number}")
    print(f"{'='*80}")
    
    # Load extracted data
    extracted_data = load_json(extracted_file)
    extracted = extracted_data.get('data', {})
    truth = ground_truth.get(permit_number, {})
    
    if not truth:
        print(f"❌ No ground truth found for permit {permit_number}")
        return {'accuracy': 0, 'matches': 0, 'total': 0}
    
    total_matches = 0
    total_fields = 0
    all_diffs = []
    
    # Compare permit details
    print("\n📋 Permit Details:")
    matches, total, diffs = compare_permit_details(
        extracted.get('permitDetails', {}),
        truth.get('permitDetails', {})
    )
    total_matches += matches
    total_fields += total
    all_diffs.extend(diffs)
    print(f"   {matches}/{total} fields match ({matches/total*100:.1f}%)")
    if diffs:
        for diff in diffs:
            print(f"   ❌{diff}")
    
    # Compare generator sets
    print("\n🏭 Generator Sets:")
    matches, total, diffs = compare_generator_sets(
        extracted.get('generatorSets', []),
        truth.get('generatorSets', [])
    )
    total_matches += matches
    total_fields += total
    all_diffs.extend(diffs)
    print(f"   {matches}/{total} fields match ({matches/total*100:.1f}%)")
    if diffs:
        for diff in diffs[:10]:  # Show first 10 diffs
            print(f"   ❌{diff}")
        if len(diffs) > 10:
            print(f"   ... and {len(diffs) - 10} more differences")
    
    # Calculate overall accuracy
    accuracy = (total_matches / total_fields * 100) if total_fields > 0 else 0
    
    print(f"\n🎯 Overall Accuracy: {accuracy:.1f}% ({total_matches}/{total_fields} fields)")
    
    return {
        'permit_number': permit_number,
        'accuracy': accuracy,
        'matches': total_matches,
        'total': total_fields,
        'differences': len(all_diffs)
    }


def main():
    """Main validation function"""
    print("="*80)
    print("PRODUCTION EXTRACTOR VALIDATION")
    print("="*80)
    print("\nComparing extracted results against ground truth...")
    
    # Load ground truth
    ground_truth_file = Path("data/validation/ground_truth.json")
    ground_truth = load_json(ground_truth_file)
    
    # Validate each permit
    permits = ["11790", "11541", "73757"]
    results = []
    
    for permit_num in permits:
        extracted_file = Path(f"data/extracted/{permit_num}_simplified.json")
        if not extracted_file.exists():
            print(f"\n❌ Extracted file not found: {extracted_file}")
            continue
        
        result = validate_permit(permit_num, extracted_file, ground_truth)
        results.append(result)
    
    # Summary
    print(f"\n{'='*80}")
    print("VALIDATION SUMMARY")
    print(f"{'='*80}")
    
    avg_accuracy = sum(r['accuracy'] for r in results) / len(results) if results else 0
    total_matches = sum(r['matches'] for r in results)
    total_fields = sum(r['total'] for r in results)
    
    print(f"\n📊 Results by Permit:")
    for result in results:
        print(f"   Permit {result['permit_number']}: {result['accuracy']:.1f}% "
              f"({result['matches']}/{result['total']} fields)")
    
    print(f"\n🎯 Overall:")
    print(f"   Average Accuracy: {avg_accuracy:.1f}%")
    print(f"   Total Matches: {total_matches}/{total_fields} fields")
    print(f"   Total Differences: {sum(r['differences'] for r in results)}")
    
    # Assessment
    print(f"\n{'='*80}")
    print("PRODUCTION READINESS ASSESSMENT")
    print(f"{'='*80}")
    
    if avg_accuracy >= 90:
        print("\n✅ EXCELLENT - System is production-ready!")
        print("   Accuracy exceeds 90% threshold")
    elif avg_accuracy >= 75:
        print("\n⚠️  GOOD - System is mostly production-ready")
        print("   Accuracy is acceptable but some improvements recommended")
    elif avg_accuracy >= 60:
        print("\n⚠️  FAIR - System needs improvements before production")
        print("   Consider additional testing and refinement")
    else:
        print("\n❌ POOR - System not ready for production")
        print("   Significant improvements needed")
    
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
