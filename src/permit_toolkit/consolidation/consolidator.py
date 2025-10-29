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
    
    def flatten_permit(self, permit_data: Dict[str, Any], source_file: str = None, metadata: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Flatten permit JSON into tabular rows (one per generator).
        
        Args:
            permit_data: Permit dictionary from JSON
            source_file: Optional source filename for tracking
            metadata: Optional metadata from top-level JSON (extraction info)
            
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
            'facility_state': permit_details.get('facilityState'),
            'permit_extraction_notes': permit_details.get('extractionNotes'),
        }
        
        # Add source file if provided
        if source_file:
            base_info['source_file'] = source_file
        
        # Handle case with no generators
        if not generator_sets:
            records.append(base_info)
            return records
        
        # Create one record per generator
        for gen in generator_sets:
            record = base_info.copy()
            
            # Handle monitoring nested object
            monitoring = gen.get('monitoring', {}) or {}
            
            # Convert allowedOperatingModes array to comma-separated string
            allowed_modes = gen.get('allowedOperatingModes')
            allowed_modes_str = ', '.join(allowed_modes) if allowed_modes else None
            
            record.update({
                # Generator identification
                'num_generators': gen.get('numGenerators'),
                'generator_ref': gen.get('referenceNumber'),
                'make': gen.get('make'),
                'model': gen.get('model'),
                
                # Capacity - rated
                'rated_capacity_bhp': gen.get('ratedCapacityBHP'),
                'rated_capacity_kw': gen.get('ratedCapacityKW'),
                
                # Capacity - maximum
                'maximum_capacity_bhp': gen.get('maximumCapacityBHP'),
                'maximum_capacity_kw': gen.get('maximumCapacityKW'),
                
                # Fuel - primary, secondary, other
                'primary_fuel_type': gen.get('primaryFuelType'),
                'secondary_fuel_type': gen.get('secondaryFuelType'),
                'other_fuels': gen.get('otherFuels'),
                'fuel_grade': gen.get('fuelGrade'),
                'fuel_specification': gen.get('fuelSpecification'),
                'fuel_normalized': gen.get('fuelNormalized'),
                'ulsd': gen.get('ulsd'),
                
                # Fuel - throughput and sulfur
                'fuel_throughput_limit': gen.get('fuelThroughputLimit'),
                'fuel_throughput_scope': gen.get('fuelThroughputScope'),
                'fuel_throughput_group_ref': gen.get('fuelThroughputGroupRef'),
                'fuel_sulfur_content_pct': gen.get('fuelSulfurContent'),
                'fuel_sulfur_content_ppm': gen.get('fuelSulfurContentPpm'),
                
                # Operating parameters
                'operating_hours_limit_yr': gen.get('operatingHoursLimit'),
                'allowed_operating_modes': allowed_modes_str,
                'control_technology': gen.get('controlTechnology'),
                'opacity_limit_percent': gen.get('opacityLimitPercent'),
                
                # Permit project inclusion
                'included_in_permit_project': gen.get('includedInPermitProject'),
                
                # Instant emissions (lbs/hr)
                'nox_limit_lbs_hr': gen.get('noxEmissionLimitLbsHr'),
                'co_limit_lbs_hr': gen.get('coEmissionLimitLbsHr'),
                'voc_limit_lbs_hr': gen.get('vocEmissionLimitLbsHr'),
                'pm_limit_lbs_hr': gen.get('pmEmissionLimitLbsHr'),
                'pm10_limit_lbs_hr': gen.get('pm10EmissionLimitLbsHr'),
                'pm25_limit_lbs_hr': gen.get('pm25EmissionLimitLbsHr'),
                'so2_limit_lbs_hr': gen.get('so2EmissionLimitLbsHr'),
                'instant_emissions_aggregation_type': gen.get('instantEmissionsAggregationType') or gen.get('emissionsScope'),
                
                # Cumulative emissions (tons/yr)
                'nox_limit_tons_yr': gen.get('noxEmissionLimitTonsYr'),
                'co_limit_tons_yr': gen.get('coEmissionLimitTonsYr'),
                'voc_limit_tons_yr': gen.get('vocEmissionLimitTonsYr'),
                'pm_limit_tons_yr': gen.get('pmEmissionLimitTonsYr'),
                'pm10_limit_tons_yr': gen.get('pm10EmissionLimitTonsYr'),
                'pm25_limit_tons_yr': gen.get('pm25EmissionLimitTonsYr'),
                'so2_limit_tons_yr': gen.get('so2EmissionLimitTonsYr'),
                'cumulative_emissions_aggregation_type': gen.get('cumulativeEmissionsAggregationType') or gen.get('emissionsGroupRef'),
                
                # Testing and monitoring
                'stack_test_required': gen.get('stackTestRequired'),
                'hour_meter_required': monitoring.get('hourMeter'),
                'fuel_flow_meter_required': monitoring.get('fuelFlowMeter'),
                'observation_frequency': monitoring.get('observationFrequency'),
                'recordkeeping_window_years': monitoring.get('recordkeepingWindowYears'),
                
                # Regulatory applicability
                'nsps_subpart_iiii': gen.get('nspsSubpartIIII'),
                'mact_subpart_zzzz': gen.get('mactSubpartZZZZ'),
                
                # Extraction notes
                'generator_extraction_notes': gen.get('extractionNotes'),
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
            # Check if extraction_dir already IS the state directory
            state_dir = self.extraction_dir / self.state
            if state_dir.exists():
                json_files = list(state_dir.glob("*.json"))
            else:
                # extraction_dir is already the state-specific directory
                json_files = list(self.extraction_dir.glob("*.json"))
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
                full_data = self.load_json(json_file)
                
                # Extract metadata and permit data from new structure
                permit_data = full_data.get('data', full_data)  # Support both old and new structure
                
                # Build metadata dict from top-level fields (new structure)
                metadata = {
                    'extraction_date': full_data.get('extraction_date'),
                    'state': full_data.get('state'),
                    'model': full_data.get('model'),
                    'qa_qc_enabled': full_data.get('qa_qc_enabled'),
                    'cost_usd': full_data.get('cost_usd'),
                    'processing_time_sec': full_data.get('processing_time_sec'),
                    'completeness_score': full_data.get('completeness_score'),
                    'generator_count': full_data.get('generator_count'),
                } if 'data' in full_data else None
                
                records = self.flatten_permit(permit_data, source_file=json_file.name, metadata=metadata)
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
        
        # Sort by facility and generator (if columns exist)
        sort_columns = []
        if 'facility_name' in df.columns:
            sort_columns.append('facility_name')
        if 'generator_ref' in df.columns:
            sort_columns.append('generator_ref')
        
        if sort_columns:
            df = df.sort_values(sort_columns)
        
        # Save if output path specified
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # Use utf-8 encoding and ensure proper character handling
            df.to_csv(output_path, index=False, encoding='utf-8')
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
            'total_facilities': int(df['facility_name'].nunique()) if 'facility_name' in df.columns else 0,
            'total_generators': len(df),
            'total_capacity_kw': float(df['rated_capacity_kw'].sum()) if 'rated_capacity_kw' in df.columns else 0,
            'counties': int(df['facility_county'].nunique()) if 'facility_county' in df.columns else 0,
        }
        
        # Fuel type distribution (using primary_fuel_type)
        if 'primary_fuel_type' in df.columns:
            fuel_dist = df['primary_fuel_type'].value_counts().to_dict()
            summary['fuel_type_distribution'] = fuel_dist
        
        # Capacity by manufacturer
        if 'make' in df.columns and 'rated_capacity_kw' in df.columns:
            capacity_by_make = (
                df.groupby('make')['rated_capacity_kw']
                .sum()
                .sort_values(ascending=False)
                .to_dict()
            )
            summary['capacity_by_manufacturer'] = capacity_by_make
        
        return summary
