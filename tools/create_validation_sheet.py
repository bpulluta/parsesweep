#!/usr/bin/env python3
"""
Create a validation comparison spreadsheet with:
- Each permit as a separate sheet
- Rows = fields to validate
- Columns: Field Name | Ground Truth | AQ Toolkit | LlamaExtract | Decision Tree | Notes
- Color-coded sections and prepopulated ground truth
"""

import json
import pandas as pd
from pathlib import Path
from typing import Any, Optional
import sys
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

WORKSPACE_ROOT = Path("/Users/bpulluta/backupgensprint")
VALIDATION_DIR = WORKSPACE_ROOT / "validation"
EXISTING_GT_FILE = VALIDATION_DIR / "validation_ground_truth_CS.xlsx"

AQTOOLKIT_DIR = VALIDATION_DIR / "aqtoolkit"
LLAMAEXTRACT_DIR = VALIDATION_DIR / "llamaextract"
DECISIONTREE_DIR = VALIDATION_DIR / "decisiontree"

# Schema fields (excluding emissions)
PERMIT_FIELDS = [
    ("Permit Number", "permitDetails.permitNumber"),
    ("Permit Issuance Date", "permitDetails.permitIssuanceDate"),
    ("Permit Expiration Date", "permitDetails.permitExpirationDate"),
    ("Facility Name", "permitDetails.facilityName"),
    ("Facility Address", "permitDetails.facilityAddress"),
    ("Facility County", "permitDetails.facilityCounty"),
    ("Facility State", "permitDetails.facilityState"),
    ("Construction Notification Required", "permitDetails.initialConstructionCommencedNotificationRequired"),
    ("Construction Notification Window (Days)", "permitDetails.constructionCommencedNotificationWindowDays"),
    ("Startup Notification Required", "permitDetails.initialStartupNotificationRequired"),
    ("Startup Notification Window (Days)", "permitDetails.startupNotificationWindowDays"),
    ("Permit Copy Onsite Required", "permitDetails.permitCopyOnsiteRequired"),
    ("Right of Entry Clause", "permitDetails.rightOfEntryClause"),
]

GENERATOR_FIELDS = [
    ("Reference Number", "referenceNumber"),
    ("Number of Generators", "numGenerators"),
    ("Included in Project", "includedInPermitProject"),
    ("Make", "make"),
    ("Model", "model"),
    ("Rated Capacity (kW)", "ratedCapacityKW"),
    ("Rated Capacity (BHP)", "ratedCapacityBHP"),
    ("Rated Capacity (HP)", "ratedCapacityHP"),
    ("Maximum Capacity (BHP)", "maximumCapacityBHP"),
    ("Maximum Capacity (kW)", "maximumCapacityKW"),
    ("Primary Fuel Type", "primaryFuelType"),
    ("Secondary Fuel Type", "secondaryFuelType"),
    ("Other Fuels", "otherFuels"),
    ("Fuel Grade", "fuelGrade"),
    ("Fuel Specification", "fuelSpecification"),
    ("Fuel Cert: Sulfur Content Required", "fuelCertificationFields.sulfurContentRequired"),
    ("Fuel Change Permit Trigger", "fuelChangePermitTrigger"),
    ("Fuel Throughput Per-Unit Limit", "fuelThroughputPerUnitLimit"),
    ("Fuel Throughput Per-Unit Scope", "fuelThroughputPerUnitScope"),
    ("Fuel Throughput Per-Unit Group Ref", "fuelThroughputPerUnitGroupRef"),
    ("Fuel Throughput Combined Limit", "fuelThroughputCombinedLimit"),
    ("Fuel Throughput Combined Group Ref", "fuelThroughputCombinedGroupRef"),
    ("Control Technology", "controlTechnology"),
    ("Operating Hours Per-Unit Limit", "operatingHoursPerUnitLimit"),
    ("Operating Hours Per-Unit Rolling Window", "operatingHoursPerUnitRollingWindow"),
    ("Operating Hours Combined Limit", "operatingHoursCombinedLimit"),
    ("Operating Hours Combined Group Ref", "operatingHoursCombinedGroupRef"),
    ("Operating Hours Combined Rolling Window", "operatingHoursCombinedRollingWindow"),
    ("Allowed Operating Modes", "allowedOperatingModes"),
    ("Opacity Limit (%)", "opacityLimitPercent"),
    ("Hour Meter Required", "hourMeterRequired"),
    ("Observation Frequency", "observationFrequency"),
    ("Recordkeeping Window (Years)", "recordkeepingWindowYears"),
    ("Operation Reason Log Required", "operationReasonLogRequired"),
    ("Manufacturer O&M Required", "manufacturerOandMRequired"),
    ("Maintenance Training Records Required", "maintenanceTrainingRecordsRequired"),
    ("NSPS Subpart IIII", "nspsSubpartIIII"),
    ("MACT Subpart ZZZZ", "mactSubpartZZZZ"),
]


