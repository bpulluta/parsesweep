"""Data consolidation - convert extracted JSON to analysis-ready datasets."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any
import unicodedata

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


def clean_text(text: Any) -> Any:
    """
    Clean text fields by normalizing Unicode and removing problematic characters.
    
    Args:
        text: Input text or other data type
        
    Returns:
        Cleaned text or original value if not a string
    """
    if not isinstance(text, str):
        return text
    
    # Normalize Unicode characters (NFKC handles compatibility characters)
    text = unicodedata.normalize('NFKC', text)
    
    # Replace common problematic characters with ASCII equivalents
    replacements = {
        '\u2265': '>=',  # ≥ greater than or equal
        '\u2264': '<=',  # ≤ less than or equal  
        '\u00b0': 'deg', # ° degree symbol
        '\u2013': '-',   # – en dash
        '\u2014': '-',   # — em dash
        '\u2018': "'",   # ' left single quote
        '\u2019': "'",   # ' right single quote
        '\u201c': '"',   # " left double quote
        '\u201d': '"',   # " right double quote
        '\u00a0': ' ',   # non-breaking space
    }
    
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    
    return text


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
        with open(json_path, 'r', encoding='utf-8') as f:
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
            'state_facility_id': permit_details.get('stateFacilityID'),
            
            # Additional permit details
            'initial_construction_commenced_notification_required': permit_details.get('initialConstructionCommencedNotificationRequired'),
            'construction_commenced_notification_window_days': permit_details.get('constructionCommencedNotificationWindowDays'),
            'initial_startup_notification_required': permit_details.get('initialStartupNotificationRequired'),
            'startup_notification_window_days': permit_details.get('startupNotificationWindowDays'),
            'permit_copy_onsite_required': permit_details.get('permitCopyOnsiteRequired'),
            'right_of_entry_clause': permit_details.get('rightOfEntryClause'),
            
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
            
            # Convert allowedOperatingModes - handle both string and array
            allowed_modes = gen.get('allowedOperatingModes')
            if isinstance(allowed_modes, list):
                allowed_modes_str = '; '.join(allowed_modes) if allowed_modes else None
            elif isinstance(allowed_modes, str):
                allowed_modes_str = allowed_modes
            else:
                allowed_modes_str = None
            
            record.update({
                # Generator identification
                'num_generators': gen.get('numGenerators'),
                'generator_ref': gen.get('referenceNumber'),
                'included_in_permit_project': gen.get('includedInPermitProject'),
                'original_permit_date': gen.get('originalPermitDate'),
                'equipment_facility_id': gen.get('equipmentFacilityID'),
                'make': gen.get('make'),
                'model': gen.get('model'),
                
                # Capacity
                'rated_capacity_bhp': gen.get('ratedCapacityBHP'),
                'rated_capacity_hp': gen.get('ratedCapacityHP'),
                'rated_capacity_kw': gen.get('ratedCapacityKW'),
                'rated_capacity_mmbtu_per_hr': gen.get('ratedCapacityMMBtuPerHr'),
                
                # Fuel - primary, secondary, other
                'primary_fuel_type': gen.get('primaryFuelType'),
                'secondary_fuel_type': gen.get('secondaryFuelType'),
                'other_fuels': gen.get('otherFuels'),
                'fuel_grade': gen.get('fuelGrade'),
                'fuel_specification': gen.get('fuelSpecification'),
                
                # Fuel - sulfur content and certification
                'fuel_sulfur_content_pct': gen.get('fuelSulfurContentPct'),
                'fuel_certification_required': gen.get('fuelCertificationRequired'),
                'fuel_change_permit_trigger': gen.get('fuelChangePermitTrigger'),
                
                # Fuel - throughput per unit
                'fuel_throughput_per_unit_limit': gen.get('fuelThroughputPerUnitLimit'),
                'fuel_throughput_per_unit_scope': gen.get('fuelThroughputPerUnitScope'),
                'fuel_throughput_per_unit_group_ref': gen.get('fuelThroughputPerUnitGroupRef'),
                
                # Fuel - throughput combined
                'fuel_throughput_combined_limit': gen.get('fuelThroughputCombinedLimit'),
                'fuel_throughput_combined_group_ref': gen.get('fuelThroughputCombinedGroupRef'),
                
                # Control technology
                'control_technology': gen.get('controlTechnology'),
                
                # Operating hours - per unit
                'operating_hours_per_unit_limit': gen.get('operatingHoursPerUnitLimit'),
                'operating_hours_per_unit_rolling_window': gen.get('operatingHoursPerUnitRollingWindow'),
                
                # Operating hours - combined
                'operating_hours_combined_limit': gen.get('operatingHoursCombinedLimit'),
                'operating_hours_combined_group_ref': gen.get('operatingHoursCombinedGroupRef'),
                'operating_hours_combined_rolling_window': gen.get('operatingHoursCombinedRollingWindow'),
                
                # Operating parameters
                'allowed_operating_modes': allowed_modes_str,
                'opacity_limit_percent': gen.get('opacityLimitPercent'),
                
                # Monitoring requirements
                'hour_meter_required': gen.get('hourMeterRequired'),
                'observation_frequency': gen.get('observationFrequency'),
                'recordkeeping_window_years': gen.get('recordkeepingWindowYears'),
                
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
        
        # Clean all text fields to ensure proper ASCII/UTF-8 compatibility
        for col in df.columns:
            if df[col].dtype == 'object':  # Only clean text columns
                df[col] = df[col].apply(clean_text)
        
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
            # Use utf-8-sig to add BOM for Excel compatibility, or utf-8 for clean UTF-8
            # Using utf-8 with errors='replace' ensures problematic characters are handled
            df.to_csv(output_path, index=False, encoding='utf-8', errors='replace')
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

    def save_styled_excel(self, df: pd.DataFrame, output_path: Path):
        """
        Save DataFrame to Excel with professional styling.
        
        Features:
        - Alternating row colors for readability
        - Frozen header row
        - Auto-adjusted column widths
        - Bold header with colored background
        - Proper text wrapping
        - Borders for clean appearance
        
        Args:
            df: DataFrame to save
            output_path: Path to save Excel file
        """
        # Save DataFrame to Excel first
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(output_path, index=False, engine='openpyxl')
        
        # Load workbook for styling
        wb = load_workbook(output_path)
        ws = wb.active
        
        # Define professional color scheme
        header_fill = PatternFill(
            start_color="1F4E78", end_color="1F4E78", fill_type="solid"
        )
        header_font = Font(bold=True, color="FFFFFF", size=11)
        
        alt_row_light = PatternFill(
            start_color="FFFFFF", end_color="FFFFFF", fill_type="solid"
        )
        alt_row_dark = PatternFill(
            start_color="F2F2F2", end_color="F2F2F2", fill_type="solid"
        )
        
        # Border styles
        thin_border = Border(
            left=Side(style="thin", color="D3D3D3"),
            right=Side(style="thin", color="D3D3D3"),
            top=Side(style="thin", color="D3D3D3"),
            bottom=Side(style="thin", color="D3D3D3"),
        )
        
        header_border = Border(
            left=Side(style="thin", color="FFFFFF"),
            right=Side(style="thin", color="FFFFFF"),
            top=Side(style="medium", color="1F4E78"),
            bottom=Side(style="medium", color="1F4E78"),
        )
        
                # Alignment - compact without text wrapping
        header_align = Alignment(
            horizontal="center", vertical="center", wrap_text=False
        )
        cell_align = Alignment(
            horizontal="left", vertical="center", wrap_text=False
        )
        
        # Style header row
        for col_num in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align
            cell.border = header_border
        
        # Set header row height
        ws.row_dimensions[1].height = 30
        
        # Style data rows with alternating colors
        for row_num in range(2, len(df) + 2):
            use_alt_color = (row_num - 2) % 2 == 1  # Every other row
            
            # Set compact row height
            ws.row_dimensions[row_num].height = 18
            
            for col_num in range(1, len(df.columns) + 1):
                cell = ws.cell(row=row_num, column=col_num)
                cell.fill = alt_row_dark if use_alt_color else alt_row_light
                cell.alignment = cell_align
                cell.border = thin_border
        
        # Set compact column widths based on column name patterns
        for col_num, column in enumerate(df.columns, 1):
            col_letter = get_column_letter(col_num)
            column_name = str(column).lower()
            
            # Define compact widths based on column type
            if 'date' in column_name or 'ref' in column_name or 'id' in column_name:
                width = 12
            elif 'state' in column_name or 'county' in column_name:
                width = 14
            elif 'name' in column_name or 'address' in column_name:
                width = 25
            elif 'notes' in column_name or 'specification' in column_name:
                width = 30
            elif any(x in column_name for x in ['limit', 'capacity', 'hours', 'percent', 'pct']):
                width = 15
            else:
                width = 18
            
            ws.column_dimensions[col_letter].width = width
        
        # Freeze header row
        ws.freeze_panes = "A2"
        
        # Save styled workbook
        wb.save(output_path)
        logger.info(f"✓ Applied professional styling to Excel file")
