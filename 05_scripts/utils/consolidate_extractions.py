#!/usr/bin/env python3
"""
Consolidate Extracted Permit Data

This script consolidates all extracted JSON files from LlamaExtract
into a single CSV dataset for analysis.

Usage:
    python consolidate_extractions.py
"""

import json
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any
import sys


def load_extracted_json(json_path: Path) -> Dict[str, Any]:
    """Load a single extracted permit JSON file."""
    with open(json_path, 'r') as f:
        return json.load(f)


def flatten_generator_data(permit_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flatten permit JSON into rows (one per generator).
    
    Returns:
        List of dictionaries, one per generator set
    """
    records = []
    
    permit_details = permit_data.get('permitDetails', {})
    generator_sets = permit_data.get('generatorSets', [])
    
    # Base information from permit
    base_info = {
        'permit_number': permit_details.get('permitNumber'),
        'permit_issuance_date': permit_details.get('permitIssuanceDate'),
        'permit_expiration_date': permit_details.get('permitExpirationDate'),
        'facility_name': permit_details.get('facilityName'),
        'facility_address': permit_details.get('facilityAddress'),
        'facility_county': permit_details.get('facilityCounty'),
    }
    
    # Create one record per generator
    for gen in generator_sets:
        record = base_info.copy()
        record.update({
            # Generator identification
            'generator_ref': gen.get('referenceNumber'),
            'make': gen.get('make'),
            'model': gen.get('model'),
            
            # Capacity
            'rated_capacity_bhp': gen.get('ratedCapacityBHP'),
            'rated_capacity_kw': gen.get('ratedCapacityKW'),
            'rated_capacity_mw': gen.get('ratedCapacityKW') / 1000 if gen.get('ratedCapacityKW') else None,
            
            # Fuel
            'fuel_type': gen.get('fuelType'),
            'fuel_throughput_gal_yr': gen.get('fuelThroughputLimit'),
            'fuel_sulfur_content': gen.get('fuelSulfurContent'),
            
            # Operating parameters
            'operating_hours_limit_yr': gen.get('operatingHoursLimit'),
            'control_technology': gen.get('controlTechnology'),
            
            # Emissions - NOx
            'nox_limit_lbs_hr': gen.get('noxEmissionLimitLbsHr'),
            'nox_limit_tons_yr': gen.get('noxEmissionLimitTonsYr'),
            
            # Emissions - CO
            'co_limit_lbs_hr': gen.get('coEmissionLimitLbsHr'),
            'co_limit_tons_yr': gen.get('coEmissionLimitTonsYr'),
            
            # Emissions - VOC
            'voc_limit_lbs_hr': gen.get('vocEmissionLimitLbsHr'),
            'voc_limit_tons_yr': gen.get('vocEmissionLimitTonsYr'),
            
            # Emissions - PM
            'pm_limit_lbs_hr': gen.get('pmEmissionLimitLbsHr'),
            'pm_limit_tons_yr': gen.get('pmEmissionLimitTonsYr'),
            
            # Emissions - PM10
            'pm10_limit_lbs_hr': gen.get('pm10EmissionLimitLbsHr'),
            'pm10_limit_tons_yr': gen.get('pm10EmissionLimitTonsYr'),
            
            # Emissions - SO2
            'so2_limit_lbs_hr': gen.get('so2EmissionLimitLbsHr'),
            'so2_limit_tons_yr': gen.get('so2EmissionLimitTonsYr'),
            
            # Testing
            'stack_test_required': gen.get('stackTestRequired'),
        })
        
        records.append(record)
    
    return records


def consolidate_all_extractions(extraction_dir: str, output_csv: str):
    """
    Consolidate all JSON extractions into a single CSV.
    
    Args:
        extraction_dir: Root directory containing extracted JSON files
        output_csv: Path to output CSV file
    """
    extraction_path = Path(extraction_dir)
    
    if not extraction_path.exists():
        print(f"Error: Directory not found: {extraction_dir}")
        sys.exit(1)
    
    # Find all JSON files
    json_files = list(extraction_path.glob("**/*.json"))
    
    if not json_files:
        print(f"Error: No JSON files found in {extraction_dir}")
        sys.exit(1)
    
    print(f"Found {len(json_files)} JSON files to process")
    
    all_records = []
    errors = []
    
    for json_file in json_files:
        try:
            print(f"Processing: {json_file.name}")
            data = load_extracted_json(json_file)
            records = flatten_generator_data(data)
            all_records.extend(records)
            print(f"  ✓ Extracted {len(records)} generator records")
            
        except Exception as e:
            error_msg = f"Error processing {json_file.name}: {str(e)}"
            print(f"  ✗ {error_msg}")
            errors.append(error_msg)
    
    if not all_records:
        print("Error: No records extracted from JSON files")
        sys.exit(1)
    
    # Create DataFrame
    df = pd.DataFrame(all_records)
    
    # Sort by facility and generator
    df = df.sort_values(['facility_name', 'generator_ref'])
    
    # Save to CSV
    df.to_csv(output_csv, index=False)
    
    print(f"\n✓ Consolidation complete!")
    print(f"  Total facilities: {df['facility_name'].nunique()}")
    print(f"  Total generators: {len(df)}")
    print(f"  Output saved to: {output_csv}")
    
    if errors:
        print(f"\n⚠ {len(errors)} errors encountered:")
        for error in errors:
            print(f"  - {error}")
    
    # Summary statistics
    print(f"\n=== Dataset Summary ===")
    print(f"States: {df['facility_county'].nunique()} counties")
    print(f"Total capacity (MW): {df['rated_capacity_mw'].sum():.2f}")
    print(f"\nFuel type distribution:")
    print(df['fuel_type'].value_counts())
    print(f"\nCapacity by make:")
    print(df.groupby('make')['rated_capacity_mw'].sum().sort_values(ascending=False))


if __name__ == '__main__':
    # Default paths
    EXTRACTION_DIR = '04_extracted_data/llamaextract'
    OUTPUT_CSV = '06_outputs/datasets/consolidated_backup_generators.csv'
    
    print("=== LlamaExtract Data Consolidation ===\n")
    consolidate_all_extractions(EXTRACTION_DIR, OUTPUT_CSV)
