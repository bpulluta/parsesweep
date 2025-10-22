#!/usr/bin/env python3
"""
Validate Extracted Permit Data

This script validates extracted JSON files against the schema
and generates a quality control report.

Usage:
    python validate_extractions.py
"""

import json
import jsonschema
from pathlib import Path
from typing import Dict, List, Tuple
import sys


def load_schema(schema_path: str) -> Dict:
    """Load the JSON schema."""
    with open(schema_path, 'r') as f:
        return json.load(f)


def load_extraction(json_path: Path) -> Dict:
    """Load an extracted permit JSON file."""
    with open(json_path, 'r') as f:
        return json.load(f)


def validate_against_schema(data: Dict, schema: Dict) -> Tuple[bool, List[str]]:
    """
    Validate data against schema.
    
    Returns:
        (is_valid, list_of_errors)
    """
    try:
        jsonschema.validate(instance=data, schema=schema)
        return True, []
    except jsonschema.ValidationError as e:
        return False, [str(e)]
    except Exception as e:
        return False, [f"Unexpected error: {str(e)}"]


def check_data_quality(data: Dict) -> Dict[str, any]:
    """
    Check data quality beyond schema validation.
    
    Returns dictionary with quality metrics.
    """
    issues = []
    metrics = {}
    
    permit_details = data.get('permitDetails', {})
    generator_sets = data.get('generatorSets', [])
    
    # Check permit details completeness
    if not permit_details.get('permitNumber'):
        issues.append("Missing permit number")
    if not permit_details.get('facilityName'):
        issues.append("Missing facility name")
    if not permit_details.get('facilityCounty'):
        issues.append("Missing facility county")
    
    # Check generators
    if not generator_sets or len(generator_sets) == 0:
        issues.append("No generator sets found")
    
    metrics['num_generators'] = len(generator_sets)
    metrics['has_capacity'] = 0
    metrics['has_fuel_type'] = 0
    metrics['has_operating_hours'] = 0
    
    for idx, gen in enumerate(generator_sets):
        # Check critical fields
        if not gen.get('make') and not gen.get('model'):
            issues.append(f"Generator {idx+1}: Missing make and model")
        
        if gen.get('ratedCapacityKW'):
            metrics['has_capacity'] += 1
            # Validate capacity is reasonable (10 kW to 10 MW for backup generators)
            capacity = gen.get('ratedCapacityKW', 0)
            if capacity < 10 or capacity > 10000:
                issues.append(f"Generator {gen.get('referenceNumber', idx+1)}: Unusual capacity {capacity} kW")
        
        if gen.get('fuelType'):
            metrics['has_fuel_type'] += 1
        
        if gen.get('operatingHoursLimit'):
            metrics['has_operating_hours'] += 1
            # Check if hours limit is reasonable (typical: 100-500 hours/year)
            hours = gen.get('operatingHoursLimit', 0)
            if hours > 8760:  # More than hours in a year
                issues.append(f"Generator {gen.get('referenceNumber', idx+1)}: Invalid operating hours {hours}")
    
    # Calculate completeness percentages
    if metrics['num_generators'] > 0:
        metrics['capacity_completeness'] = metrics['has_capacity'] / metrics['num_generators'] * 100
        metrics['fuel_completeness'] = metrics['has_fuel_type'] / metrics['num_generators'] * 100
        metrics['hours_completeness'] = metrics['has_operating_hours'] / metrics['num_generators'] * 100
    else:
        metrics['capacity_completeness'] = 0
        metrics['fuel_completeness'] = 0
        metrics['hours_completeness'] = 0
    
    return {
        'issues': issues,
        'metrics': metrics
    }


