"""Data consolidation - convert extracted JSON to analysis-ready datasets."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)


class PermitConsolidator:
    """
    Consolidate extracted permit JSON files into CSV datasets.
    
    Flattens nested JSON structures into tabular format with one row
    per generator set, suitable for analysis and reporting.
    """
    
    def __init__(self, extraction_dir: Path, state: str = None):
        """
        Initialize consolidator.
        
        Args:
            extraction_dir: Directory containing extracted JSON files
            state: Optional state name to filter (e.g., 'Virginia')
        """
        self.extraction_dir = Path(extraction_dir)
        self.state = state
        
    def load_json(self, json_path: Path) -> Dict[str, Any]:
        """Load a single extracted permit JSON file."""
        with open(json_path, 'r') as f:
            return json.load(f)
    
    def flatten_permit(self, permit_data: Dict[str, Any], source_file: str = None) -> List[Dict[str, Any]]:
        """
        Flatten permit JSON into tabular rows (one per generator).
        
        Args:
            permit_data: Permit dictionary from JSON
            source_file: Optional source filename for tracking
            
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
        
        if source_file:
            base_info['source_file'] = source_file
        
        # Handle case with no generators
        if not generator_sets:
            records.append(base_info)
            return records
        
        # Create one record per generator
        for gen in generator_sets:
            record = base_info.copy()
            record.update({
                # Generator identification
                'num_generators': gen.get('numGenerators'),
                'generator_ref': gen.get('referenceNumber'),
                'make': gen.get('make'),
                'model': gen.get('model'),
                
                # Capacity
                'rated_capacity_bhp': gen.get('ratedCapacityBHP'),
                'rated_capacity_kw': gen.get('ratedCapacityKW'),
                'rated_capacity_mw': (
                    gen.get('ratedCapacityKW') / 1000 
                    if gen.get('ratedCapacityKW') else None
                ),
                
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
    
    def consolidate(self, output_path: Path = None) -> pd.DataFrame:
        """
        Consolidate all JSON extractions into a single DataFrame.
        
        Args:
            output_path: Optional path to save CSV output
            
        Returns:
            Consolidated pandas DataFrame
        """
        # Find JSON files
        if self.state:
            json_files = list((self.extraction_dir / self.state).glob("*.json"))
        else:
            json_files = list(self.extraction_dir.glob("**/*.json"))
        
        if not json_files:
            logger.error(f"No JSON files found in {self.extraction_dir}")
            return pd.DataFrame()
        
        logger.info(f"Found {len(json_files)} JSON files to process")
        
        all_records = []
        errors = []
        
        for json_file in json_files:
            try:
                logger.debug(f"Processing: {json_file.name}")
                data = self.load_json(json_file)
                records = self.flatten_permit(data, source_file=json_file.name)
                all_records.extend(records)
                logger.debug(f"  ✓ Extracted {len(records)} generator records")
                
            except Exception as e:
                error_msg = f"Error processing {json_file.name}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        if not all_records:
            logger.error("No records extracted from JSON files")
            return pd.DataFrame()
        
        # Create DataFrame
        df = pd.DataFrame(all_records)
        
        # Sort by facility and generator
        df = df.sort_values(['facility_name', 'generator_ref'])
        
        # Save if output path specified
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False)
            logger.info(f"✓ Saved consolidated data to: {output_path}")
        
        # Log summary
        logger.info(f"✓ Consolidation complete!")
        logger.info(f"  Total facilities: {df['facility_name'].nunique()}")
        logger.info(f"  Total generator records: {len(df)}")
        
        if errors:
            logger.warning(f"  {len(errors)} errors encountered during processing")
        
        return df
    
    def generate_summary(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Generate summary statistics from consolidated data.
        
        Args:
            df: Consolidated DataFrame
            
        Returns:
            Dictionary with summary statistics
        """
        summary = {
            'total_facilities': int(df['facility_name'].nunique()),
            'total_generators': len(df),
            'total_capacity_mw': float(df['rated_capacity_mw'].sum()),
            'counties': int(df['facility_county'].nunique()),
        }
        
        # Fuel type distribution
        fuel_dist = df['fuel_type'].value_counts().to_dict()
        summary['fuel_type_distribution'] = fuel_dist
        
        # Capacity by manufacturer
        capacity_by_make = (
            df.groupby('make')['rated_capacity_mw']
            .sum()
            .sort_values(ascending=False)
            .to_dict()
        )
        summary['capacity_by_manufacturer'] = capacity_by_make
        
        return summary
