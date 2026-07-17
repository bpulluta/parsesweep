#!/usr/bin/env python3
"""
Data flattening utilities for converting nested JSON to flat spreadsheet rows.

Handles intelligent flattening of complex data structures for analysis-ready output.
"""

import pandas as pd
from typing import Dict, List, Any


class DataFlattener:
    """
    Flatten nested JSON structures into spreadsheet-friendly rows.

    Provides smart handling of:
    - Nested objects → flattened columns
    - Arrays of primitives → comma-separated strings
    - Arrays of objects → expanded columns or readable summaries
    - Automatic column naming with Title Case
    """

    def flatten_item(self, item: Dict) -> Dict:
        """
        Intelligently flatten a data item for spreadsheet output.

        Automatically chooses the best representation for nested structures:
        - Simple values → direct columns
        - Nested objects → flattened columns
        - Arrays of primitives → comma-separated
        - Arrays of objects → smart expansion or summary

        Args:
            item: Dictionary with potentially nested structure

        Returns:
            Flattened dictionary suitable for DataFrame row
        """
        flattened = {}

        for key, value in item.items():
            column_name = self.make_column_name(key)

            if value is None or (isinstance(value, str) and not value.strip()):
                flattened[column_name] = ""
            elif isinstance(value, dict):
                # Nested object - flatten it
                for nested_key, nested_val in value.items():
                    nested_col = self.make_column_name(f"{key}_{nested_key}")
                    flattened[nested_col] = nested_val
            elif isinstance(value, list):
                flattened.update(self.handle_array(column_name, value))
            else:
                flattened[column_name] = value

        return flattened

    def make_column_name(self, name: str) -> str:
        """
        Convert any naming style to clean Title Case.

        Handles snake_case, camelCase, and mixed formats.

        Args:
            name: Raw field name

        Returns:
            Clean Title Case column name

        Examples:
            >>> flattener.make_column_name("charge_type")
            "Charge Type"
            >>> flattener.make_column_name("annualConsumption")
            "Annual Consumption"
        """
        # Handle camelCase and snake_case
        name = name.replace("_", " ")
        name = "".join([" " + c if c.isupper() else c for c in name]).strip()
        return " ".join(word.capitalize() for word in name.split())

    def handle_array(self, key: str, items: List) -> Dict:
        """
        Intelligently handle array data based on its structure.

        Decision logic:
        - Empty → empty string
        - Simple values → comma-separated
        - Few objects (<8) with few fields (<8) → expand to columns
        - Many/complex objects → readable summary

        Args:
            key: Column name for this array
            items: Array data to process

        Returns:
            Dictionary with column(s) for this array
        """
        if not items:
            return {key: ""}

        # Simple array (strings, numbers)
        if not isinstance(items[0], dict):
            return {key: ", ".join(str(x) for x in items)}

        # Complex array (objects)
        return self.handle_object_array(key, items)

    def handle_object_array(self, key: str, items: List[Dict]) -> Dict:
        """
        Smart handling of arrays of objects.

        Universal decision logic:
        - Small, consistent arrays → expand to columns (good for analysis)
        - Large or inconsistent arrays → readable summary (good for context)

        Works for any domain: tariffs, permits, requirements, etc.

        Args:
            key: Parent key for this array
            items: List of dictionaries

        Returns:
            Dictionary with either expanded columns or summary string
        """
        num_items = len(items)

        if num_items == 0:
            return {key: ""}

        # Check field consistency across items
        first_keys = set(items[0].keys())
        all_same_structure = all(
            set(item.keys()) == first_keys for item in items
        )
        num_fields = len(first_keys)

        # Decision: expand if small & consistent (good for structured data analysis)
        # This naturally works for charges, fees, tiers, components, etc.
        should_expand = (
            num_items <= 15  # Not too many rows
            and num_fields <= 20  # Not too many columns
            and all_same_structure  # Consistent structure
        )

        if should_expand:
            return self.expand_to_columns(key, items)
        else:
            return {key: self.summarize_objects(items)}

    def expand_to_columns(self, parent_key: str, items: List[Dict]) -> Dict:
        """
        Expand array of objects into structured columns for analysis.

        Automatically creates clean column names by:
        1. Grouping items by their type/category field
        2. Adding distinguishing context (season, period, tier, etc.)
        3. Expanding each item's fields into separate columns

        Works for any data: charges, requirements, fees, tiers, etc.

        Args:
            parent_key: Parent field name
            items: List of objects to expand

        Returns:
            Dictionary with multiple columns (one per field per item)
        """
        result = {}

        # Find grouping field
        type_fields = ["type", "charge_type", "category", "fee_type", "name"]
        group_key = next((f for f in type_fields if f in items[0]), None)

        if group_key:
            # Group by type and expand with context
            groups = {}
            for item in items:
                group_val = str(item.get(group_key, "Other"))
                if group_val not in groups:
                    groups[group_val] = []
                groups[group_val].append(item)

            for group_name, group_items in groups.items():
                # Clean group name for columns
                group_clean = self.make_column_name(group_name)

                for idx, obj in enumerate(group_items):
                    # Determine suffix based on distinguishing features
                    suffix = self.get_item_suffix(obj, idx, len(group_items))

                    # Create columns for important fields
                    for field, value in obj.items():
                        if field == group_key or value is None:
                            continue

                        # Skip verbose/redundant fields in column expansion
                        skip_fields = [
                            "details",
                            "description",
                            "charge_description",
                            "conditions",
                            "notes",
                            "comments",
                        ]
                        if field in skip_fields:
                            continue

                        field_clean = self.make_column_name(field)
                        col_name = (
                            f"{parent_key} {group_clean}{suffix} {field_clean}"
                        )
                        result[col_name] = value
        else:
            # No grouping - number sequentially
            for idx, obj in enumerate(items, 1):
                for field, value in obj.items():
                    if value is not None and field not in [
                        "details",
                        "description",
                    ]:
                        col_name = f"{parent_key} {idx} {self.make_column_name(field)}"
                        result[col_name] = value

        return result

    def get_item_suffix(self, obj: Dict, idx: int, total: int) -> str:
        """
        Generate suffix to distinguish items with same type.

        Looks for common distinguishing fields in order:
        - season, time_period, tier, period
        Falls back to numbering if no distinguisher found.

        Args:
            obj: Object to analyze
            idx: Index in the array
            total: Total items in array

        Returns:
            Suffix string (e.g., " Summer", " Tier 1", " 2")
        """
        if total == 1:
            return ""

        # Try to find distinguishing characteristic
        distinguishing_fields = [
            ("season", obj.get("season")),
            ("time_period", obj.get("time_period")),
            ("tier", obj.get("tier")),
            ("period", obj.get("period")),
        ]

        for field_name, value in distinguishing_fields:
            if value and str(value).lower() not in [
                "none",
                "null",
                "year-round",
                "",
            ]:
                return f" {self.make_column_name(str(value))}"

        # Fall back to numbering
        return f" {idx + 1}"

    def summarize_objects(self, items: List[Dict]) -> str:
        """
        Create readable summary when array is too large to expand.

        Builds concise "Type: Value Unit" format from common field patterns.
        Automatically detects type, value, unit, and contextual fields.

        Args:
            items: List of objects to summarize

        Returns:
            Semicolon-separated summary string
        """
        summaries = []

        for item in items:
            # Try to identify key information
            type_val = self.find_value(
                item, ["type", "charge_type", "category", "name"]
            )
            value_val = self.find_value(
                item, ["rate", "value", "amount", "cost"]
            )
            unit_val = self.find_value(item, ["unit", "units"])
            season_val = self.find_value(item, ["season", "period"])

            # Build summary
            if type_val:
                summary = str(type_val)
                if season_val and str(season_val).lower() != "year-round":
                    summary += f" ({season_val})"
                if value_val is not None:
                    summary += f": {value_val}"
                    if unit_val:
                        summary += f" {unit_val}"
                summaries.append(summary)

        return "; ".join(summaries) if summaries else str(items)

    def find_value(self, obj: Dict, possible_keys: List[str]) -> Any:
        """
        Find first non-null value from list of possible keys.

        Useful for finding data in objects with varying field names.

        Args:
            obj: Dictionary to search
            possible_keys: List of keys to try in order

        Returns:
            First non-null value found, or None
        """
        for key in possible_keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        return None

    def normalize_units(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize unit values for consistency.

        Converts symbols to spelled-out units for data analysis.

        Args:
            df: DataFrame with potential unit column

        Returns:
            DataFrame with normalized units
        """
        # Check if 'Unit' column exists
        unit_col = None
        for col in df.columns:
            if col.lower() in ["unit", "units"]:
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