def validate_all_extractions(extraction_dir: str, schema_path: str):
    """
    Validate all JSON extractions and generate QC report.
    
    Args:
        extraction_dir: Root directory containing extracted JSON files
        schema_path: Path to the JSON schema file
    """
    extraction_path = Path(extraction_dir)
    
    if not extraction_path.exists():
        print(f"Error: Directory not found: {extraction_dir}")
        sys.exit(1)
    
    # Load schema
    print(f"Loading schema from {schema_path}")
    schema = load_schema(schema_path)
    
    # Find all JSON files
    json_files = list(extraction_path.glob("**/*.json"))
    
    if not json_files:
        print(f"Error: No JSON files found in {extraction_dir}")
        sys.exit(1)
    
    print(f"\nValidating {len(json_files)} JSON files\n")
    
    results = {
        'total': len(json_files),
        'schema_valid': 0,
        'schema_invalid': 0,
        'quality_issues': 0,
        'details': []
    }
    
    for json_file in json_files:
        print(f"Checking: {json_file.name}")
        
        try:
            data = load_extraction(json_file)
            
            # Schema validation
            is_valid, schema_errors = validate_against_schema(data, schema)
            
            # Quality checks
            quality_check = check_data_quality(data)
            
            file_result = {
                'file': json_file.name,
                'schema_valid': is_valid,
                'schema_errors': schema_errors,
                'quality_issues': quality_check['issues'],
                'metrics': quality_check['metrics']
            }
            
            results['details'].append(file_result)
            
            if is_valid:
                results['schema_valid'] += 1
                print("  ✓ Schema valid")
            else:
                results['schema_invalid'] += 1
                print(f"  ✗ Schema errors: {len(schema_errors)}")
                for error in schema_errors[:2]:  # Show first 2 errors
                    print(f"    - {error[:100]}")
            
            if quality_check['issues']:
                results['quality_issues'] += 1
                print(f"  ⚠ Quality issues: {len(quality_check['issues'])}")
                for issue in quality_check['issues'][:3]:  # Show first 3 issues
                    print(f"    - {issue}")
            
            # Show metrics
            metrics = quality_check['metrics']
            print(f"    Generators: {metrics['num_generators']}, "
                  f"Capacity: {metrics['capacity_completeness']:.0f}%, "
                  f"Fuel: {metrics['fuel_completeness']:.0f}%, "
                  f"Hours: {metrics['hours_completeness']:.0f}%")
            
        except Exception as e:
            print(f"  ✗ Error loading file: {str(e)}")
            results['details'].append({
                'file': json_file.name,
                'error': str(e)
            })
    
    # Print summary
    print("\n" + "="*60)
    print("VALIDATION SUMMARY")
    print("="*60)
    print(f"Total files:          {results['total']}")
    print(f"Schema valid:         {results['schema_valid']} ({results['schema_valid']/results['total']*100:.1f}%)")
    print(f"Schema invalid:       {results['schema_invalid']}")
    print(f"With quality issues:  {results['quality_issues']}")
    
    # Calculate aggregate metrics
    total_generators = sum(r['metrics']['num_generators'] for r in results['details'] if 'metrics' in r)
    avg_capacity_complete = sum(r['metrics']['capacity_completeness'] for r in results['details'] if 'metrics' in r) / len(results['details'])
    avg_fuel_complete = sum(r['metrics']['fuel_completeness'] for r in results['details'] if 'metrics' in r) / len(results['details'])
    avg_hours_complete = sum(r['metrics']['hours_completeness'] for r in results['details'] if 'metrics' in r) / len(results['details'])
    
    print(f"\nTotal generators:     {total_generators}")
    print(f"Avg capacity data:    {avg_capacity_complete:.1f}%")
    print(f"Avg fuel type data:   {avg_fuel_complete:.1f}%")
    print(f"Avg hours data:       {avg_hours_complete:.1f}%")
    
    # Save detailed report
    report_path = '06_outputs/reports/validation_report.json'
    with open(report_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nDetailed report saved to: {report_path}")


if __name__ == '__main__':
    # Default paths
    EXTRACTION_DIR = '04_extracted_data/llamaextract'
    SCHEMA_PATH = 'docs/schemas/air_quality_permits_schema.json'
    
    print("=== Extraction Validation Tool ===\n")
    validate_all_extractions(EXTRACTION_DIR, SCHEMA_PATH)
