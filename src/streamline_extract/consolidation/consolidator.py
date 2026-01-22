#!/usr/bin/env python3
"""
Universal Consolidator - Works with ANY schema type.

Automatically detects schema structure and creates clean, consolidated output.
Schema-agnostic design for maximum flexibility.
"""
import json
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


class Consolidator:
    """
    Schema-agnostic consolidator that works with any document extraction schema.
    
    Automatically:
    - Detects schema structure (nested arrays, key fields)
    - Flattens hierarchical data into tables
    - Deduplicates entries with intelligent heuristics
    - Creates clean, readable Excel and CSV output
    """
    
    def __init__(self):
        self.schema_info = None
    
    def consolidate_from_directory(self, json_dir: Path) -> Tuple[pd.DataFrame, Dict]:
        """
        Load all JSON files and consolidate into DataFrame.
        
        Returns:
            Tuple of (DataFrame, schema_info dict)
        """
        json_files = list(json_dir.glob("*.json"))
        if not json_files:
            print(f"No JSON files found in {json_dir}")
            return pd.DataFrame(), {}
        
        # Analyze first file to understand schema structure
        with open(json_files[0]) as f:
            sample_data = json.load(f)
        
        # Handle wrapped extraction results
        if "data" in sample_data and isinstance(sample_data["data"], dict):
            sample_data = sample_data["data"]
        
        # Detect schema type and structure
        self.schema_info = self._detect_schema_structure(sample_data)
        
        print(f"📋 Schema detected: {self.schema_info['type']}")
        print(f"   Main entity: {self.schema_info['main_array_key']}")
        print(f"   Identifier fields: {', '.join(self.schema_info['id_fields'])}")
        
        # Extract data from all files
        rows = []
        for json_file in sorted(json_files):
            with open(json_file) as f:
                data = json.load(f)
            
            # Handle wrapped extraction
            if "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            
            # Extract identifier/context fields
            context = self._extract_context(data, self.schema_info)
            
            # Extract main array items
            main_array = data.get(self.schema_info['main_array_key'], [])
            
            for item in main_array:
                row = {**context, **self._flatten_item(item)}
                rows.append(row)
        
        # Create DataFrame and deduplicate
        df = pd.DataFrame(rows)
        df = self._normalize_units(df)
        df = self._deduplicate_rows(df)
        
        return df, self.schema_info
    
    def _normalize_units(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize unit values for consistency.
        Converts symbols to spelled-out units for data analysis.
        """
        # Check if 'Unit' column exists
        unit_col = None
        for col in df.columns:
            if col.lower() in ['unit', 'units']:
                unit_col = col
                break
        
        if unit_col is None:
            return df
        
        # Unit normalizations
        normalizations = {
            "'": "feet",
            "\u2032": "feet",  # Prime symbol  
            "ft": "feet",
            "ft.": "feet",
            '"': "inches",
            "\u2033": "inches",  # Double prime
            "in": "inches",
            "in.": "inches",
            "dB(A)": "dBA",
            "db(a)": "dBA",
            "DB(A)": "dBA",
        }
        
        # Apply normalizations
        df[unit_col] = df[unit_col].replace(normalizations)
        
        return df
    
    def _detect_schema_structure(self, data: Dict) -> Dict:
        """
        Automatically detect schema structure.
        
        Returns dict with:
        - type: schema type name
        - main_array_key: key containing the main data array
        - id_fields: list of identifier/context field keys
        """
        # Find arrays in the data
        array_keys = [k for k, v in data.items() if isinstance(v, list) and len(v) > 0]
        object_keys = [k for k, v in data.items() if isinstance(v, dict)]
        
        if not array_keys:
            raise ValueError("No arrays found in schema. Unable to consolidate.")
        
        # Determine main array (usually the longest or most specific name)
        main_array_key = array_keys[0]
        if len(array_keys) > 1:
            # Prefer arrays with more specific names
            for key in array_keys:
                if any(word in key.lower() for word in ['requirement', 'plan', 'charge', 'item', 'entry']):
                    main_array_key = key
                    break
        
        # Determine schema type from key names
        schema_type = "Generic Document"
        if 'ordinance' in main_array_key.lower() or 'requirement' in main_array_key.lower():
            schema_type = "Ordinance/Regulation"
        elif 'rate' in main_array_key.lower() or 'tariff' in main_array_key.lower():
            schema_type = "Utility Tariff"
        elif 'plan' in main_array_key.lower():
            schema_type = "Service Plan"
        
        # Find identifier object(s)
        id_fields = []
        for obj_key in object_keys:
            if any(word in obj_key.lower() for word in ['details', 'info', 'jurisdiction', 'utility', 'company', 'metadata']):
                id_fields.append(obj_key)
        
        return {
            'type': schema_type,
            'main_array_key': main_array_key,
            'id_fields': id_fields,
            'object_keys': object_keys
        }
    
    def _extract_context(self, data: Dict, schema_info: Dict) -> Dict:
        """Extract context/identifier fields from the document."""
        context = {}
        
        for id_field_key in schema_info['id_fields']:
            id_obj = data.get(id_field_key, {})
            for k, v in id_obj.items():
                # Convert camelCase to Title Case with spaces
                display_name = ''.join([' ' + c if c.isupper() else c for c in k]).strip().title()
                context[display_name] = v
        
        return context
    
    def _flatten_item(self, item: Dict) -> Dict:
        """Flatten a single item, handling nested structures."""
        flattened = {}
        
        for key, value in item.items():
            # Convert camelCase to Title Case
            display_key = ''.join([' ' + c if c.isupper() else c for c in key]).strip().title()
            
            if isinstance(value, dict):
                # Flatten nested dict
                for nested_key, nested_value in value.items():
                    nested_display = ''.join([' ' + c if c.isupper() else c for c in nested_key]).strip().title()
                    flattened[f"{display_key} - {nested_display}"] = nested_value
            elif isinstance(value, list):
                # Convert list to comma-separated string
                flattened[display_key] = ', '.join(str(v) for v in value)
            else:
                flattened[display_key] = value
        
        return flattened
    
    def _deduplicate_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Intelligently deduplicate rows.
        
        Strategy:
        1. Standard deduplication: exact duplicates based on key columns
        2. Value-based deduplication is disabled to preserve separate requirements
        """
        if df.empty:
            return df
        
        # Add a notes column if it doesn't exist
        if 'Notes' not in df.columns:
            df['Notes'] = ''
        
        # PASS 1: Standard deduplication (exact duplicates)
        df, removed_standard = self._deduplicate_standard(df)
        
        # PASS 2: Value-based deduplication - DISABLED for now to preserve data
        # df, removed_value_based = self._deduplicate_by_value(df)
        removed_value_based = 0
        
        total_removed = removed_standard + removed_value_based
        if total_removed > 0:
            print(f"  ✓ Removed {total_removed} duplicate entries")
        else:
            print("  ✓ No duplicates found")
        
        return df
    
    def _deduplicate_standard(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """Standard deduplication based on key columns."""
        # Identify key columns for deduplication
        # CRITICAL: Must include category/type to distinguish different requirements
        # Example: "Setback from residence" vs "Setback from school" both have value=1320 feet
        # but are DIFFERENT requirements
        key_cols = []
        for col in df.columns:
            col_lower = col.lower()
            # Include: jurisdiction (state/county), category/type, applies_to, value, unit
            # This ensures we only merge truly identical requirements
            if any(keyword in col_lower for keyword in ['state', 'county', 'city', 'jurisdiction', 'category', 'type', 'applies_to', 'value', 'unit']):
                key_cols.append(col)
        
        if not key_cols:
            return df, 0
        
        # Find duplicates
        duplicates_mask = df.duplicated(subset=key_cols, keep=False)
        
        if not duplicates_mask.any():
            return df, 0
        
        # Process duplicates
        duplicate_groups = df[duplicates_mask].groupby(key_cols, dropna=False)
        rows_to_drop = []
        
        for group_keys, group_df in duplicate_groups:
            indices = group_df.index.tolist()
            
            # Check if they're exactly identical
            if group_df.drop(columns=['Notes'], errors='ignore').drop_duplicates().shape[0] == 1:
                # Exact duplicates - keep first, drop others
                keep_idx = indices[0]
                rows_to_drop.extend(indices[1:])
                df.at[keep_idx, 'Notes'] = self._append_note(
                    df.at[keep_idx, 'Notes'], 
                    f"Duplicate entry removed ({len(indices)-1} identical)"
                )
            else:
                # Near-duplicates - keep the most complete
                scores = group_df.apply(lambda row: sum(pd.notna(v) and str(v).strip() != '' for v in row), axis=1)
                best_idx = scores.idxmax()
                rows_to_drop.extend([idx for idx in indices if idx != best_idx])
                df.at[best_idx, 'Notes'] = self._append_note(
                    df.at[best_idx, 'Notes'],
                    f"Similar entries merged ({len(indices)-1} variants)"
                )
        
        # Drop duplicates
        df = df.drop(index=rows_to_drop)
        return df, len(rows_to_drop)
    
    def _deduplicate_by_value(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """
        Deduplicate based on jurisdiction + value, even if different requirement types.
        This catches cases like 'Operating hours' vs 'Working hours' with same value.
        Only merges on specific meaningful values (numbers, time ranges), not generic values like 'Yes'.
        """
        # Find jurisdiction and value columns
        jurisdiction_cols = []
        value_col = None
        type_col = None
        section_col = None
        details_col = None
        
        for col in df.columns:
            col_lower = col.lower()
            if 'state' in col_lower or 'county' in col_lower or 'jurisdiction' in col_lower:
                jurisdiction_cols.append(col)
            elif col_lower == 'value' or 'value' in col_lower:
                value_col = col
            elif 'requirement_type' in col_lower or col_lower == 'requirement_type':
                type_col = col
            elif 'section' in col_lower:
                section_col = col
            elif 'details' in col_lower:
                details_col = col
        
        if not jurisdiction_cols or not value_col:
            return df, 0
        
        # Filter out rows with generic values that shouldn't be deduplicated
        generic_values = {'Yes', 'yes', 'No', 'no', 'Required', 'required', 'Conditional', 'conditional', 
                         'Permitted', 'permitted', 'Allowed', 'allowed', 'Prohibited', 'prohibited'}
        
        # Only consider rows with specific numeric or time-based values for deduplication
        meaningful_mask = df[value_col].apply(
            lambda v: pd.notna(v) and 
            str(v).strip() not in generic_values and 
            str(v).strip() != '' and
            (isinstance(v, (int, float)) or  # numeric values
             'a.m.' in str(v).lower() or 'p.m.' in str(v).lower() or  # time ranges
             'feet' in str(v).lower() or 'meter' in str(v).lower() or  # distance values
             str(v).replace(',', '').replace('.', '').replace('-', '').replace(':', '').isdigit())  # numeric strings
        )
        
        df_to_dedup = df[meaningful_mask]
        
        if df_to_dedup.empty:
            return df, 0
        
        # Find rows with same jurisdiction + same value
        dedup_cols = jurisdiction_cols + [value_col]
        duplicates_mask = df_to_dedup.duplicated(subset=dedup_cols, keep=False)
        
        if not duplicates_mask.any():
            return df, 0
        
        # Process value-based duplicates
        duplicate_groups = df[duplicates_mask].groupby(dedup_cols, dropna=False)
        rows_to_drop = []
        
        for group_keys, group_df in duplicate_groups:
            if len(group_df) == 1:
                continue
                
            indices = group_df.index.tolist()
            
            # Prefer certain types over others (e.g., 'Working hours' over 'Operating hours')
            preferred_types = ['Working hours', 'Setback', 'Noise limits', 'Permit required']
            
            # Find best entry to keep
            best_idx = None
            if type_col and type_col in group_df.columns:
                # First try to find preferred type
                for pref_type in preferred_types:
                    matching = group_df[group_df[type_col] == pref_type]
                    if not matching.empty:
                        best_idx = matching.index[0]
                        break
            
            # If no preferred type found, use completeness score
            if best_idx is None:
                scores = group_df.apply(lambda row: sum(pd.notna(v) and str(v).strip() != '' for v in row), axis=1)
                best_idx = scores.idxmax()
            
            # Merge details and sections from all entries
            if details_col and details_col in df.columns:
                combined_details = []
                seen_details = set()
                for idx in indices:
                    detail = df.at[idx, details_col]
                    if pd.notna(detail) and str(detail).strip():
                        detail_str = str(detail).strip()
                        if detail_str not in seen_details:
                            combined_details.append(detail_str)
                            seen_details.add(detail_str)
                
                if combined_details and len(combined_details) > 1:
                    df.at[best_idx, details_col] = ' | '.join(combined_details)
            
            # Merge sections
            if section_col and section_col in df.columns:
                combined_sections = []
                seen_sections = set()
                for idx in indices:
                    section = df.at[idx, section_col]
                    if pd.notna(section) and str(section).strip():
                        section_str = str(section).strip()
                        if section_str not in seen_sections:
                            combined_sections.append(section_str)
                            seen_sections.add(section_str)
                
                if combined_sections:
                    df.at[best_idx, section_col] = ', '.join(combined_sections)
            
            # Drop other entries
            rows_to_drop.extend([idx for idx in indices if idx != best_idx])
            df.at[best_idx, 'Notes'] = self._append_note(
                df.at[best_idx, 'Notes'],
                f"Merged {len(indices)-1} entries with same value"
            )
        
        # Drop duplicates
        df = df.drop(index=rows_to_drop)
        return df, len(rows_to_drop)
    
    def _append_note(self, existing_note: Any, new_note: str) -> str:
        """Append a note to existing notes."""
        existing = str(existing_note) if pd.notna(existing_note) and str(existing_note).strip() else ""
        if existing:
            return f"{existing}; {new_note}"
        return new_note
    
    def save_excel(self, df: pd.DataFrame, output_path: Path):
        """
        Save DataFrame to clean Excel file with professional formatting.
        
        Features:
        - Dark blue header with white bold text
        - Alternating row colors (white/light gray) for readability
        - Text wrapping enabled for long content
        - Centered alignment for short fields, left-aligned for details
        - Auto-adjusted column widths with smart limits
        - Frozen header row for scrolling
        - Clean borders throughout
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write data
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Data", index=False)
        
        # Load workbook for styling
        wb = load_workbook(output_path)
        ws = wb["Data"]
        
        # Define styles
        header_fill = PatternFill(
            start_color='FF1F4E78',  # Dark blue
            end_color='FF1F4E78',
            fill_type="solid"
        )
        header_font = Font(bold=True, color='FFFFFFFF', size=11)
        
        # Alternating row colors
        white_fill = PatternFill(
            start_color='FFFFFFFF',
            end_color='FFFFFFFF',
            fill_type="solid"
        )
        gray_fill = PatternFill(
            start_color='FFF2F2F2',  # Light gray
            end_color='FFF2F2F2',
            fill_type="solid"
        )
        
        thin_border = Border(
            left=Side(style="thin", color="D3D3D3"),
            right=Side(style="thin", color="D3D3D3"),
            top=Side(style="thin", color="D3D3D3"),
            bottom=Side(style="thin", color="D3D3D3"),
        )
        
        data_font = Font(size=10)
        
        # Identify column types for smart alignment
        # Short/categorical columns get centered, long text gets left-aligned with wrap
        center_aligned_cols = []
        for idx, col_name in enumerate(df.columns, start=1):
            # Check if column has mostly short values
            col_letter = get_column_letter(idx)
            sample_values = df[col_name].dropna().astype(str).head(20)
            avg_length = sample_values.str.len().mean() if len(sample_values) > 0 else 0
            
            # Center if: numeric-like, dates, short categorical values
            if avg_length < 30 or col_name.lower() in ['state', 'county', 'city', 'type', 'value', 'unit', 'date', 'number']:
                center_aligned_cols.append(col_letter)
        
        # Style header row
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.border = thin_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        
        # Style data rows with alternating colors
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, max_row=ws.max_row), start=2):
            # Alternate row color
            row_fill = white_fill if row_idx % 2 == 0 else gray_fill
            
            for cell in row:
                cell.fill = row_fill
                cell.border = thin_border
                cell.font = data_font
                
                # Smart alignment based on column
                col_letter = get_column_letter(cell.column)
                if col_letter in center_aligned_cols:
                    cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        
        # Freeze header row
        ws.freeze_panes = "A2"
        
        # Auto-adjust column widths with smart sizing
        for column in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column[0].column)
            
            for cell in column:
                try:
                    cell_value = str(cell.value) if cell.value is not None else ""
                    if len(cell_value) > max_length:
                        max_length = len(cell_value)
                except:
                    pass
            
            # Set width with reasonable limits
            # Narrower for short columns, wider for details
            if column_letter in center_aligned_cols:
                adjusted_width = min(max(max_length + 2, 12), 25)  # Centered cols: 12-25
            else:
                adjusted_width = min(max(max_length + 2, 20), 60)  # Text cols: 20-60
            
            ws.column_dimensions[column_letter].width = adjusted_width
        
        # Set default row height to accommodate wrapped text
        for row in range(2, ws.max_row + 1):
            ws.row_dimensions[row].height = None  # Auto-height
        
        # Save
        wb.save(output_path)
    
    def save_csv(self, df: pd.DataFrame, output_path: Path):
        """Save DataFrame to CSV."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
