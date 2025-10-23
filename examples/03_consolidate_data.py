"""
Example: Consolidating extracted data to CSV

This example demonstrates how to consolidate extracted JSON files
into a single CSV dataset for analysis.
"""

from pathlib import Path
from permit_toolkit.consolidation import PermitConsolidator

def main():
    # Configure paths
    extraction_dir = Path("data/extracted/Virginia")
    output_path = Path("data/outputs/virginia_generators.csv")
    
    if not extraction_dir.exists():
        print(f"ERROR: Extraction directory not found: {extraction_dir}")
        print("Please run the extraction example first!")
        return
    
    # Create consolidator
    consolidator = PermitConsolidator(
        extraction_dir=extraction_dir,
        state="Virginia"
    )
    
    print(f"Consolidating extractions from: {extraction_dir}")
    print(f"Output: {output_path}\n")
    
    # Consolidate
    df = consolidator.consolidate(output_path=output_path)
    
    if df.empty:
        print("No data to consolidate")
        return
    
    # Generate and display summary
    summary = consolidator.generate_summary(df)
    
    print(f"\n{'='*80}")
    print("CONSOLIDATION SUMMARY")
    print(f"{'='*80}")
    print(f"Total facilities: {summary['total_facilities']}")
    print(f"Total generators: {summary['total_generators']}")
    print(f"Total capacity: {summary['total_capacity_mw']:.2f} MW")
    print(f"Counties: {summary['counties']}")
    
    print("\nFuel type distribution:")
    for fuel, count in summary['fuel_type_distribution'].items():
        print(f"  {fuel}: {count}")
    
    print("\nTop manufacturers by capacity:")
    for make, capacity in list(summary['capacity_by_manufacturer'].items())[:5]:
        print(f"  {make}: {capacity:.2f} MW")
    
    print(f"\n✓ Dataset saved to: {output_path}")
    print("You can now analyze the data with pandas, Excel, or other tools!")


if __name__ == "__main__":
    main()
