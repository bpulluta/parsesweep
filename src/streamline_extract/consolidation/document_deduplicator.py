#!/usr/bin/env python3
"""
Document-level deduplication for consolidated data.

Identifies and removes duplicate extractions of the same source document.
This catches cases where the same ordinance/document was extracted multiple
times from different file formats or with different extraction approaches.
"""
import pandas as pd
from typing import List, Set
import logging

logger = logging.getLogger(__name__)


class DocumentDeduplicator:
    """
    Remove duplicate document extractions from consolidated data.
    
    Identifies when the same ordinance/document was extracted multiple times
    by grouping on jurisdiction + date + section patterns, then keeps the
    most complete extraction.
    """
    
    def __init__(self, schema_metadata):
        """
        Initialize document deduplicator.
        
        Args:
            schema_metadata: SchemaMetadata instance for schema-specific logic
        """
        self.schema_metadata = schema_metadata
    
    def deduplicate_documents(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate document extractions.
        
        Groups rows by jurisdiction identifiers (State, County, Date) to find
        documents that were likely extracted multiple times. Within each group,
        identifies document versions by their Section/Ordinance_Code patterns
        and keeps the most complete version.
        
        Conservative approach: Only merges when safety checks confirm identical content.
        Preserves potentially distinct documents to avoid data loss.
        
        Args:
            df: DataFrame with potentially duplicate documents
            
        Returns:
            DataFrame with duplicate documents removed
        """
        if df.empty:
            return df
        
        # Ensure Notes column exists
        if 'Notes' not in df.columns:
            df['Notes'] = ''
        
        # Identify grouping columns (jurisdiction identifiers)
        group_cols = self._get_grouping_columns(df)
        if not group_cols:
            print("  ⚠ Cannot identify jurisdiction columns - skipping document deduplication")
            return df
        
        # Find and process duplicate documents
        rows_to_drop, preserved_versions = self._find_duplicate_documents(df, group_cols)
        
        if rows_to_drop:
            df = df.drop(index=rows_to_drop)
            print(f"  ✓ Removed {len(rows_to_drop)} rows from duplicate document extractions")
        else:
            print("  ✓ No duplicate documents found")
        
        # Inform user about preserved document versions (potential manual review needed)
        if preserved_versions > 0:
            print(f"  ℹ Preserved {preserved_versions} document version(s) for safety - manual review recommended")
        
        return df
    
    def _get_grouping_columns(self, df: pd.DataFrame) -> List[str]:
        """
        Identify columns to group by for finding duplicate documents.
        
        Returns columns that identify a jurisdiction/document context
        (e.g., State, County, Date_Adopted).
        
        IMPORTANT: Includes multiple date fields to prevent merging historical versions.
        """
        group_cols = []
        
        # Common jurisdiction identifiers
        jurisdiction_fields = ['State', 'County', 'City', 'Municipality', 'Jurisdiction']
        for field in jurisdiction_fields:
            if field in df.columns:
                group_cols.append(field)
        
        # Date fields - include ALL to preserve historical tracking
        # Different dates = different ordinance versions, should NOT be merged
        date_fields = ['Date_Adopted', 'Date_Effective', 'Effective_Date', 'Adopted_Date']
        for field in date_fields:
            if field in df.columns:
                group_cols.append(field)
        
        # Also group by Supersedes to keep ordinance lineage separate
        if 'Supersedes' in df.columns:
            group_cols.append('Supersedes')
        
        return group_cols
    
    def _find_duplicate_documents(self, df: pd.DataFrame, group_cols: List[str]) -> tuple:
        """
        Find rows that belong to duplicate document extractions.
        
        Within each jurisdiction group, looks for different document versions
        (identified by Section or Ordinance_Code patterns) and keeps the most
        complete version.
        
        IMPORTANT: Only merges if documents have the SAME substantive content.
        Preserves historical versions, amendments, and ordinances with different values.
        
        Returns:
            Tuple of (rows_to_drop: List[int], preserved_version_groups: int)
        """
        rows_to_drop = []
        preserved_version_groups = 0
        
        # Group by jurisdiction
        for group_key, group in df.groupby(group_cols, dropna=False):
            if len(group) <= 1:
                continue
            
            # Look for different document versions within this group
            doc_versions = self._identify_document_versions(group)
            
            if len(doc_versions) <= 1:
                continue  # Only one document version
            
            # SAFETY CHECK: Verify versions have the same substantive content
            # Do NOT merge if they represent different ordinances or amendments
            if not self._are_versions_truly_duplicates(group, doc_versions):
                # Different versions with potentially different content - preserve both
                preserved_version_groups += 1
                continue
            
            # Find the most complete version to keep
            keep_version = self._select_best_version(group, doc_versions)
            
            # Mark other versions for deletion
            for version_id, indices in doc_versions.items():
                if version_id != keep_version:
                    rows_to_drop.extend(indices)
            
            # Add note to kept rows
            num_versions_removed = len(doc_versions) - 1
            for idx in doc_versions[keep_version]:
                note = f"Document-level dedup: removed {num_versions_removed} duplicate extraction(s)"
                current_note = df.at[idx, 'Notes']
                df.at[idx, 'Notes'] = f"{current_note}; {note}" if current_note else note
        
        return rows_to_drop, preserved_version_groups
    
    def _identify_document_versions(self, group: pd.DataFrame) -> dict:
        """
        Identify different document extraction versions within a group.
        
        Uses Section and Ordinance_Code patterns to distinguish versions.
        For example, "17.35.030" vs "§ 17.35.030" likely indicates two
        extractions of the same ordinance.
        
        Returns:
            Dict mapping version_id -> list of row indices
        """
        versions = {}
        
        # Use Section and Ordinance_Code to identify versions
        section_col = None
        ordinance_col = None
        
        for col in ['Section', 'Section_Number', 'Relevant_Sections']:
            if col in group.columns:
                section_col = col
                break
        
        for col in ['Ordinance_Code', 'Code', 'Title']:
            if col in group.columns:
                ordinance_col = col
                break
        
        if not section_col and not ordinance_col:
            # Can't identify versions without these fields
            return {0: group.index.tolist()}
        
        # Group by section/code patterns to identify document versions
        # Conservative approach: only normalize obvious formatting artifacts
        for idx, row in group.iterrows():
            # Create version signature
            signature_parts = []
            
            if section_col:
                section = str(row[section_col]) if pd.notna(row[section_col]) else ''
                # Keep section reference mostly as-is to avoid false merges
                # Different formats may indicate different source documents
                signature_parts.append(section.strip())
            
            if ordinance_col:
                code = str(row[ordinance_col]) if pd.notna(row[ordinance_col]) else ''
                # Different ordinance codes indicate different document sources
                signature_parts.append(code.strip() if code.strip() else '[EMPTY]')
            
            version_id = '|'.join(signature_parts)
            
            if version_id not in versions:
                versions[version_id] = []
            versions[version_id].append(idx)
        
        return versions
    
    def _select_best_version(self, group: pd.DataFrame, versions: dict) -> str:
        """
        Select which document version to keep.
        
        Prefers the version with:
        1. More total rows (more complete extraction)
        2. More non-null fields (better quality)
        
        Returns:
            version_id of the version to keep
        """
        version_scores = {}
        
        for version_id, indices in versions.items():
            version_rows = group.loc[indices]
            
            # Score based on completeness
            row_count = len(indices)
            avg_completeness = version_rows.apply(
                lambda row: sum(1 for v in row if pd.notna(v) and str(v).strip()),
                axis=1
            ).mean()
            
            # Combined score: prioritize row count, then completeness
            score = (row_count * 1000) + avg_completeness
            version_scores[version_id] = score
        
        # Return version with highest score
        return max(version_scores, key=version_scores.get)
    
    def _are_versions_truly_duplicates(self, group: pd.DataFrame, versions: dict) -> bool:
        """
        Verify that document versions are actually duplicates and safe to merge.
        
        Checks for material differences that would indicate these are NOT duplicates:
        - Different Date_Last_Amended (amendment vs original)
        - Different Supersedes values (one supersedes another)
        - Different requirement values (substantive changes)
        - Different requirement counts (one more complete than other for DIFFERENT reasons)
        
        Returns:
            True if versions are safe to merge (true duplicates), False otherwise
        """
        # Check 1: Different amendment dates = different ordinance versions
        if 'Date_Last_Amended' in group.columns:
            amendment_dates = set()
            for idx, row in group.iterrows():
                date = row['Date_Last_Amended']
                if pd.notna(date) and str(date).strip():
                    amendment_dates.add(str(date))
            
            if len(amendment_dates) > 1:
                # Different amendment dates = historical versions, DO NOT merge
                return False
        
        # Check 2: Different Supersedes values = related but distinct ordinances
        if 'Supersedes' in group.columns:
            supersedes_values = set()
            for idx, row in group.iterrows():
                val = row['Supersedes']
                if pd.notna(val) and str(val).strip():
                    supersedes_values.add(str(val))
            
            if len(supersedes_values) > 1:
                # Different supersedes = different ordinance lineage, DO NOT merge
                return False
        
        # Check 3: Compare substantive content between versions
        # If the same Category+Specific Subject has different Values, DO NOT merge
        if 'Category' in group.columns and 'Value' in group.columns:
            # Build signature of requirement -> value mapping for each version
            version_signatures = {}
            
            for version_id, indices in versions.items():
                version_rows = group.loc[indices]
                signature = {}
                
                for idx, row in version_rows.iterrows():
                    category = str(row.get('Category', ''))
                    specific = str(row.get('Specific Subject', ''))
                    value = str(row.get('Value', ''))
                    
                    if category or specific:  # Only check if we have identifying info
                        req_key = f"{category}|{specific}"
                        if pd.notna(row.get('Value')) and value.strip():
                            signature[req_key] = value
                
                version_signatures[version_id] = signature
            
            # Compare signatures - if same requirements have different values, DO NOT merge
            all_req_keys = set()
            for sig in version_signatures.values():
                all_req_keys.update(sig.keys())
            
            for req_key in all_req_keys:
                values_for_req = set()
                for sig in version_signatures.values():
                    if req_key in sig:
                        values_for_req.add(sig[req_key])
                
                # If the same requirement has different values across versions, DO NOT merge
                if len(values_for_req) > 1:
                    return False
        
        # Passed all safety checks - versions appear to be true duplicates
        return True
