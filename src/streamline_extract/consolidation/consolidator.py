#!/usr/bin/env python3
"""
Universal Consolidator - Works with ANY schema automatically.

Clean, simple, and intelligent. No configuration needed.
"""
import json
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


class Consolidator:
    """
    Universal consolidator that automatically handles any extraction schema.
    
    Simply point it at extracted JSON files and it:
    - Detects schema structure automatically
    - Intelligently flattens nested data for spreadsheets
    - Creates readable, analysis-ready output
    - Handles both simple and complex nested structures
    
    No configuration required - just works.
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
            
            # Check if items have a nested array that should be flattened
            # Pattern: parent_array → nested_array (e.g., rate_schedules → charges)
            nested_array_key = None
            if main_array and isinstance(main_array[0], dict):
                # Find first array field in the item (if any)
                for key, value in main_array[0].items():
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        nested_array_key = key
                        break
            
            if nested_array_key:
                # Flatten: one row per nested item with parent context
                for parent_item in main_array:
                    nested_items = parent_item.pop(nested_array_key, [])
                    parent_context = self._flatten_item(parent_item)
                    
                    for nested_item in nested_items:
                        nested_row = self._flatten_item(nested_item)
                        row = {**context, **parent_context, **nested_row}
                        rows.append(row)
            else:
                # Standard: one row per main array item
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
        """
        Intelligently flatten a data item for spreadsheet output.
        
        Automatically chooses the best representation for nested structures:
        - Simple values → direct columns
        - Nested objects → flattened columns
        - Arrays of primitives → comma-separated
        - Arrays of objects → smart expansion or summary
        """
        flattened = {}
        
        for key, value in item.items():
            column_name = self._make_column_name(key)
            
            if value is None or (isinstance(value, str) and not value.strip()):
                flattened[column_name] = ''
            elif isinstance(value, dict):
                # Nested object - flatten it
                for nested_key, nested_val in value.items():
                    nested_col = self._make_column_name(f"{key}_{nested_key}")
                    flattened[nested_col] = nested_val
            elif isinstance(value, list):
                flattened.update(self._handle_array(column_name, value))
            else:
                flattened[column_name] = value
        
        return flattened
    
    def _make_column_name(self, name: str) -> str:
        """Convert any naming style to clean Title Case."""
        # Handle camelCase and snake_case
        name = name.replace('_', ' ')
        name = ''.join([' ' + c if c.isupper() else c for c in name]).strip()
        return ' '.join(word.capitalize() for word in name.split())
    
    def _handle_array(self, key: str, items: List) -> Dict:
        """
        Intelligently handle array data based on its structure.
        
        Decision logic:
        - Empty → empty string
        - Simple values → comma-separated
        - Few objects (<8) with few fields (<8) → expand to columns
        - Many/complex objects → readable summary
        """
        if not items:
            return {key: ''}
        
        # Simple array (strings, numbers)
        if not isinstance(items[0], dict):
            return {key: ', '.join(str(x) for x in items)}
        
        # Complex array (objects)
        return self._handle_object_array(key, items)
    
    def _handle_object_array(self, key: str, items: List[Dict]) -> Dict:
        """
        Smart handling of arrays of objects.
        
        Universal decision logic:
        - Small, consistent arrays → expand to columns (good for analysis)
        - Large or inconsistent arrays → readable summary (good for context)
        
        Works for any domain: tariffs, permits, requirements, etc.
        """
        num_items = len(items)
        
        if num_items == 0:
            return {key: ''}
        
        # Check field consistency across items
        first_keys = set(items[0].keys())
        all_same_structure = all(set(item.keys()) == first_keys for item in items)
        num_fields = len(first_keys)
        
        # Decision: expand if small & consistent (good for structured data analysis)
        # This naturally works for charges, fees, tiers, components, etc.
        should_expand = (
            num_items <= 15 and  # Not too many rows
            num_fields <= 20 and  # Not too many columns  
            all_same_structure  # Consistent structure
        )
        
        if should_expand:
            return self._expand_to_columns(key, items)
        else:
            return {key: self._summarize_objects(items)}
    
    def _expand_to_columns(self, parent_key: str, items: List[Dict]) -> Dict:
        """
        Expand array of objects into structured columns for analysis.
        
        Automatically creates clean column names by:
        1. Grouping items by their type/category field
        2. Adding distinguishing context (season, period, tier, etc.)
        3. Expanding each item's fields into separate columns
        
        Works for any data: charges, requirements, fees, tiers, etc.
        """
        result = {}
        
        # Find grouping field
        type_fields = ['type', 'charge_type', 'category', 'fee_type', 'name']
        group_key = next((f for f in type_fields if f in items[0]), None)
        
        if group_key:
            # Group by type and expand with context
            groups = {}
            for item in items:
                group_val = str(item.get(group_key, 'Other'))
                if group_val not in groups:
                    groups[group_val] = []
                groups[group_val].append(item)
            
            for group_name, group_items in groups.items():
                # Clean group name for columns
                group_clean = self._make_column_name(group_name)
                
                for idx, obj in enumerate(group_items):
                    # Determine suffix based on distinguishing features
                    suffix = self._get_item_suffix(obj, idx, len(group_items))
                    
                    # Create columns for important fields
                    for field, value in obj.items():
                        if field == group_key or value is None:
                            continue
                        
                        # Skip verbose/redundant fields in column expansion
                        skip_fields = ['details', 'description', 'charge_description', 
                                      'conditions', 'notes', 'comments']
                        if field in skip_fields:
                            continue
                        
                        field_clean = self._make_column_name(field)
                        col_name = f"{parent_key} {group_clean}{suffix} {field_clean}"
                        result[col_name] = value
        else:
            # No grouping - number sequentially
            for idx, obj in enumerate(items, 1):
                for field, value in obj.items():
                    if value is not None and field not in ['details', 'description']:
                        col_name = f"{parent_key} {idx} {self._make_column_name(field)}"
                        result[col_name] = value
        
        return result
    
    def _get_item_suffix(self, obj: Dict, idx: int, total: int) -> str:
        """
        Generate suffix to distinguish items with same type.
        
        Looks for common distinguishing fields in order:
        - season, time_period, tier, period
        Falls back to numbering if no distinguisher found.
        """
        if total == 1:
            return ""
        
        # Try to find distinguishing characteristic
        distinguishing_fields = [
            ('season', obj.get('season')),
            ('time_period', obj.get('time_period')),
            ('tier', obj.get('tier')),
            ('period', obj.get('period'))
        ]
        
        for field_name, value in distinguishing_fields:
            if value and str(value).lower() not in ['none', 'null', 'year-round', '']:
                return f" {self._make_column_name(str(value))}"
        
        # Fall back to numbering
        return f" {idx + 1}"
    
    def _summarize_objects(self, items: List[Dict]) -> str:
        """
        Create readable summary when array is too large to expand.
        
        Builds concise "Type: Value Unit" format from common field patterns.
        Automatically detects type, value, unit, and contextual fields.
        """
        summaries = []
        
        for item in items:
            # Try to identify key information
            type_val = self._find_value(item, ['type', 'charge_type', 'category', 'name'])
            value_val = self._find_value(item, ['rate', 'value', 'amount', 'cost'])
            unit_val = self._find_value(item, ['unit', 'units'])
            season_val = self._find_value(item, ['season', 'period'])
            
            # Build summary
            if type_val:
                summary = str(type_val)
                if season_val and str(season_val).lower() != 'year-round':
                    summary += f" ({season_val})"
                if value_val is not None:
                    summary += f": {value_val}"
                    if unit_val:
                        summary += f" {unit_val}"
                summaries.append(summary)
        
        return '; '.join(summaries) if summaries else str(items)
    
    def _find_value(self, obj: Dict, possible_keys: List[str]) -> Any:
        """Find first non-null value from list of possible keys."""
        for key in possible_keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        return None
    
    def _deduplicate_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate rows using all columns except metadata.
        
        Only removes rows that are truly identical in all identifying fields.
        Excludes metadata columns (Notes, section references, etc.) from comparison.
        """
        if df.empty:
            return df
        
        if 'Notes' not in df.columns:
            df['Notes'] = ''
        
        # Exclude metadata/non-identifying columns from duplicate detection
        # These are added by consolidator or are references, not identifying data
        exclude_cols = [col for col in df.columns 
                       if any(kw in col.lower() for kw in 
                             ['notes', 'section', 'location', 'tariff location'])]
        
        # Use all other columns for duplicate detection
        compare_cols = [col for col in df.columns if col not in exclude_cols]
        
        if not compare_cols:
            print("  ✓ No duplicates found")
            return df
        
        # Find exact duplicates (all compare columns must match)
        duplicates = df[df.duplicated(subset=compare_cols, keep=False)]
        if duplicates.empty:
            print("  ✓ No duplicates found")
            return df
        
        # Process each duplicate group
        rows_to_drop = []
        for _, group in duplicates.groupby(compare_cols, dropna=False):
            if len(group) <= 1:
                continue
            
            indices = group.index.tolist()
            
            # Keep the most complete row (most non-null values)
            completeness = group.apply(
                lambda row: sum(1 for v in row if pd.notna(v) and str(v).strip()), 
                axis=1
            )
            keep_idx = completeness.idxmax()
            drop_indices = [idx for idx in indices if idx != keep_idx]
            
            rows_to_drop.extend(drop_indices)
            
            # Add note to kept row
            note = f"Merged {len(drop_indices)} duplicate(s)"
            current_note = df.at[keep_idx, 'Notes']
            df.at[keep_idx, 'Notes'] = f"{current_note}; {note}" if current_note else note
        
        # Remove duplicates
        df = df.drop(index=rows_to_drop)
        
        if rows_to_drop:
            print(f"  ✓ Removed {len(rows_to_drop)} duplicate entries")
        else:
            print("  ✓ No duplicates found")
        
        return df
    
    
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
