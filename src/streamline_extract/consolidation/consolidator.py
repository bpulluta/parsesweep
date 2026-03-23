#!/usr/bin/env python3
"""
Universal Consolidator - Works with ANY schema automatically.

Clean, simple, and intelligent. No configuration needed.
"""
import json
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple
from .schema_detector import SchemaDetector
from .data_flattener import DataFlattener
from .deduplicator import Deduplicator
from .excel_formatter import ExcelFormatter
from .csv_exporter import CsvExporter
from ..utils.exceptions import SchemaMetadataError
from ..utils.normalizers import normalize_state_column


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
    
    def __init__(self, schema_metadata, verbose=True, debug=False):
        """
        Initialize consolidator.
        
        Args:
            schema_metadata: SchemaMetadata instance (required in v2.0+)
            verbose: Whether to print informational messages (default: True)
            debug: Whether to print debug-level details (default: False)
            
        Raises:
            SchemaMetadataError: If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "Consolidator requires schema metadata.\n"
                "Schema metadata is required as of StreamlineExtract v2.0."
            )
        
        self.schema_info = None
        self.schema_metadata = schema_metadata
        self.verbose = verbose
        self.debug = debug
        self.file_count = 0
        self.total_items = 0
        self.duplicates_removed = 0
        self.detector = SchemaDetector(schema_metadata=schema_metadata)
        self.flattener = DataFlattener()
        self.deduplicator = Deduplicator(schema_metadata=schema_metadata)
        self.excel_formatter = ExcelFormatter()
        self.csv_exporter = CsvExporter()
    
    def consolidate_from_directory(self, json_dir: Path) -> Tuple[pd.DataFrame, Dict]:
        """
        Load all JSON files and consolidate into DataFrame.
        Searches recursively through nested subdirectories.
        
        Returns:
            Tuple of (DataFrame, schema_info dict)
        """
        # First try direct children, then search recursively for nested structures
        json_files = list(json_dir.glob("*.json"))
        if not json_files:
            json_files = list(json_dir.rglob("*.json"))
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
        self.schema_info = self.detector.detect_structure(sample_data)
        
        if self.verbose:
            print(f"📋 Schema detected: {self.schema_info['type']}")
            print(f"   Main entity: {self.schema_info['main_array_key']}")
            print(f"   Identifier fields: {', '.join(self.schema_info['id_fields'])}")
        
        if self.debug:
            print(f"\n[DEBUG] Full schema metadata:")
            metadata = self.schema_metadata.metadata
            print(f"  Domain: {metadata.get('domain', 'N/A')}")
            print(f"  Version: {metadata.get('version', 'N/A')}")
            if 'extraction' in metadata:
                print(f"  Extraction config:")
                for key, value in metadata['extraction'].items():
                    print(f"    {key}: {value}")
            if 'consolidation' in metadata and 'deduplication' in metadata['consolidation']:
                dedup = metadata['consolidation']['deduplication']
                print(f"  Deduplication config:")
                print(f"    Key fields: {dedup.get('key_fields', [])}")
                print(f"    Ignore fields: {dedup.get('ignore_fields', [])}")
        
        # Extract data from all files
        rows = []
        self.file_count = len(json_files)
        
        for idx, json_file in enumerate(sorted(json_files), 1):
            if self.debug:
                print(f"\n[DEBUG] Processing file {idx}/{self.file_count}: {json_file.name}")
            
            with open(json_file) as f:
                data = json.load(f)
            
            # Handle wrapped extraction
            if "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            
            # Extract identifier/context fields
            context = self.detector.extract_context(data, self.schema_info)
            
            # Extract main array items
            main_array = data.get(self.schema_info['main_array_key'], [])
            
            # Filter out invalid items (e.g., strings instead of dicts)
            if main_array:
                main_array = [item for item in main_array if isinstance(item, dict)]
            
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
                file_items = 0
                for parent_item in main_array:
                    nested_items = parent_item.pop(nested_array_key, [])
                    parent_context = self.flattener.flatten_item(parent_item)
                    
                    for nested_item in nested_items:
                        nested_row = self.flattener.flatten_item(nested_item)
                        row = {**context, **parent_context, **nested_row}
                        rows.append(row)
                        file_items += 1
                
                if self.debug:
                    print(f"  Extracted {file_items} nested items from {len(main_array)} parent items")
            else:
                # Standard: one row per main array item
                file_items = len(main_array)
                for item in main_array:
                    row = {**context, **self.flattener.flatten_item(item)}
                    rows.append(row)
                
                if self.debug:
                    print(f"  Extracted {file_items} items")
        
        # Create DataFrame and deduplicate
        df = pd.DataFrame(rows)
        df = self.flattener.normalize_units(df)
        
        # Normalize state names to 2-letter abbreviations for consistency
        normalize_state_column(df, "State")
        
        # Track stats
        self.total_items = len(df)
        
        # Row-level deduplication (schema-driven, universal)
        df_before = len(df)
        df = self.deduplicator.deduplicate(df)
        self.duplicates_removed = df_before - len(df)
        
        if self.verbose and self.duplicates_removed > 0:
            key_fields = self.schema_metadata.metadata.get('consolidation', {}).get('deduplication', {}).get('key_fields', [])
            print(f"\n🔄 Removed {self.duplicates_removed} duplicate(s) based on key fields: {key_fields}")
        
        if self.debug:
            print(f"\n[DEBUG] DataFrame info:")
            print(f"  Shape: {df.shape[0]} rows × {df.shape[1]} columns")
            print(f"  Memory usage: {df.memory_usage(deep=True).sum() / 1024:.1f} KB")
            print(f"  Column dtypes:")
            for col, dtype in df.dtypes.items():
                print(f"    {col}: {dtype}")

        # Apply schema-driven column exclusion (optional)
        df = self._apply_exclude_fields(df)

        return df, self.schema_info
    
    def _apply_exclude_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove columns specified in schema output.exclude_fields.
        
        This is schema-driven and universal - any schema can specify columns to hide.
        """
        exclude_fields = self.schema_metadata.metadata.get("consolidation", {}).get("output", {}).get("exclude_fields", [])
        
        if not exclude_fields:
            return df
        
        # Only drop fields that actually exist in the DataFrame
        cols_to_drop = [col for col in exclude_fields if col in df.columns]
        
        if cols_to_drop:
            if self.verbose:
                print(f"\n🚫 Excluding {len(cols_to_drop)} column(s): {', '.join(cols_to_drop)}")
            df = df.drop(columns=cols_to_drop)
        
        return df
    
    def save_excel(self, df: pd.DataFrame, output_path: Path):
        """Save DataFrame to Excel with professional formatting."""
        self.excel_formatter.save(df, output_path)
    
    def save_csv(self, df: pd.DataFrame, output_path: Path):
        """Save DataFrame to CSV."""
        self.csv_exporter.save(df, output_path)
