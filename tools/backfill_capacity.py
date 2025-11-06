#!/usr/bin/env python3
"""
Backfill missing capacity data in extracted JSON files.

This script scans extracted permit JSON files and uses engineering formulas
to calculate missing capacity values (kW, HP, BHP, MMBtu/hr) from available data.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Tuple
import argparse

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def calculate_missing_capacity(
    bhp: Any, hp: Any, kw: Any, mmbtu_hr: Any,
    eff_gen: float = 0.90, eff_fuel: float = 0.35
) -> Dict[str, Any]:
    """
    Calculate missing capacity values using engineering formulas.
    
    Based on standard conversion factors:
    - BHP/HP are treated as equivalent
    - kW = BHP/HP * 0.746 * Eff_gen
    - MMBTU/hr = BHP/HP * 0.746 * Eff_gen * 3412 / (Eff_fuel * 1,000,000)
    
    Args:
        bhp: Rated capacity in BHP (brake horsepower)
        hp: Rated capacity in HP (horsepower)
        kw: Rated capacity in kW (kilowatts)
        mmbtu_hr: Rated capacity in MMBTU/hr (million BTU per hour input)
        eff_gen: Generator efficiency (default 0.90)
        eff_fuel: Thermal efficiency (default 0.35)
        
    Returns:
        Dictionary with calculated values for all capacity fields and flags indicating which were calculated
    """
    result = {
        'ratedCapacityBHP': bhp,
        'ratedCapacityHP': hp,
        'ratedCapacityKW': kw,
        'ratedCapacityMMBtuPerHr': mmbtu_hr,
        'calculated_bhp': False,
        'calculated_hp': False,
        'calculated_kw': False,
        'calculated_mmbtu_per_hr': False
    }
    
    # Convert to numeric, handling None/empty values
    def to_float(val):
        if val is None or val == '':
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    
    bhp_val = to_float(bhp)
    hp_val = to_float(hp)
    kw_val = to_float(kw)
    mmbtu_val = to_float(mmbtu_hr)
    
    # Determine primary power value (BHP/HP are equivalent)
    power_hp = bhp_val if bhp_val is not None else hp_val
    
    # Case 1: Have BHP/HP, calculate missing kW and MMBTU/hr
    if power_hp is not None:
        # Fill in whichever HP field was missing (but don't override if both exist)
        if bhp_val is None:
            result['ratedCapacityBHP'] = power_hp
            result['calculated_bhp'] = True
        if hp_val is None:
            result['ratedCapacityHP'] = power_hp
            result['calculated_hp'] = True
            
        # Calculate kW ONLY if missing: kW = BHP * 0.746 * Eff_gen
        if kw_val is None:
            result['ratedCapacityKW'] = round(power_hp * 0.746 * eff_gen, 2)
            result['calculated_kw'] = True
        else:
            result['ratedCapacityKW'] = kw  # Preserve original value
        
        # Calculate MMBTU/hr ONLY if missing: MMBTU/hr = (BHP * 0.746 * Eff_gen * 3412) / (Eff_fuel * 1,000,000)
        if mmbtu_val is None:
            result['ratedCapacityMMBtuPerHr'] = round(
                (power_hp * 0.746 * eff_gen * 3412) / (eff_fuel * 1_000_000), 3
            )
            result['calculated_mmbtu_per_hr'] = True
        else:
            result['ratedCapacityMMBtuPerHr'] = mmbtu_hr  # Preserve original value
    
    # Case 2: Have kW, calculate missing BHP/HP and MMBTU/hr
    elif kw_val is not None:
        # Calculate BHP/HP from kW: BHP = kW / (0.746 * Eff_gen)
        calculated_hp = round(kw_val / (0.746 * eff_gen), 2)
        if bhp_val is None:
            result['ratedCapacityBHP'] = calculated_hp
            result['calculated_bhp'] = True
        else:
            result['ratedCapacityBHP'] = bhp  # Preserve original value
        if hp_val is None:
            result['ratedCapacityHP'] = calculated_hp
            result['calculated_hp'] = True
        else:
            result['ratedCapacityHP'] = hp  # Preserve original value
        
        # Calculate MMBTU/hr ONLY if missing: MMBTU/hr = (kW * 3412) / (Eff_fuel * 1,000,000)
        if mmbtu_val is None:
            result['ratedCapacityMMBtuPerHr'] = round(
                (kw_val * 3412) / (eff_fuel * 1_000_000), 3
            )
            result['calculated_mmbtu_per_hr'] = True
        else:
            result['ratedCapacityMMBtuPerHr'] = mmbtu_hr  # Preserve original value
    
    # Case 3: Have MMBTU/hr, calculate missing BHP/HP and kW
    elif mmbtu_val is not None:
        # Calculate BHP/HP from MMBTU/hr: BHP = (MMBTU/hr * Eff_fuel * 1,000,000) / (0.746 * Eff_gen * 3412)
        calculated_hp = round(
            (mmbtu_val * eff_fuel * 1_000_000) / (0.746 * eff_gen * 3412), 2
        )
        if bhp_val is None:
            result['ratedCapacityBHP'] = calculated_hp
            result['calculated_bhp'] = True
        else:
            result['ratedCapacityBHP'] = bhp  # Preserve original value
        if hp_val is None:
            result['ratedCapacityHP'] = calculated_hp
            result['calculated_hp'] = True
        else:
            result['ratedCapacityHP'] = hp  # Preserve original value
        
        # Calculate kW ONLY if missing: kW = (MMBTU/hr * Eff_fuel * 1,000,000) / 3412
        if kw_val is None:
            result['ratedCapacityKW'] = round(
                (mmbtu_val * eff_fuel * 1_000_000) / 3412, 2
            )
            result['calculated_kw'] = True
        else:
            result['ratedCapacityKW'] = kw  # Preserve original value
    
    return result


def process_json_file(file_path: Path, dry_run: bool = True) -> Tuple[int, int, List[str]]:
    """
    Process a single JSON file and backfill missing capacity data.
    
    Args:
        file_path: Path to JSON file
        dry_run: If True, don't write changes to disk
        
    Returns:
        Tuple of (generators_processed, generators_updated, update_details)
    """
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        generator_sets = data.get('data', {}).get('generatorSets', [])
        if not generator_sets:
            return 0, 0, []
        
        generators_updated = 0
        update_details = []
        
        for gen in generator_sets:
            ref = gen.get('referenceNumber', 'Unknown')
            
            # Get current capacity values
            bhp = gen.get('ratedCapacityBHP')
            hp = gen.get('ratedCapacityHP')
            kw = gen.get('ratedCapacityKW')
            mmbtu_hr = gen.get('ratedCapacityMMBtuPerHr')
            
            # Check if any values are missing
            has_missing = any(v is None for v in [bhp, hp, kw, mmbtu_hr])
            has_any_value = any(v is not None for v in [bhp, hp, kw, mmbtu_hr])
            
            if has_missing and has_any_value:
                # Calculate missing values
                capacity_values = calculate_missing_capacity(bhp, hp, kw, mmbtu_hr)
                
                # Determine what was calculated
                calculated = []
                if capacity_values['calculated_kw']:
                    calculated.append(f"kW={capacity_values['ratedCapacityKW']}")
                if capacity_values['calculated_hp']:
                    calculated.append(f"HP={capacity_values['ratedCapacityHP']}")
                if capacity_values['calculated_bhp']:
                    calculated.append(f"BHP={capacity_values['ratedCapacityBHP']}")
                if capacity_values['calculated_mmbtu_per_hr']:
                    calculated.append(f"MMBtu/hr={capacity_values['ratedCapacityMMBtuPerHr']}")
                
                if calculated:
                    # Update generator with calculated values
                    gen['ratedCapacityBHP'] = capacity_values['ratedCapacityBHP']
                    gen['ratedCapacityHP'] = capacity_values['ratedCapacityHP']
                    gen['ratedCapacityKW'] = capacity_values['ratedCapacityKW']
                    gen['ratedCapacityMMBtuPerHr'] = capacity_values['ratedCapacityMMBtuPerHr']
                    
                    # Add metadata about calculated values
                    if 'metadata' not in gen:
                        gen['metadata'] = {}
                    gen['metadata']['calculatedCapacityFields'] = {
                        'calculated_bhp': capacity_values['calculated_bhp'],
                        'calculated_hp': capacity_values['calculated_hp'],
                        'calculated_kw': capacity_values['calculated_kw'],
                        'calculated_mmbtu_per_hr': capacity_values['calculated_mmbtu_per_hr']
                    }
                    
                    generators_updated += 1
                    update_details.append(f"  {ref}: Calculated {', '.join(calculated)}")
        
        # Write back if not dry run and changes were made
        if not dry_run and generators_updated > 0:
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=2)
        
        return len(generator_sets), generators_updated, update_details
        
    except Exception as e:
        logger.error(f"Error processing {file_path}: {e}")
        return 0, 0, []


def main():
    parser = argparse.ArgumentParser(
        description='Backfill missing capacity data in extracted permit JSON files.'
    )
    parser.add_argument(
        'extracted_dir',
        type=Path,
        nargs='?',
        default=Path('data/extracted'),
        help='Directory containing extracted JSON files (default: data/extracted)'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually write changes to files (default is dry-run)'
    )
    parser.add_argument(
        '--state',
        type=str,
        help='Process only files from a specific state directory'
    )
    
    args = parser.parse_args()
    
    if not args.extracted_dir.exists():
        logger.error(f"Directory not found: {args.extracted_dir}")
        return 1
    
    # Find all JSON files
    if args.state:
        state_dir = args.extracted_dir / args.state
        if not state_dir.exists():
            logger.error(f"State directory not found: {state_dir}")
            return 1
        json_files = list(state_dir.glob('*.json'))
    else:
        json_files = list(args.extracted_dir.rglob('*.json'))
    
    if not json_files:
        logger.warning(f"No JSON files found in {args.extracted_dir}")
        return 0
    
    logger.info(f"{'DRY RUN - ' if not args.execute else ''}Processing {len(json_files)} JSON files...")
    if not args.execute:
        logger.info("(Use --execute to write changes to files)")
    
    total_generators = 0
    total_updated = 0
    files_with_updates = 0
    
    for json_file in sorted(json_files):
        gens_processed, gens_updated, details = process_json_file(json_file, dry_run=not args.execute)
        
        total_generators += gens_processed
        total_updated += gens_updated
        
        if gens_updated > 0:
            files_with_updates += 1
            rel_path = json_file.relative_to(args.extracted_dir)
            print(f"\n{rel_path}:")
            for detail in details:
                print(detail)
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Files processed: {len(json_files)}")
    print(f"Files with updates: {files_with_updates}")
    print(f"Total generators processed: {total_generators}")
    print(f"Total generators updated: {total_updated}")
    if total_generators > 0:
        print(f"Update rate: {total_updated/total_generators*100:.1f}%")
    
    if not args.execute and total_updated > 0:
        print(f"\n⚠️  This was a DRY RUN - no files were modified.")
        print(f"Run with --execute to apply changes.")
    
    return 0


if __name__ == '__main__':
    exit(main())
