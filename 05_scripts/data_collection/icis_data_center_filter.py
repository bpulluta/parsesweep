#!/usr/bin/env python3
"""
ICIS Air Facilities Data Center Filter

This script reads EPA's Integrated Compliance Information System (ICIS) data
for Clean Air Act Stationary Sources and filters for data center facilities
by state.

Data center identification is based on:
- NAICS codes: 518210 (Data Processing, Hosting, and Related Services)
- NAICS codes: 541511, 541512, 541513, 541519 (Computer Systems Design Services)
- Facility names containing data center keywords
"""

import pandas as pd
import sys
from typing import List, Dict
import argparse


class ICISDataCenterFilter:
    def __init__(self, csv_file_path: str):
        """Initialize the filter with the CSV file path."""
        self.csv_file_path = csv_file_path
        self.data = None
        
        # NAICS codes related to data centers and computing services
        self.data_center_naics = [
            '518210',  # Data Processing, Hosting, and Related Services
            '541511',  # Custom Computer Programming Services
            '541512',  # Computer Systems Design Services
            '541513',  # Computer Facilities Management Services
            '541519',  # Other Computer Related Services
        ]
        
        # Keywords in facility names that might indicate data centers
        self.data_center_keywords = [
            'data center', 'datacenter', 'data centre', 'datacentre',
            'server farm', 'hosting', 'cloud', 'computing center',
            'technology center', 'tech center', 'it center',
            'data processing', 'data facility', 'data warehouse'
        ]
    
    def load_data(self, chunk_size: int = 10000) -> None:
        """Load the CSV data in chunks to handle large files efficiently."""
        print(f"Loading data from {self.csv_file_path}...")
        
        try:
            # Read the CSV file in chunks to handle large files
            chunks = []
            for chunk in pd.read_csv(self.csv_file_path, chunksize=chunk_size, low_memory=False):
                chunks.append(chunk)
            
            self.data = pd.concat(chunks, ignore_index=True)
            print(f"Successfully loaded {len(self.data)} records")
            
        except Exception as e:
            print(f"Error loading data: {e}")
            sys.exit(1)
    
    def filter_by_naics(self) -> pd.DataFrame:
        """Filter facilities by data center related NAICS codes."""
        if self.data is None:
            raise ValueError("Data not loaded. Call load_data() first.")
        
        # Convert NAICS_CODES to string and handle NaN values
        naics_mask = self.data['NAICS_CODES'].astype(str).str.contains(
            '|'.join(self.data_center_naics), 
            case=False, 
            na=False
        )
        
        return self.data[naics_mask]
    
    def filter_by_facility_name(self) -> pd.DataFrame:
        """Filter facilities by data center keywords in facility names."""
        if self.data is None:
            raise ValueError("Data not loaded. Call load_data() first.")
        
        # Create regex pattern for keywords
        keyword_pattern = '|'.join(self.data_center_keywords)
        
        name_mask = self.data['FACILITY_NAME'].astype(str).str.contains(
            keyword_pattern, 
            case=False, 
            na=False
        )
        
        return self.data[name_mask]
    
    def get_data_centers(self) -> pd.DataFrame:
        """Get all potential data center facilities (NAICS or name based)."""
        naics_filtered = self.filter_by_naics()
        name_filtered = self.filter_by_facility_name()
        
        # Combine both filters and remove duplicates
        combined = pd.concat([naics_filtered, name_filtered]).drop_duplicates()
        
        return combined
    
    def filter_by_state(self, states: List[str]) -> pd.DataFrame:
        """Filter data centers by specified states."""
        data_centers = self.get_data_centers()
        
        # Convert states to uppercase for consistency
        states_upper = [state.upper() for state in states]
        
        state_mask = data_centers['STATE'].isin(states_upper)
        
        return data_centers[state_mask]
    
    def get_summary_stats(self, filtered_data: pd.DataFrame) -> Dict:
        """Generate summary statistics for the filtered data."""
        stats = {
            'total_facilities': len(filtered_data),
            'states_count': filtered_data['STATE'].nunique(),
            'states_list': sorted(filtered_data['STATE'].unique().tolist()),
            'operating_status': filtered_data['AIR_OPERATING_STATUS_DESC'].value_counts().to_dict(),
            'facility_types': filtered_data['FACILITY_TYPE_CODE'].value_counts().to_dict(),
            'pollutant_classes': filtered_data['AIR_POLLUTANT_CLASS_DESC'].value_counts().to_dict()
        }
        
        return stats
    
    def export_results(self, filtered_data: pd.DataFrame, output_file: str) -> None:
        """Export filtered results to CSV."""
        try:
            filtered_data.to_csv(output_file, index=False)
            print(f"Results exported to {output_file}")
        except Exception as e:
            print(f"Error exporting results: {e}")
    
    def print_summary(self, filtered_data: pd.DataFrame) -> None:
        """Print a summary of the filtered data."""
        stats = self.get_summary_stats(filtered_data)
        
        print("\n" + "="*60)
        print("DATA CENTER FACILITIES SUMMARY")
        print("="*60)
        print(f"Total data center facilities found: {stats['total_facilities']}")
        print(f"Number of states represented: {stats['states_count']}")
        print(f"States: {', '.join(stats['states_list'])}")
        
        print("\nOperating Status Distribution:")
        for status, count in stats['operating_status'].items():
            print(f"  {status}: {count}")
        
        print("\nFacility Type Distribution:")
        for ftype, count in stats['facility_types'].items():
            print(f"  {ftype}: {count}")
        
        print("\nPollutant Class Distribution:")
        for pclass, count in stats['pollutant_classes'].items():
            print(f"  {pclass}: {count}")
        
        print("\n" + "="*60)


