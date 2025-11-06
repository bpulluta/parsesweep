"""Data consolidation - convert extracted JSON to analysis-ready datasets."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple
import unicodedata

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


# Validation thresholds for flagging unusual values
LARGE_CAPACITY_KW = 4000  # Flag generators >4 MW
SMALL_CAPACITY_KW = 10    # Flag generators <10 kW
LARGE_GENERATOR_COUNT = 100  # Flag if numGenerators > 10
HIGH_CONFIDENCE_THRESHOLD = 0.80  # QA/QC high confidence
LOW_CONFIDENCE_THRESHOLD = 0.60   # QA/QC flag for review


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
        'rated_capacity_bhp': bhp,
        'rated_capacity_hp': hp,
        'rated_capacity_kw': kw,
        'rated_capacity_mmbtu_per_hr': mmbtu_hr,
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
            result['rated_capacity_bhp'] = power_hp
            result['calculated_bhp'] = True
        if hp_val is None:
            result['rated_capacity_hp'] = power_hp
            result['calculated_hp'] = True
            
        # Calculate kW ONLY if missing: kW = BHP * 0.746 * Eff_gen
        if kw_val is None:
            result['rated_capacity_kw'] = round(power_hp * 0.746 * eff_gen, 2)
            result['calculated_kw'] = True
        else:
            result['rated_capacity_kw'] = kw  # Preserve original value
        
        # Calculate MMBTU/hr ONLY if missing: MMBTU/hr = (BHP * 0.746 * Eff_gen * 3412) / (Eff_fuel * 1,000,000)
        if mmbtu_val is None:
            result['rated_capacity_mmbtu_per_hr'] = round(
                (power_hp * 0.746 * eff_gen * 3412) / (eff_fuel * 1_000_000), 3
            )
            result['calculated_mmbtu_per_hr'] = True
        else:
            result['rated_capacity_mmbtu_per_hr'] = mmbtu_hr  # Preserve original value
    
    # Case 2: Have kW, calculate missing BHP/HP and MMBTU/hr
    elif kw_val is not None:
        # Calculate BHP/HP from kW: BHP = kW / (0.746 * Eff_gen)
        calculated_hp = round(kw_val / (0.746 * eff_gen), 2)
        if bhp_val is None:
            result['rated_capacity_bhp'] = calculated_hp
            result['calculated_bhp'] = True
        else:
            result['rated_capacity_bhp'] = bhp  # Preserve original value
        if hp_val is None:
            result['rated_capacity_hp'] = calculated_hp
            result['calculated_hp'] = True
        else:
            result['rated_capacity_hp'] = hp  # Preserve original value
        
        # Calculate MMBTU/hr ONLY if missing: MMBTU/hr = (kW * 3412) / (Eff_fuel * 1,000,000)
        if mmbtu_val is None:
            result['rated_capacity_mmbtu_per_hr'] = round(
                (kw_val * 3412) / (eff_fuel * 1_000_000), 3
            )
            result['calculated_mmbtu_per_hr'] = True
        else:
            result['rated_capacity_mmbtu_per_hr'] = mmbtu_hr  # Preserve original value
    
    # Case 3: Have MMBTU/hr, calculate missing BHP/HP and kW
    elif mmbtu_val is not None:
        # Calculate BHP/HP from MMBTU/hr: BHP = (MMBTU/hr * Eff_fuel * 1,000,000) / (0.746 * Eff_gen * 3412)
        calculated_hp = round(
            (mmbtu_val * eff_fuel * 1_000_000) / (0.746 * eff_gen * 3412), 2
        )
        if bhp_val is None:
            result['rated_capacity_bhp'] = calculated_hp
            result['calculated_bhp'] = True
        else:
            result['rated_capacity_bhp'] = bhp  # Preserve original value
        if hp_val is None:
            result['rated_capacity_hp'] = calculated_hp
            result['calculated_hp'] = True
        else:
            result['rated_capacity_hp'] = hp  # Preserve original value
        
        # Calculate kW ONLY if missing: kW = (MMBTU/hr * Eff_fuel * 1,000,000) / 3412
        if kw_val is None:
            result['rated_capacity_kw'] = round(
                (mmbtu_val * eff_fuel * 1_000_000) / 3412, 2
            )
            result['calculated_kw'] = True
        else:
            result['rated_capacity_kw'] = kw  # Preserve original value
    
    return result


def assess_data_quality(record: dict) -> Tuple[str, str]:
    """
    Assess data quality for a generator record and return flag text and severity.
    Focused ONLY on capacity - that's what matters most.
    
    Returns:
        Tuple of (flag_text, severity_level)
        severity_level: 'critical', 'warning', or 'ok'
    """
    issues = []
    severity = 'ok'
    
    # Check for missing capacity (only critical issue we care about)
    has_capacity = any([
        record.get('rated_capacity_kw'),
        record.get('rated_capacity_hp'),
        record.get('rated_capacity_bhp'),
        record.get('rated_capacity_mmbtu_per_hr')
    ])
    
    if not has_capacity:
        issues.append("No Capacity")
        severity = 'critical'
        # Return immediately - no capacity is the only critical issue
        return f"⚠️ {', '.join(issues)}", severity
    
    # Check for unusual capacity values
    kw = record.get('rated_capacity_kw')
    if kw:
        try:
            kw_val = float(kw)
            if kw_val > LARGE_CAPACITY_KW:
                issues.append(f"Large ({kw_val:.0f} kW)")
                severity = 'warning'
            elif kw_val < SMALL_CAPACITY_KW:
                issues.append(f"Small ({kw_val:.0f} kW)")
                severity = 'warning'
        except (ValueError, TypeError):
            pass
    
    # Check generator count
    num_gens = record.get('num_generators')
    if num_gens:
        try:
            num_val = int(num_gens)
            if num_val > LARGE_GENERATOR_COUNT:
                issues.append(f"Count: {num_val}")
                severity = 'warning' if severity == 'ok' else severity
        except (ValueError, TypeError):
            pass
    
    # Generate concise flag text
    if not issues:
        return "✓", "ok"
    elif severity == 'critical':
        return "⚠️ " + ", ".join(issues), severity
    else:  # warning
        return "⚡ " + ", ".join(issues), severity


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
            
            # Calculate missing capacity values
            capacity_values = calculate_missing_capacity(
                bhp=gen.get('ratedCapacityBHP'),
                hp=gen.get('ratedCapacityHP'),
                kw=gen.get('ratedCapacityKW'),
                mmbtu_hr=gen.get('ratedCapacityMMBtuPerHr')
            )
            
            record.update({
                # Generator identification
                'generator_ref': gen.get('referenceNumber'),
                'included_in_permit_project': gen.get('includedInPermitProject'),
                'original_permit_date': gen.get('originalPermitDate'),
                'equipment_facility_id': gen.get('equipmentFacilityID'),
                'num_generators': gen.get('numGenerators'),
                'make': gen.get('make'),
                'model': gen.get('model'),
                
                # Capacity (with calculated values)
                'rated_capacity_bhp': capacity_values['rated_capacity_bhp'],
                'rated_capacity_hp': capacity_values['rated_capacity_hp'],
                'rated_capacity_kw': capacity_values['rated_capacity_kw'],
                'rated_capacity_mmbtu_per_hr': capacity_values['rated_capacity_mmbtu_per_hr'],
                
                # Flags for calculated capacity values (for Excel styling)
                'calculated_bhp': capacity_values['calculated_bhp'],
                'calculated_hp': capacity_values['calculated_hp'],
                'calculated_kw': capacity_values['calculated_kw'],
                'calculated_mmbtu_per_hr': capacity_values['calculated_mmbtu_per_hr'],
                
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
            
            # Add data quality assessment
            flag_text, severity = assess_data_quality(record)
            record['data_quality_flag'] = flag_text
            record['data_quality_severity'] = severity
            
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
        Save DataFrame to Excel with professional styling and enhanced validation markers.
        
        Features:
        - Alternating row colors for readability
        - Color-coded data quality markers (yellow=missing, orange=warning, blue=calculated, green=validated)
        - Frozen header row
        - Auto-adjusted column widths
        - Bold header with colored background
        - Dedicated Instructions & Legend sheet
        - Summary Statistics sheet
        
        Args:
            df: DataFrame to save
            output_path: Path to save Excel file
        """
        # Identify internal columns to exclude from Excel output
        internal_cols = ['calculated_bhp', 'calculated_hp', 'calculated_kw', 'calculated_mmbtu_per_hr', 'data_quality_severity']
        
        # Create a copy for Excel without the internal columns
        df_excel = df.drop(columns=[col for col in internal_cols if col in df.columns], errors='ignore')
        
        # Save DataFrame to Excel first
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            df_excel.to_excel(writer, sheet_name='Data', index=False)
            
            # Add Instructions & Legend sheet
            self._create_instructions_sheet(writer)
            
            # Add Summary Statistics sheet
            self._create_summary_sheet(writer, df)
        
        # Load workbook for styling
        wb = load_workbook(output_path)
        ws = wb['Data']
        
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
        
        # Enhanced color scheme for data quality
        # Blue - Calculated/derived values (info)
        calculated_fill = PatternFill(
            start_color="D6EAF8", end_color="D6EAF8", fill_type="solid"
        )
        calculated_fill_alt = PatternFill(
            start_color="C4DEF6", end_color="C4DEF6", fill_type="solid"
        )
        
        # Yellow - Missing critical data (critical)
        missing_fill = PatternFill(
            start_color="FFF4CC", end_color="FFF4CC", fill_type="solid"
        )
        missing_fill_alt = PatternFill(
            start_color="FFE699", end_color="FFE699", fill_type="solid"
        )
        
        # Orange - Validation warning (unusual values)
        warning_fill = PatternFill(
            start_color="FFE6CC", end_color="FFE6CC", fill_type="solid"
        )
        warning_fill_alt = PatternFill(
            start_color="FFCC99", end_color="FFCC99", fill_type="solid"
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
        for col_num in range(1, len(df_excel.columns) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align
            cell.border = header_border
        
        # Set header row height
        ws.row_dimensions[1].height = 30
        
        # Create mapping from Excel columns to original DataFrame columns
        capacity_cols_map = {}
        if 'rated_capacity_bhp' in df_excel.columns:
            capacity_cols_map['rated_capacity_bhp'] = df_excel.columns.get_loc('rated_capacity_bhp') + 1
        if 'rated_capacity_hp' in df_excel.columns:
            capacity_cols_map['rated_capacity_hp'] = df_excel.columns.get_loc('rated_capacity_hp') + 1
        if 'rated_capacity_kw' in df_excel.columns:
            capacity_cols_map['rated_capacity_kw'] = df_excel.columns.get_loc('rated_capacity_kw') + 1
        if 'rated_capacity_mmbtu_per_hr' in df_excel.columns:
            capacity_cols_map['rated_capacity_mmbtu_per_hr'] = df_excel.columns.get_loc('rated_capacity_mmbtu_per_hr') + 1
        
        # Style data rows with color coding based on data quality
        for row_num in range(2, len(df) + 2):
            use_alt_color = (row_num - 2) % 2 == 1  # Every other row
            df_row_idx = row_num - 2  # Index into original DataFrame
            
            # Get data quality severity for this row
            severity = df.iloc[df_row_idx].get('data_quality_severity', 'ok')
            
            # Get data quality severity for this row
            severity = df.iloc[df_row_idx].get('data_quality_severity', 'ok')
            
            # Choose base row color for critical/warning rows ONLY
            if severity == 'critical':
                base_row_fill = missing_fill_alt if use_alt_color else missing_fill
            elif severity == 'warning':
                base_row_fill = warning_fill_alt if use_alt_color else warning_fill
            else:  # 'ok' - use standard alternating colors
                base_row_fill = alt_row_dark if use_alt_color else alt_row_light
            
            # Set compact row height
            ws.row_dimensions[row_num].height = 18
            
            for col_num in range(1, len(df_excel.columns) + 1):
                cell = ws.cell(row=row_num, column=col_num)
                col_name = df_excel.columns[col_num - 1]
                
                # Check if this specific capacity cell was calculated
                is_calculated = False
                if col_name == 'rated_capacity_bhp' and 'calculated_bhp' in df.columns:
                    is_calculated = df.iloc[df_row_idx]['calculated_bhp']
                elif col_name == 'rated_capacity_hp' and 'calculated_hp' in df.columns:
                    is_calculated = df.iloc[df_row_idx]['calculated_hp']
                elif col_name == 'rated_capacity_kw' and 'calculated_kw' in df.columns:
                    is_calculated = df.iloc[df_row_idx]['calculated_kw']
                elif col_name == 'rated_capacity_mmbtu_per_hr' and 'calculated_mmbtu_per_hr' in df.columns:
                    is_calculated = df.iloc[df_row_idx]['calculated_mmbtu_per_hr']
                
                # Apply cell-specific fill: blue for calculated, otherwise use row fill
                if is_calculated:
                    cell.fill = calculated_fill_alt if use_alt_color else calculated_fill
                else:
                    cell.fill = base_row_fill
                
                cell.alignment = cell_align
                cell.border = thin_border
        
        # Set compact column widths based on column name patterns
        for col_num, column in enumerate(df_excel.columns, 1):
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
            elif 'quality' in column_name:
                width = 35
            elif any(x in column_name for x in ['limit', 'capacity', 'hours', 'percent', 'pct']):
                width = 15
            else:
                width = 18
            
            ws.column_dimensions[col_letter].width = width
        
        # Freeze header row
        ws.freeze_panes = "A2"
        
        # Save styled workbook
        wb.save(output_path)
        logger.info("✓ Applied capacity-focused validation styling")
        logger.info("  🟡 Yellow rows = Missing capacity data")
        logger.info("  ⚡ Orange rows = Unusual capacity values (verify)")
        logger.info("  🔵 Blue in capacity cells = Calculated from other units")
        logger.info("  ✓ Check 'data_quality_flag' column for quick status")
        logger.info("  📊 See 'Summary Statistics' and 'Instructions' sheets")
        logger.info("  ⚪ White/Gray = Normal capacity data")
    
    def _create_instructions_sheet(self, writer):
        """
        Create comprehensive instructions and legend sheet.
        
        Args:
            writer: pandas ExcelWriter object
        """
        instructions_data = []
        
        # Title section
        instructions_data.extend([
            ["AIR QUALITY PERMIT TOOLKIT - CONSOLIDATED DATA GUIDE", "", "", ""],
            ["", "", "", ""],
        ])
        
        # Color Legend section
        instructions_data.extend([
            ["COLOR CODING - SIMPLE & CLEAR", "", "", ""],
            ["What You See", "What It Means", "What To Do", ""],
            ["🟡 Yellow ROW", "No capacity data found", "Check source permit - may need manual data entry", ""],
            ["⚡ Orange ROW", "Unusual capacity (verify)", "Large/small capacity or high count - confirm it's correct", ""],
            ["🔵 Blue in capacity CELL", "We calculated this value", "That specific capacity cell was calculated from another unit", ""],
            ["⚪ Gray/White ROW", "Complete & normal capacity", "No action needed", ""],
            ["", "", "", ""],
            ["IMPORTANT: Row colors show data quality.", "", "", ""],
            ["Blue appears ONLY in individual capacity cells that were calculated.", "", "", ""],
            ["", "", "", ""],
        ])
        
        # Capacity Conversion Formulas section
        instructions_data.extend([
            ["CAPACITY CONVERSIONS", "", "", ""],
            ["Blue cells = calculated using these formulas:", "", "", ""],
            ["", "", "", ""],
            ["From/To", "Formula", "Notes", ""],
            ["HP/BHP → kW", "kW = HP x 0.746 x Eff_gen (90%)", "Standard electrical conversion", ""],
            ["kW → HP/BHP", "HP = kW ÷ 0.746 ÷ Eff_gen (90%)", "Reverse conversion", ""],
            ["MMBtu/hr → kW", "kW = (MMBtu/hr x 0.35 x 1M) ÷ 3412", "Thermal efficiency = 35%", ""],
            ["kW → MMBtu/hr", "MMBtu/hr = (kW × 3412) ÷ (Eff_fuel × 1,000,000)", "Reverse thermal conversion", ""],
            ["", "", "", ""],
            ["Efficiency Assumptions:", "", "", ""],
            ["  • Generator Efficiency (Eff_gen) = 0.90 (90%)", "", "", ""],
            ["  • Thermal Efficiency (Eff_fuel) = 0.35 (35%)", "", "", ""],
            ["", "", "", ""],
        ])
        
        # Validation Thresholds section
        instructions_data.extend([
            ["WHAT TRIGGERS COLOR CODING", "", "", ""],
            ["🟡 Yellow = No capacity data found in any field", "", "", ""],
            ["⚡ Orange = Unusual capacity values:", "", "", ""],
            ["  • Very large: > 5,000 kW", "", "", ""],
            ["  • Very small: < 10 kW", "", "", ""],
            ["  • High generator count: > 10 units", "", "", ""],
            ["", "", "", ""],
            ["These are guidance only - verify against source permit.", "", "", ""],
            ["", "", "", ""],
        ])
        
        # Known Limitations section
        instructions_data.extend([
            ["NOTES", "", "", ""],
            ["✓ Calculated capacity uses standard engineering formulas", "", "", ""],
            ["✓ We focus on capacity - make/model are nice-to-have", "", "", ""],
            ["✓ Engineering assumptions: 90% generator efficiency, 35% thermal efficiency", "", "", ""],
            ["", "", "", ""],
        ])
        
        # Quick Reference section
        instructions_data.extend([
            ["QUICK REFERENCE", "", "", ""],
            ["• Each row = one generator", "", "", ""],
            ["• Check yellow/orange rows first", "", "", ""],
            ["• Blue cells = calculated values", "", "", ""],
            ["• See Summary Statistics sheet for totals", "", "", ""],
            ["", "", "", ""],
            ["", "", "", ""],
            [f"Generated by: Air Quality Permit Toolkit v{self._get_version()}", "", "", ""],
        ])
        
        # Write to Excel
        df_instructions = pd.DataFrame(instructions_data)
        df_instructions.to_excel(writer, sheet_name='Instructions & Legend', index=False, header=False)
        
        # Apply styling to instructions sheet
        wb = writer.book
        ws = wb['Instructions & Legend']
        
        # Style title
        ws.merge_cells('A1:D1')
        title_cell = ws['A1']
        title_cell.font = Font(bold=True, size=14, color="1F4E78")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        
        # Style section headers (rows with section titles)
        section_headers = [3, 11, 27, 35, 44, 52]
        for row in section_headers:
            cell = ws.cell(row=row, column=1)
            cell.font = Font(bold=True, size=12, color="1F4E78")
            cell.fill = PatternFill(start_color="E8F4F8", end_color="E8F4F8", fill_type="solid")
        
        # Style color legend examples
        color_examples = {
            6: "FFF4CC",  # Yellow
            7: "FFE6CC",  # Orange
            8: "D6EAF8",  # Blue
            9: "FFFFFF",  # White
        }
        for row, color in color_examples.items():
            ws.cell(row=row, column=1).fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        
        # Set column widths
        ws.column_dimensions['A'].width = 30
        ws.column_dimensions['B'].width = 50
        ws.column_dimensions['C'].width = 40
        ws.column_dimensions['D'].width = 30
    
    def _create_summary_sheet(self, writer, df: pd.DataFrame):
        """
        Create summary statistics sheet with data quality metrics.
        
        Args:
            writer: pandas ExcelWriter object
            df: Original DataFrame with all data
        """
        summary_data = []
        
        # Title
        summary_data.extend([
            ["SUMMARY STATISTICS", "", ""],
            ["", "", ""],
        ])
        
        # Overall counts
        total_records = len(df)
        total_facilities = df['facility_name'].nunique() if 'facility_name' in df.columns else 0
        total_permits = df['permit_number'].nunique() if 'permit_number' in df.columns else 0
        
        summary_data.extend([
            ["OVERALL METRICS", "", ""],
            ["Total Generator Records", total_records, ""],
            ["Unique Facilities", total_facilities, ""],
            ["Unique Permits", total_permits, ""],
            ["", "", ""],
        ])
        
        # Data quality breakdown
        if 'data_quality_severity' in df.columns:
            severity_counts = df['data_quality_severity'].value_counts()
            
            summary_data.extend([
                ["DATA QUALITY BREAKDOWN", "", ""],
                ["Severity Level", "Count", "Percentage"],
                ["🔴 Critical (Missing Data)", severity_counts.get('critical', 0), 
                 f"{severity_counts.get('critical', 0) / total_records * 100:.1f}%"],
                ["⚡ Warning (Unusual Values)", severity_counts.get('warning', 0),
                 f"{severity_counts.get('warning', 0) / total_records * 100:.1f}%"],
                ["✓ OK (No Issues)", severity_counts.get('ok', 0),
                 f"{severity_counts.get('ok', 0) / total_records * 100:.1f}%"],
                ["", "", ""],
            ])
        
        # Missing field analysis - only truly missing fields
        summary_data.extend([
            ["MISSING CRITICAL FIELDS", "", ""],
            ["Field", "Missing Count", "Percentage"],
        ])
        
        # Check for missing capacity (ANY capacity field is OK)
        has_any_capacity = df.apply(lambda row: any([
            pd.notna(row.get('rated_capacity_kw')),
            pd.notna(row.get('rated_capacity_hp')),
            pd.notna(row.get('rated_capacity_bhp')),
            pd.notna(row.get('rated_capacity_mmbtu_per_hr'))
        ]), axis=1)
        missing_capacity = (~has_any_capacity).sum()
        summary_data.append(["Any Capacity Field", missing_capacity, f"{missing_capacity / total_records * 100:.1f}%"])
        
        summary_data.append(["", "", ""])
        
        # Calculated values summary
        calc_cols = ['calculated_bhp', 'calculated_hp', 'calculated_kw', 'calculated_mmbtu_per_hr']
        summary_data.extend([
            ["CALCULATED VALUES SUMMARY", "", ""],
            ["Capacity Type", "Calculated Count", "Percentage"],
        ])
        
        for col in calc_cols:
            if col in df.columns:
                calc_count = df[col].sum()
                pct = calc_count / total_records * 100
                field_name = col.replace('calculated_', '').upper()
                summary_data.append([field_name, calc_count, f"{pct:.1f}%"])
        
        # Write to Excel
        df_summary = pd.DataFrame(summary_data)
        df_summary.to_excel(writer, sheet_name='Summary Statistics', index=False, header=False)
        
        # Apply styling
        wb = writer.book
        ws = wb['Summary Statistics']
        
        # Style title
        ws.merge_cells('A1:C1')
        title_cell = ws['A1']
        title_cell.font = Font(bold=True, size=14, color="1F4E78")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        
        # Style section headers
        section_rows = [3, 9, 18, 27]
        for row in section_rows:
            cell = ws.cell(row=row, column=1)
            cell.font = Font(bold=True, size=11, color="1F4E78")
            cell.fill = PatternFill(start_color="E8F4F8", end_color="E8F4F8", fill_type="solid")
        
        # Set column widths
        ws.column_dimensions['A'].width = 40
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 20
    
    def _get_version(self) -> str:
        """Get toolkit version."""
        return "1.0.0"  # Could read from package metadata