def load_json_file(file_path: Path) -> Optional[dict]:
    """Load JSON file."""
    try:
        return json.loads(file_path.read_text())
    except Exception as e:
        print(f"Error loading {file_path}: {e}")
        return None


def find_permit_file(base_dir: Path, permit_number: str, state: str) -> Optional[Path]:
    """Find JSON file for permit."""
    state_dir = base_dir / state
    if not state_dir.exists():
        return None
    
    for pattern in [f"{permit_number}_DC_Permit.json", f"{permit_number}.json", f"llama-extract-{permit_number}_DC_Permit_*.json"]:
        files = list(state_dir.glob(pattern))
        if files:
            return files[0]
    return None


def get_nested_value(data: dict, path: str) -> Any:
    """Get value from nested dict using dot notation."""
    keys = path.split(".")
    value = data
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def format_value(value: Any, field_name: str = ""):
    """Format value for display, preserving numeric types for Excel."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        # Preserve numeric types for Excel
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2)
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return str(value)


def load_ground_truth_data(existing_file: Path) -> dict:
    """Load existing ground truth data."""
    ground_truth = {}
    
    if not existing_file.exists():
        return ground_truth
    
    try:
        xls = pd.ExcelFile(existing_file)
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(existing_file, sheet_name=sheet_name)
            permit_key = sheet_name.replace("#", "_")
            field_mapping = {}
            
            if "Extraction Parameter" in df.columns and "Ground Truth" in df.columns:
                for _, row in df.iterrows():
                    field_name = row.get("Extraction Parameter")
                    gt_val = row.get("Ground Truth")
                    if pd.notna(field_name) and field_name:
                        field_mapping[str(field_name).strip()] = gt_val
            
            ground_truth[permit_key] = field_mapping
            print(f"  Loaded {len(field_mapping)} ground truth values from {sheet_name}")
    
    except Exception as e:
        print(f"  Error loading ground truth: {e}")
    
    return ground_truth


def match_ground_truth(field_name: str, gt_mapping: dict) -> Any:
    """Match field name to ground truth value with fuzzy matching."""
    # Direct match
    if field_name in gt_mapping:
        return gt_mapping[field_name]
    
    # Normalize field name for fuzzy matching
    normalized_field = field_name.lower().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    
    # Try various common variations
    for key, value in gt_mapping.items():
        normalized_key = str(key).lower().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
        
        # Exact normalized match
        if normalized_field == normalized_key:
            return value
        
        # Check for common abbreviation patterns
        # "Rated Capacity (HP)" -> "Rated Capacity hp" or "RatedCapacityhp"
        # "Rated Capacity (kW)" -> "Rated Capacity kW" or "RatedCapacitykw"
        if normalized_field in normalized_key or normalized_key in normalized_field:
            # Additional checks to ensure it's a good match
            field_tokens = set(field_name.lower().split())
            key_tokens = set(str(key).lower().split())
            
            # If most tokens match, it's a good match
            if len(field_tokens & key_tokens) >= min(len(field_tokens), len(key_tokens)) - 1:
                return value
    
    return None


def create_permit_sheet(permit_number: str, state: str,
                       aqt_data: Optional[dict], llama_data: Optional[dict], 
                       dtree_data: Optional[dict], gt_mapping: dict) -> pd.DataFrame:
    """Create comparison sheet for one permit - NO EMPTY ROWS."""
    
    rows = []

    # Permit details section header
    rows.append({"Field Name": "PERMIT DETAILS", "Ground Truth": "",
                 "AQ Toolkit": "", "LlamaExtract": "", "Decision Tree": "",
                 "Notes": "", "_section": "permit_section"})
    
    # Permit fields - NO EMPTY ROWS
    for display_name, json_path in PERMIT_FIELDS:
        aqt_val = get_nested_value(aqt_data.get("data", {}) if aqt_data else {}, json_path)
        llama_val = get_nested_value(llama_data.get("data", {}) if llama_data else {}, json_path)
        dtree_val = get_nested_value(dtree_data.get("data", {}) if dtree_data else {}, json_path)
        gt_val = match_ground_truth(display_name, gt_mapping)
        
        rows.append({
            "Field Name": display_name,
            "Ground Truth": format_value(gt_val, display_name),
            "AQ Toolkit": format_value(aqt_val, display_name),
            "LlamaExtract": format_value(llama_val, display_name),
            "Decision Tree": format_value(dtree_val, display_name),
            "Notes": "",
            "_section": "permit_data"
        })
    
    # Generator sets - NO EMPTY ROWS between sets
    aqt_gens = aqt_data.get("data", {}).get("generatorSets", []) if aqt_data else []
    llama_gens = llama_data.get("data", {}).get("generatorSets", []) if llama_data else []
    dtree_gens = dtree_data.get("data", {}).get("generatorSets", []) if dtree_data else []
    max_gens = max(len(aqt_gens), len(llama_gens), len(dtree_gens))
    
    for i in range(max_gens):
        # Generator section header
        rows.append({"Field Name": f"GENERATOR SET {i + 1}",
                     "Ground Truth": "", "AQ Toolkit": "", "LlamaExtract": "",
                     "Decision Tree": "", "Notes": "", "_section": "generator_section"})
        
        aqt_gen = aqt_gens[i] if i < len(aqt_gens) else {}
        llama_gen = llama_gens[i] if i < len(llama_gens) else {}
        dtree_gen = dtree_gens[i] if i < len(dtree_gens) else {}
        
        # Generator fields - NO EMPTY ROWS
        for display_name, field_key in GENERATOR_FIELDS:
            gt_val = match_ground_truth(display_name, gt_mapping)
            
            # Handle nested field keys (e.g., "fuelCertificationFields.supplierNameRequired")
            aqt_val = get_nested_value(aqt_gen, field_key) if aqt_gen else None
            llama_val = get_nested_value(llama_gen, field_key) if llama_gen else None
            dtree_val = get_nested_value(dtree_gen, field_key) if dtree_gen else None
            
            rows.append({
                "Field Name": display_name,
                "Ground Truth": format_value(gt_val, display_name),
                "AQ Toolkit": format_value(aqt_val, display_name),
                "LlamaExtract": format_value(llama_val, display_name),
                "Decision Tree": format_value(dtree_val, display_name),
                "Notes": "",
                "_section": "generator_data"
            })
    
    return pd.DataFrame(rows)


def apply_styling(worksheet, df):
    """Apply color coding, alternating rows, and formatting to worksheet."""
    # Define styles
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=12)
    
    # Section headers - different colors for permit vs generator
    permit_section_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    generator_section_fill = PatternFill(start_color="70AD47", end_color="70AD47", fill_type="solid")
    section_font = Font(bold=True, color="FFFFFF", size=11)
    
    # Field name column
    field_name_fill = PatternFill(start_color="E7E6E6", end_color="E7E6E6", fill_type="solid")
    field_name_font = Font(bold=True, size=10)
    
    # Ground truth column
    gt_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    
    # Alternating row colors for data
    alt_row_light = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    alt_row_dark = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    
    # Borders
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin")
    )
    
    thick_border = Border(
        left=Side(style="medium"), right=Side(style="medium"),
        top=Side(style="medium"), bottom=Side(style="medium")
    )
    
    section_border = Border(
        left=Side(style="medium"), right=Side(style="medium"),
        top=Side(style="thick"), bottom=Side(style="medium")
    )
    
    center_align = Alignment(horizontal="center", vertical="top", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="top", wrap_text=True)
    
    # Track section context and data row counter for alternating colors
    current_section_type = None
    data_row_counter = 0
    
    # Apply column headers (row 1)
    for col_num in range(1, 7):
        cell = worksheet.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thick_border
    
    # Apply row styles based on section type
    for row_num in range(2, len(df) + 2):
        section_type = df.iloc[row_num - 2].get("_section", "data")
        field_name = df.iloc[row_num - 2].get("Field Name", "")
        
        # Determine section context
        if "PERMIT DETAILS" in str(field_name):
            current_section_type = "permit"
            data_row_counter = 0  # Reset counter for new section
        elif "GENERATOR SET" in str(field_name):
            current_section_type = "generator"
            data_row_counter = 0  # Reset counter for new section
        
        # Determine if this is a data row (for alternating colors)
        is_data_row = section_type in ["permit_data", "generator_data"]
        if is_data_row:
            data_row_counter += 1
            use_alt_color = (data_row_counter % 2 == 0)
        
        for col_num in range(1, 7):
            cell = worksheet.cell(row=row_num, column=col_num)
            cell.alignment = left_align
            cell.border = thin_border
            
            # Apply styling based on section type
            if section_type == "header":
                cell.fill = header_fill
                cell.font = header_font
                cell.border = thick_border
                cell.alignment = center_align
                
            elif section_type == "permit_section" or section_type == "generator_section":
                # Section headers
                if current_section_type == "permit":
                    cell.fill = permit_section_fill
                else:
                    cell.fill = generator_section_fill
                cell.font = section_font
                cell.border = section_border
                
            elif is_data_row:
                # Data rows with alternating colors
                if col_num == 1:
                    # Field Name column - always gray
                    cell.fill = field_name_fill
                    cell.font = field_name_font
                elif col_num == 2:
                    # Ground Truth column - always yellow
                    cell.fill = gt_fill
                else:
                    # Data columns - alternating white/light gray
                    cell.fill = alt_row_dark if use_alt_color else alt_row_light
    
    # Set column widths
    worksheet.column_dimensions["A"].width = 40
    worksheet.column_dimensions["B"].width = 35
    worksheet.column_dimensions["C"].width = 35
    worksheet.column_dimensions["D"].width = 35
    worksheet.column_dimensions["E"].width = 35
    worksheet.column_dimensions["F"].width = 45
    
    # Freeze the header row
    worksheet.freeze_panes = "A2"


def main():
    print("Creating styled validation comparison spreadsheet...")
    
    # Load ground truth
    print("\nLoading ground truth data...")
    ground_truth = load_ground_truth_data(EXISTING_GT_FILE)
    
    # Get permits
    permits = []
    for state_dir in AQTOOLKIT_DIR.iterdir():
        if state_dir.is_dir() and state_dir.name not in [".DS_Store", ".gitkeep"]:
            for json_file in state_dir.glob("*.json"):
                if json_file.stem != "validation_consolidated":
                    permit_number = json_file.stem.replace("_DC_Permit", "")
                    permits.append({
                        "permit_number": permit_number,
                        "state": state_dir.name,
                        "sheet_name": f"{state_dir.name}_{permit_number}"[:31]
                    })
    
    permits = sorted(permits, key=lambda x: (x["state"], x["permit_number"]))
    print(f"Found {len(permits)} permits")
    
    # Create workbook
    output_path = VALIDATION_DIR / "validation_comparison_styled.xlsx"
    
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for permit_info in permits:
            permit_number = permit_info["permit_number"]
            state = permit_info["state"]
            sheet_name = permit_info["sheet_name"]
            
            print(f"\nProcessing {state} - {permit_number}")
            
            # Load extraction data
            aqt_file = find_permit_file(AQTOOLKIT_DIR, permit_number, state)
            llama_file = find_permit_file(LLAMAEXTRACT_DIR, permit_number, state)
            dtree_file = find_permit_file(DECISIONTREE_DIR, permit_number, state)
            
            aqt_data = load_json_file(aqt_file) if aqt_file else None
            llama_data = load_json_file(llama_file) if llama_file else None
            dtree_data = load_json_file(dtree_file) if dtree_file else None
            
            # Get ground truth for this permit
            # Try multiple key formats to match the ground truth file
            possible_keys = [
                f"{state}_{permit_number}",  # e.g., "Virginia_11541"
                f"{state}#{permit_number}",   # e.g., "Virginia#11541"
            ]
            
            # Also try state abbreviations (VA, IL, KY, MI)
            state_abbrev_map = {
                "Virginia": "VA",
                "Illinois": "IL",
                "Kentucky": "KY",
                "Michigan": "MI"
            }
            if state in state_abbrev_map:
                abbrev = state_abbrev_map[state]
                possible_keys.extend([
                    f"{abbrev}_{permit_number}",  # e.g., "VA_11541"
                    f"{abbrev}#{permit_number}",   # e.g., "VA#11541"
                ])
            
            gt_mapping = {}
            for gt_key in possible_keys:
                if gt_key in ground_truth:
                    gt_mapping = ground_truth[gt_key]
                    print(f"  Found ground truth with key: {gt_key} ({len(gt_mapping)} values)")
                    break
            
            # Create sheet
            df = create_permit_sheet(permit_number, state, aqt_data, llama_data, 
                                    dtree_data, gt_mapping)
            
            # Write to Excel (excluding _section column)
            df_display = df.drop(columns=["_section"])
            df_display.to_excel(writer, sheet_name=sheet_name, index=False)
            
            # Apply styling
            worksheet = writer.sheets[sheet_name]
            apply_styling(worksheet, df)
    
    print(f"\n✅ Created: {output_path}")
    print("\nStructure:")
    print("  - Each sheet = one permit")
    print("  - Rows = fields to validate")
    print("  - Color coded: Headers (dark blue), Sections (blue), Fields (gray), Ground Truth (yellow)")
    print("\nNext: Fill in any missing Ground Truth values and compare!")


if __name__ == "__main__":
    main()
