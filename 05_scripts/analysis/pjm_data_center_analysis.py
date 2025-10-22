#!/usr/bin/env python3
"""
ICIS Data Center Filter for PJM Territory

This script filters EPA's ICIS air quality permits data for data centers
in the PJM territory states: Delaware, Illinois, Indiana, Kentucky, Maryland, 
Michigan, New Jersey, North Carolina, Ohio, Pennsylvania, Tennessee, Virginia, 
and West Virginia.
"""

import pandas as pd
import sys


def filter_pjm_data_centers(csv_file='ICIS-AIR_FACILITIES.csv'):
    """
    Filter ICIS data for data centers in PJM territory states.
    
    Returns:
        pandas.DataFrame: Data centers in PJM states with air quality permits
    """
    
    # PJM territory states
    pjm_states = [
        'DE',  # Delaware
        'IL',  # Illinois
        'IN',  # Indiana
        'KY',  # Kentucky
        'MD',  # Maryland
        'MI',  # Michigan
        'NJ',  # New Jersey
        'NC',  # North Carolina
        'OH',  # Ohio
        'PA',  # Pennsylvania
        'TN',  # Tennessee
        'VA',  # Virginia
        'WV'   # West Virginia
    ]
    
    print("Loading ICIS Air Quality Permits data...")
    print(f"Target PJM States: {', '.join(pjm_states)}")
    
    try:
        # Read CSV in chunks to handle large file
        chunks = []
        for chunk in pd.read_csv(csv_file, chunksize=10000, low_memory=False):
            chunks.append(chunk)
        
        data = pd.concat(chunks, ignore_index=True)
        print(f"Loaded {len(data)} total facilities")
        
    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)
    
    # Data center NAICS codes
    data_center_naics = [
        '518210',  # Data Processing, Hosting, and Related Services
        '541511',  # Custom Computer Programming Services
        '541512',  # Computer Systems Design Services
        '541513',  # Computer Facilities Management Services
        '541519'   # Other Computer Related Services
    ]
    
    # Data center keywords for facility names
    data_center_keywords = [
        'data center', 'datacenter', 'data centre', 'datacentre',
        'server farm', 'hosting', 'cloud computing', 'computing center',
        'technology center', 'tech center', 'it center',
        'data processing', 'data facility', 'data warehouse',
        'colocation', 'colo', 'internet exchange'
    ]
    
    print("Filtering for data center facilities...")
    
    # Filter by NAICS codes
    naics_filter = data['NAICS_CODES'].astype(str).str.contains(
        '|'.join(data_center_naics), case=False, na=False
    )
    
    # Filter by facility name keywords
    keyword_pattern = '|'.join(data_center_keywords)
    name_filter = data['FACILITY_NAME'].astype(str).str.contains(
        keyword_pattern, case=False, na=False
    )
    
    # Combine NAICS and name filters to get all potential data centers
    data_centers = data[naics_filter | name_filter]
    print(f"Found {len(data_centers)} potential data center facilities nationwide")
    
    # Filter for PJM states
    pjm_data_centers = data_centers[data_centers['STATE'].isin(pjm_states)]
    print(f"Found {len(pjm_data_centers)} data centers in PJM territory")
    
    return pjm_data_centers


def analyze_pjm_data_centers(pjm_data):
    """Analyze the PJM data center facilities."""
    
    print("\n" + "="*70)
    print("PJM TERRITORY DATA CENTER AIR QUALITY PERMITS ANALYSIS")
    print("="*70)
    
    print(f"Total data center facilities in PJM territory: {len(pjm_data)}")
    
    # Breakdown by state
    print("\nData centers by PJM state:")
    state_counts = pjm_data['STATE'].value_counts()
    for state, count in state_counts.items():
        state_names = {
            'DE': 'Delaware', 'IL': 'Illinois', 'IN': 'Indiana', 'KY': 'Kentucky',
            'MD': 'Maryland', 'MI': 'Michigan', 'NJ': 'New Jersey', 'NC': 'North Carolina',
            'OH': 'Ohio', 'PA': 'Pennsylvania', 'TN': 'Tennessee', 'VA': 'Virginia', 'WV': 'West Virginia'
        }
        print(f"  {state} ({state_names.get(state, state)}): {count}")
    
    # Operating status
    print("\nOperating status distribution:")
    status_counts = pjm_data['AIR_OPERATING_STATUS_DESC'].value_counts()
    for status, count in status_counts.items():
        print(f"  {status}: {count}")
    
    # Air pollutant class
    print("\nAir pollutant class distribution:")
    pollutant_counts = pjm_data['AIR_POLLUTANT_CLASS_DESC'].value_counts()
    for pollutant_class, count in pollutant_counts.items():
        print(f"  {pollutant_class}: {count}")
    
    # Facility type
    print("\nFacility type distribution:")
    facility_counts = pjm_data['FACILITY_TYPE_CODE'].value_counts()
    facility_types = {
        'POF': 'Point Source',
        'NON': 'Non-Point Source', 
        'CNG': 'Conditionally Exempt Non-Point',
        'COR': 'Corporate',
        'CTG': 'Control Technique Guidelines',
        'MXO': 'Mixed Operations',
        'STF': 'State Title V Facility',
        'DIS': 'Distributed',
        'FDF': 'Federal Facility'
    }
    for ftype, count in facility_counts.items():
        type_desc = facility_types.get(ftype, ftype)
        print(f"  {ftype} ({type_desc}): {count}")
    
    return {
        'total': len(pjm_data),
        'by_state': state_counts.to_dict(),
        'by_status': status_counts.to_dict(),
        'by_pollutant_class': pollutant_counts.to_dict(),
        'by_facility_type': facility_counts.to_dict()
    }


