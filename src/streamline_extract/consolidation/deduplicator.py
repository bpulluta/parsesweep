#!/usr/bin/env python3
"""
Row deduplication for consolidated data.

Intelligently removes truly duplicate rows while preserving unique data.
"""
import pandas as pd
from typing import List, Optional
import logging

from ..utils.exceptions import SchemaMetadataError

logger = logging.getLogger(__name__)


class Deduplicator:
    """
    Remove duplicate rows from consolidated data.
    
    Uses schema metadata to intelligently identify which fields to
    ignore during deduplication (v2.0+ requires metadata).
    
    Intelligently identifies and removes rows that are truly identical
    in all identifying fields, while excluding metadata columns from
    comparison and preserving the most complete row from each duplicate group.
    """
    
    def __init__(self, schema_metadata):
        """
        Initialize deduplicator.
        
        Args:
            schema_metadata: SchemaMetadata instance (required in v2.0+)
            
        Raises:
            SchemaMetadataError: If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "Deduplicator requires schema metadata.\n"
                "Schema metadata is required as of StreamlineExtract v2.0."
            )
        self.schema_metadata = schema_metadata
    
    def deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate rows using key_fields from schema metadata.
        
        Uses schema-defined key_fields to identify duplicates. Two rows are
        duplicates if they match on all key_fields, regardless of differences
        in other fields (like State formatting, Section references, etc.).
        
        Args:
            df: DataFrame to deduplicate
            
        Returns:
            DataFrame with duplicates removed
            
        Example:
            >>> dedup = Deduplicator(schema_metadata)
            >>> clean_df = dedup.deduplicate(df)
        """
        if df.empty:
            return df
        
        # Ensure Notes column exists
        if 'Notes' not in df.columns:
            df['Notes'] = ''
        
        # Get key fields from schema metadata (v2.0+ requirement)
        key_fields = self.schema_metadata.get_deduplication_key_fields()
        
        if not key_fields:
            print("  ⚠ No key_fields defined in schema - skipping deduplication")
            return df
        
        # Map schema key_fields to actual DataFrame columns (case-insensitive)
        compare_cols = self._map_key_fields_to_columns(df, key_fields)
        
        if not compare_cols:
            print(f"  ⚠ None of the key_fields {key_fields} found in DataFrame - skipping deduplication")
            return df
        
        # Find exact duplicates (all compare columns must match)
        duplicates = df[df.duplicated(subset=compare_cols, keep=False)]
        if duplicates.empty:
            print("  ✓ No duplicates found")
            return df
        
        # Process each duplicate group
        rows_to_drop = self._process_duplicate_groups(df, duplicates, compare_cols)
        
        # Remove duplicates
        df = df.drop(index=rows_to_drop)
        
        if rows_to_drop:
            print(f"  ✓ Removed {len(rows_to_drop)} duplicate entries")
        else:
            print("  ✓ No duplicates found")
        
        return df
    
    def _map_key_fields_to_columns(self, df: pd.DataFrame, key_fields: List[str]) -> List[str]:
        """
        Map schema key_fields to actual DataFrame columns.
        
        Handles case variations and common naming patterns.
        For nested fields like "jurisdiction.state", looks for "State" column.
        Handles transformations like "specific_subject" -> "Specific Subject".
        
        Args:
            df: DataFrame to map columns from
            key_fields: Key field names from schema
            
        Returns:
            List of actual DataFrame column names that match key_fields
        """
        mapped_cols = []
        
        for key_field in key_fields:
            # Handle nested field names (e.g., "jurisdiction.state" -> "state")
            field_name = key_field.split('.')[-1]
            
            # Normalize field name for comparison:
            # Convert snake_case to space-separated: "specific_subject" -> "specific subject"
            normalized_field = field_name.replace('_', ' ').lower()
            
            # Find matching column (case-insensitive, with/without underscores)
            matching_col = None
            for col in df.columns:
                normalized_col = col.replace('_', ' ').lower()
                if normalized_col == normalized_field:
                    matching_col = col
                    break
            
            if matching_col:
                mapped_cols.append(matching_col)
            else:
                logger.warning(f"Key field '{key_field}' not found in DataFrame columns")
        
        return mapped_cols
    
    def _get_metadata_columns(self, df: pd.DataFrame) -> List[str]:
        """
        Identify metadata columns that should be excluded from duplicate detection.
        
        Uses schema metadata (required in v2.0+).
        
        Args:
            df: DataFrame to analyze
            
        Returns:
            List of column names that are metadata
        """
        # Use metadata-specified ignore fields (required in v2.0+)
        ignore_fields = set(self.schema_metadata.get_deduplication_ignore_fields())
        
        # Match column names (case-insensitive)
        exclude_cols = [
            col for col in df.columns
            if any(field.lower() in col.lower() for field in ignore_fields)
        ]
        
        return exclude_cols
    
    def _process_duplicate_groups(
        self, 
        df: pd.DataFrame, 
        duplicates: pd.DataFrame, 
        compare_cols: List[str]
    ) -> List[int]:
        """
        Process groups of duplicate rows and identify which to drop.
        
        For each group of duplicates:
        - Keep the most complete row (most non-null values)
        - Add a note to the kept row about merged duplicates
        - Mark other rows for deletion
        
        Args:
            df: Full DataFrame
            duplicates: Subset of duplicate rows
            compare_cols: Columns used for comparison
            
        Returns:
            List of row indices to drop
        """
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
        
        return rows_to_drop
