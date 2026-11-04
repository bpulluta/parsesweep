"""Flatten extracted items into spreadsheet rows; columns keep schema field names."""

import json
from typing import Any, Dict, List

import pandas as pd


class DataFlattener:
    """Nested objects become ``parent_child`` columns; arrays become one cell."""

    def __init__(self, schema_metadata):
        cfg = schema_metadata.get_flattening_config()
        self.unit_fields: List[str] = cfg.get("unit_fields", [])
        self.unit_normalizations: Dict[str, str] = cfg.get("unit_normalizations", {})

    def flatten_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        flattened: Dict[str, Any] = {}
        for key, value in item.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                flattened[key] = ""
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    flattened[f"{key}_{nested_key}"] = nested_value
            elif isinstance(value, list):
                flattened[key] = self._join(value)
            else:
                flattened[key] = value
        return flattened

    @staticmethod
    def _join(values: List[Any]) -> str:
        """Primitives comma-join; arrays holding objects keep compact JSON."""
        if any(isinstance(value, (dict, list)) for value in values):
            return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        return ", ".join(str(value) for value in values)

    def normalize_units(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.unit_normalizations:
            for column in self.unit_fields:
                if column in df.columns:
                    df[column] = df[column].replace(self.unit_normalizations)
        return df