def display_facility_details(pjm_data):
    """Display detailed information about the facilities."""
    
    print("\n" + "="*70)
    print("DETAILED FACILITY INFORMATION")
    print("="*70)
    
    # Key columns for air quality permit analysis
    key_columns = [
        'FACILITY_NAME', 'CITY', 'STATE', 'COUNTY_NAME',
        'NAICS_CODES', 'AIR_OPERATING_STATUS_DESC', 
        'AIR_POLLUTANT_CLASS_DESC', 'FACILITY_TYPE_CODE',
        'CURRENT_HPV'  # High Priority Violation status
    ]
    
    # Display all facilities
    display_data = pjm_data[key_columns].copy()
    
    print(f"All {len(display_data)} PJM Data Center Facilities:")
    print("-" * 120)
    
    # Sort by state then by facility name
    display_data = display_data.sort_values(['STATE', 'FACILITY_NAME'])
    
    for idx, row in display_data.iterrows():
        print(f"{row['STATE']} | {row['FACILITY_NAME'][:40]:<40} | {row['CITY'][:20]:<20} | {row['AIR_OPERATING_STATUS_DESC'][:15]:<15} | {row['AIR_POLLUTANT_CLASS_DESC'][:20]:<20}")
    
    return display_data


def export_pjm_results(pjm_data):
    """Export PJM data center results to CSV files."""
    
    # Main export file
    main_file = 'pjm_data_centers_air_permits.csv'
    pjm_data.to_csv(main_file, index=False)
    print(f"\nAll PJM data center permits exported to: {main_file}")
    
    # Operating facilities only
    operating_data = pjm_data[pjm_data['AIR_OPERATING_STATUS_DESC'] == 'Operating']
    operating_file = 'pjm_operating_data_centers.csv'
    operating_data.to_csv(operating_file, index=False)
    print(f"Operating facilities only exported to: {operating_file}")
    
    # Summary by state
    state_summary = pjm_data.groupby('STATE').agg({
        'FACILITY_NAME': 'count',
        'AIR_OPERATING_STATUS_DESC': lambda x: (x == 'Operating').sum(),
        'AIR_POLLUTANT_CLASS_DESC': lambda x: x.value_counts().index[0] if len(x) > 0 else 'N/A'
    }).rename(columns={
        'FACILITY_NAME': 'Total_Facilities',
        'AIR_OPERATING_STATUS_DESC': 'Operating_Facilities',
        'AIR_POLLUTANT_CLASS_DESC': 'Most_Common_Pollutant_Class'
    })
    
    summary_file = 'pjm_data_centers_summary_by_state.csv'
    state_summary.to_csv(summary_file)
    print(f"State summary exported to: {summary_file}")
    
    return {
        'main_file': main_file,
        'operating_file': operating_file,
        'summary_file': summary_file
    }


def main():
    """Main function to execute PJM data center analysis."""
    
    print("EPA ICIS Air Quality Permits - PJM Territory Data Centers")
    print("="*70)
    
    # Filter data for PJM data centers
    pjm_data_centers = filter_pjm_data_centers()
    
    if len(pjm_data_centers) == 0:
        print("No data centers found in PJM territory.")
        return
    
    # Analyze the data
    analysis_results = analyze_pjm_data_centers(pjm_data_centers)
    
    # Display detailed facility information
    display_facility_details(pjm_data_centers)
    
    # Export results
    export_files = export_pjm_results(pjm_data_centers)
    
    print("\n" + "="*70)
    print("ANALYSIS COMPLETE")
    print("="*70)
    print("Generated files:")
    for file_type, filename in export_files.items():
        print(f"  - {filename}")
    
    print("\nKey findings:")
    print(f"  - {analysis_results['total']} data center facilities with air quality permits in PJM territory")
    print(f"  - States with most facilities: {', '.join(list(analysis_results['by_state'].keys())[:3])}")
    operating_count = analysis_results['by_status'].get('Operating', 0)
    print(f"  - {operating_count} facilities currently operating")


if __name__ == "__main__":
    main()