def main():
    """Main function to run the script."""
    parser = argparse.ArgumentParser(
        description='Filter ICIS Air Facilities data for data centers by state'
    )
    parser.add_argument(
        '--input', 
        default='ICIS-AIR_FACILITIES.csv',
        help='Input CSV file path (default: ICIS-AIR_FACILITIES.csv)'
    )
    parser.add_argument(
        '--states', 
        nargs='+',
        help='State codes to filter by (e.g., CA NY TX)'
    )
    parser.add_argument(
        '--output',
        help='Output CSV file path for filtered results'
    )
    parser.add_argument(
        '--summary-only',
        action='store_true',
        help='Only show summary statistics, do not display individual records'
    )
    
    args = parser.parse_args()
    
    # Initialize the filter
    icis_filter = ICISDataCenterFilter(args.input)
    
    # Load the data
    icis_filter.load_data()
    
    # Filter data
    if args.states:
        print(f"\nFiltering for states: {', '.join(args.states)}")
        filtered_data = icis_filter.filter_by_state(args.states)
    else:
        print("\nGetting all data center facilities...")
        filtered_data = icis_filter.get_data_centers()
    
    # Print summary
    icis_filter.print_summary(filtered_data)
    
    # Display sample records if not summary-only
    if not args.summary_only and len(filtered_data) > 0:
        print("\nSample Data Center Facilities:")
        print("-" * 100)
        
        # Select relevant columns for display
        display_columns = [
            'FACILITY_NAME', 'CITY', 'STATE', 'NAICS_CODES',
            'AIR_OPERATING_STATUS_DESC', 'FACILITY_TYPE_CODE'
        ]
        
        sample_data = filtered_data[display_columns].head(10)
        print(sample_data.to_string(index=False, max_colwidth=30))
        
        if len(filtered_data) > 10:
            print(f"\n... and {len(filtered_data) - 10} more facilities")
    
    # Export results if output file specified
    if args.output:
        icis_filter.export_results(filtered_data, args.output)
    
    print("\nProcessing complete!")


if __name__ == "__main__":
    main()