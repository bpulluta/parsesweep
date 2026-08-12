#!/usr/bin/env python3
"""
Data flattening utilities for converting nested JSON to flat spreadsheet rows.

Handles intelligent flattening of complex data structures for analysis-ready output.

Domain-neutral: how arrays of objects become columns (which field labels a group,
which fields distinguish siblings, which fields carry the value/unit, how units are
normalized, and the expand-vs-summarize thresholds) is driven by the optional
``compilation.flattening`` schema block. Every knob falls back to a documented
general default, so a schema that declares nothing keeps the legacy behavior.
"""

import pandas as pd
from typing import Dict, List, Any

from ..utils.normalizers import humanize_field_name

# --- General defaults (used when the schema declares no override) -------------
# These are broadly useful field-name conventions, not domain rules: any schema
# may override them via ``compilation.flattening`` without touching code.
DEFAULT_TYPE_FIELDS = ["type", "charge_type", "category", "fee_type", "name"]
DEFAULT_SKIP_FIELDS = [
    "details",
    "description",
    "charge_description",
    "conditions",
    "notes",
    "comments",
]
DEFAULT_DISTINGUISHING_FIELDS = ["season", "time_period", "tier", "period"]
DEFAULT_VALUE_FIELDS = ["rate", "value", "amount", "cost"]
DEFAULT_UNIT_FIELDS = ["unit", "units"]
DEFAULT_SEASON_FIELDS = ["season", "period"]
# Values treated as "no meaningful distinguisher / not worth showing".
DEFAULT_PLACEHOLDER_VALUES = ["none", "null", "year-round", ""]
DEFAULT_EXPAND_MAX_ITEMS = 15
DEFAULT_EXPAND_MAX_FIELDS = 20
# Symbol/abbreviation → canonical unit. General measurement conveniences; extend
# or replace per domain via ``compilation.flattening.unit_normalizations``.
DEFAULT_UNIT_NORMALIZATIONS = {
    "'": "feet",
    "′": "feet",  # Prime symbol
    "ft": "feet",
    "ft.": "feet",
    '"': "inches",
    "″": "inches",  # Double prime
    "in": "inches",
    "in.": "inches",
    "dB(A)": "dBA",
    "db(a)": "dBA",
    "DB(A)": "dBA",
}


class DataFlattener:
    """
    Flatten nested JSON structures into spreadsheet-friendly rows.

    Provides smart handling of:
    - Nested objects → flattened columns
    - Arrays of primitives → comma-separated strings
    - Arrays of objects → expanded columns or readable summaries
    - Automatic column naming with Title Case
    """

    def __init__(self, schema_metadata=None):
        """
        Args:
            schema_metadata: Optional SchemaMetadata. When provided, its
                ``compilation.flattening`` block overrides the module defaults.
                When omitted (e.g. callers that only use ``make_column_name``),
                the documented defaults apply.
        """
        cfg = (
            schema_metadata.get_flattening_config() if schema_metadata else {}
        ) or {}
        self.type_fields = cfg.get("type_fields") or DEFAULT_TYPE_FIELDS
        self.skip_fields = cfg.get("skip_fields") or DEFAULT_SKIP_FIELDS
        self.distinguishing_fields = (
            cfg.get("distinguishing_fields") or DEFAULT_DISTINGUISHING_FIELDS
        )
        self.value_fields = cfg.get("value_fields") or DEFAULT_VALUE_FIELDS
        self.unit_fields = cfg.get("unit_fields") or DEFAULT_UNIT_FIELDS
        self.season_fields = cfg.get("season_fields") or DEFAULT_SEASON_FIELDS
        self.placeholder_values = {
            str(v).lower()
            for v in (
                cfg.get("placeholder_values") or DEFAULT_PLACEHOLDER_VALUES
            )
        }
        self.expand_max_items = int(
            cfg.get("expand_max_items", DEFAULT_EXPAND_MAX_ITEMS)
        )
        self.expand_max_fields = int(
            cfg.get("expand_max_fields", DEFAULT_EXPAND_MAX_FIELDS)
        )
        # None → use defaults; explicit {} → no normalization (opt-out).
        unit_norm = cfg.get("unit_normalizations")
        self.unit_normalizations = (
            DEFAULT_UNIT_NORMALIZATIONS if unit_norm is None else unit_norm
        )

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

        Returns
        -------
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

        Returns
        -------
            Clean Title Case column name

        Examples
        --------
            >>> flattener.make_column_name("charge_type")
            "Charge Type"
            >>> flattener.make_column_name("annualConsumption")
            "Annual Consumption"
        """
        return humanize_field_name(name)

    def handle_array(self, key: str, items: List) -> Dict:
        """
        Intelligently handle array data based on its structure.

        Decision logic:
        - Empty → empty string
        - Simple values → comma-separated
        - Few objects (< expand_max_items) with few fields (< expand_max_fields)
          → expand to columns
        - Many/complex objects → readable summary

        Args:
            key: Column name for this array
            items: Array data to process

        Returns
        -------
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

        Returns
        -------
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
            num_items <= self.expand_max_items
            and num_fields <= self.expand_max_fields
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

        Returns
        -------
            Dictionary with multiple columns (one per field per item)
        """
        result = {}

        # Find grouping field
        group_key = next(
            (f for f in self.type_fields if f in items[0]), None
        )

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
                        if field in self.skip_fields:
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
                    if value is not None and field not in self.skip_fields:
                        col_name = f"{parent_key} {idx} {self.make_column_name(field)}"
                        result[col_name] = value

        return result

    def get_item_suffix(self, obj: Dict, idx: int, total: int) -> str:
        """
        Generate suffix to distinguish items with same type.

        Looks for the configured distinguishing fields in order, skipping
        placeholder values. Falls back to numbering if none is found.

        Args:
            obj: Object to analyze
            idx: Index in the array
            total: Total items in array

        Returns
        -------
            Suffix string (e.g., " Summer", " Tier 1", " 2")
        """
        if total == 1:
            return ""

        # Try to find distinguishing characteristic
        for field_name in self.distinguishing_fields:
            value = obj.get(field_name)
            if value and str(value).lower() not in self.placeholder_values:
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

        Returns
        -------
            Semicolon-separated summary string
        """
        summaries = []

        for item in items:
            # Try to identify key information
            type_val = self.find_value(item, self.type_fields)
            value_val = self.find_value(item, self.value_fields)
            unit_val = self.find_value(item, self.unit_fields)
            season_val = self.find_value(item, self.season_fields)

            # Build summary
            if type_val:
                summary = str(type_val)
                if (
                    season_val
                    and str(season_val).lower() not in self.placeholder_values
                ):
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

        Returns
        -------
            First non-null value found, or None
        """
        for key in possible_keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        return None

    def normalize_units(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize unit values for consistency.

        Converts symbols/abbreviations to canonical units for data analysis,
        using the configured ``unit_normalizations`` map (general measurement
        defaults unless the schema overrides them).

        Args:
            df: DataFrame with potential unit column

        Returns
        -------
            DataFrame with normalized units
        """
        if not self.unit_normalizations:
            return df

        # Find a unit column by the configured unit field names.
        unit_col = None
        unit_names = {name.lower() for name in self.unit_fields}
        for col in df.columns:
            if col.lower() in unit_names:
                unit_col = col
                break

        if unit_col is None:
            return df

        df[unit_col] = df[unit_col].replace(self.unit_normalizations)

        return df
