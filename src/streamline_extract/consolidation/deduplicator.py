#!/usr/bin/env python3
"""
Row deduplication for consolidated data.

Intelligently removes truly duplicate rows while preserving unique data.
"""
import pandas as pd
from typing import List


class Deduplicator:
    """
    Remove duplicate rows from consolidated data.
    
    Intelligently identifies and removes rows that are truly identical
    in all identifying fields, while excluding metadata columns from
    comparison and preserving the most complete row from each duplicate group.
    """
    
    def deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate rows using all columns except metadata.
        
        Only removes rows that are truly identical in all identifying fields.
        Excludes metadata columns (Notes, section references, etc.) from comparison.
        
        Args:
            df: DataFrame to deduplicate
            
        Returns:
            DataFrame with duplicates removed
            
        Example:
            >>> dedup = Deduplicator()
            >>> clean_df = dedup.deduplicate(df)
        """
        if df.empty:
            return df
        
        # Ensure Notes column exists
        if 'Notes' not in df.columns:
            df['Notes'] = ''
        
        # Exclude metadata/non-identifying columns from duplicate detection
        # These are added by consolidator or are references, not identifying data
        exclude_cols = self._get_metadata_columns(df)
        
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
        rows_to_drop = self._process_duplicate_groups(df, duplicates, compare_cols)
        
        # Remove duplicates
        df = df.drop(index=rows_to_drop)
        
        if rows_to_drop:
            print(f"  ✓ Removed {len(rows_to_drop)} duplicate entries")
        else:
            print("  ✓ No duplicates found")
        
        return df
    
    def _get_metadata_columns(self, df: pd.DataFrame) -> List[str]:
        """
        Identify metadata columns that should be excluded from duplicate detection.
        
        Args:
            df: DataFrame to analyze
            
        Returns:
            List of column names that are metadata
        """
        exclude_cols = [col for col in df.columns 
                       if any(kw in col.lower() for kw in 
                             ['notes', 'section', 'location', 'tariff location'])]
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
