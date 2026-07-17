#!/usr/bin/env python3
"""
Row deduplication for consolidated data.

Intelligently removes truly duplicate rows while preserving unique data.
"""

import pandas as pd
from typing import Any, Dict, List, Optional
import logging

from ..utils.exceptions import SchemaMetadataError
from ..utils.item_matcher import map_key_fields_to_columns

logger = logging.getLogger(__name__)


PROVENANCE_COLUMNS = frozenset(
    {
        "Run Id",
        "Artifact Id",
        "Error Count",
        "Error Categories",
        "Error Messages",
        "Notes",
    }
)

HIGH_SEVERITY_TOKENS = frozenset(
    {
        "value",
        "unit",
        "amount",
        "rate",
        "cost",
        "price",
        "capacity",
        "limit",
        "minimum",
        "maximum",
        "fee",
        "charge",
        "quantity",
        "output",
    }
)

MEDIUM_SEVERITY_TOKENS = frozenset(
    {
        "category",
        "type",
        "subject",
        "classification",
        "period",
        "season",
        "term",
        "description",
        "details",
        "section",
    }
)


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
        self.field_severity_hints = (
            schema_metadata.get_consolidation_field_severity_hints()
        )

    def deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate rows using key_fields from schema metadata.

        Uses schema-defined key_fields to identify duplicates. Two rows are
        duplicates if they match on all key_fields, regardless of differences
        in other fields (like State formatting, Section references, etc.).

        Supports fuzzy matching: treats empty/null values in optional fields
        (like 'condition') as wildcards that match any value.

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
        if "Notes" not in df.columns:
            df["Notes"] = ""

        # Get key fields from schema metadata (v2.0+ requirement)
        key_fields = self.schema_metadata.get_deduplication_key_fields()

        if not key_fields:
            print(
                "  ⚠ No key_fields defined in schema - skipping deduplication"
            )
            return df

        # Map schema key_fields to actual DataFrame columns (case-insensitive)
        compare_cols = map_key_fields_to_columns(df, key_fields)

        if not compare_cols:
            print(
                f"  ⚠ None of the key_fields {key_fields} found in DataFrame - skipping deduplication"
            )
            return df

        # Find duplicates using fuzzy matching logic
        duplicate_groups = self._collect_duplicate_groups(df, compare_cols)
        rows_to_drop = self._apply_duplicate_groups(df, duplicate_groups)

        # Remove duplicates
        df = df.drop(index=rows_to_drop)

        if rows_to_drop:
            print(f"  ✓ Removed {len(rows_to_drop)} duplicate entries")
        else:
            print("  ✓ No duplicates found")

        return df

    def preview_deduplication(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Preview deduplication effects without mutating or writing outputs."""
        preview_df = df.copy(deep=True)
        if "Notes" not in preview_df.columns:
            preview_df["Notes"] = ""

        key_fields = self.schema_metadata.get_deduplication_key_fields()
        if not key_fields:
            return {
                "key_fields": [],
                "compare_columns": [],
                "duplicate_groups": [],
                "duplicates_removed": 0,
                "warnings": ["No key_fields defined in schema metadata"],
            }

        compare_cols = map_key_fields_to_columns(preview_df, key_fields)
        if not compare_cols:
            return {
                "key_fields": key_fields,
                "compare_columns": [],
                "duplicate_groups": [],
                "duplicates_removed": 0,
                "warnings": [
                    f"None of the key_fields {key_fields} were found in the consolidated rows"
                ],
            }

        duplicate_groups = self._collect_duplicate_groups(
            preview_df, compare_cols
        )
        duplicates_removed = sum(
            len(group["drop_indices"]) for group in duplicate_groups
        )
        excluded_columns = self._get_preview_excluded_columns(
            preview_df, compare_cols
        )
        suspicious_groups = []
        severity_counts = {"high": 0, "medium": 0, "low": 0}

        preview_groups = []
        for group in duplicate_groups:
            conflicts = self._collect_conflicting_columns(
                preview_df,
                group["duplicate_indices"],
                excluded_columns,
            )
            severity = self._classify_conflict_severity(conflicts.keys())
            group_preview = {
                "keep_index": group["keep_idx"],
                "drop_indices": list(group["drop_indices"]),
                "merged_count": len(group["drop_indices"]),
                "note": group["note"],
                "sample_values": {},
                "suspicious": bool(conflicts),
                "severity": severity,
                "conflicting_columns": list(conflicts.keys()),
                "conflicts": conflicts,
            }
            for col in compare_cols:
                if col in preview_df.columns:
                    group_preview["sample_values"][col] = preview_df.at[
                        group["keep_idx"], col
                    ]
            preview_groups.append(group_preview)

            if conflicts:
                severity_counts[severity] += 1
                suspicious_groups.append(
                    {
                        "keep_index": group["keep_idx"],
                        "drop_indices": list(group["drop_indices"]),
                        "severity": severity,
                        "conflicting_columns": list(conflicts.keys()),
                        "conflicts": conflicts,
                    }
                )

        warnings = []
        if suspicious_groups:
            warnings.append(
                "Potentially unsafe deduplication: "
                f"{len(suspicious_groups)} duplicate group(s) matched on key_fields but disagree on other populated columns"
            )

        return {
            "key_fields": key_fields,
            "compare_columns": compare_cols,
            "duplicate_groups": preview_groups,
            "duplicates_removed": duplicates_removed,
            "warnings": warnings,
            "suspicious_groups": suspicious_groups,
            "suspicious_groups_count": len(suspicious_groups),
            "suspicious_groups_by_severity": severity_counts,
        }

    def _classify_conflict_severity(self, conflicting_columns) -> str:
        """Rank suspicious conflicts so schema authors can prioritize fixes."""
        schema_derived_severity = self._classify_schema_derived_severity(
            conflicting_columns
        )
        if schema_derived_severity:
            return schema_derived_severity

        normalized_columns = {
            column.lower().replace("_", " ") for column in conflicting_columns
        }

        if any(
            any(token in column for token in HIGH_SEVERITY_TOKENS)
            for column in normalized_columns
        ):
            return "high"

        if any(
            any(token in column for token in MEDIUM_SEVERITY_TOKENS)
            for column in normalized_columns
        ):
            return "medium"

        return "low"

    def _classify_schema_derived_severity(
        self, conflicting_columns
    ) -> Optional[str]:
        """Use schema field definitions to rank conflicts before falling back to name heuristics."""
        severity_order = {"low": 0, "medium": 1, "high": 2}
        highest_severity: Optional[str] = None

        for column in conflicting_columns:
            normalized_column = column.lower().replace(" ", "_")
            hinted_severity = self.field_severity_hints.get(normalized_column)
            if hinted_severity is None:
                continue

            if (
                highest_severity is None
                or severity_order[hinted_severity]
                > severity_order[highest_severity]
            ):
                highest_severity = hinted_severity

        return highest_severity

    def _get_preview_excluded_columns(
        self, df: pd.DataFrame, compare_cols: List[str]
    ) -> set[str]:
        """Return columns that should not trigger suspicious dedup warnings."""
        excluded_columns = set(compare_cols)
        excluded_columns.update(PROVENANCE_COLUMNS)
        excluded_columns.update(self._get_metadata_columns(df))

        identifier_fields = self.schema_metadata.get_identifier_fields()
        excluded_columns.update(
            map_key_fields_to_columns(
                df, identifier_fields, warn_on_missing=False
            )
        )
        return excluded_columns

    def _collect_conflicting_columns(
        self,
        df: pd.DataFrame,
        row_indices: List[int],
        excluded_columns: set[str],
    ) -> Dict[str, List[str]]:
        """Find populated non-key columns whose values disagree across a duplicate group."""
        conflicts: Dict[str, List[str]] = {}

        for column in df.columns:
            if column in excluded_columns:
                continue

            values = []
            seen = set()
            for row_index in row_indices:
                raw_value = df.at[row_index, column]
                if pd.isna(raw_value):
                    continue

                value = str(raw_value).strip()
                if not value or value in seen:
                    continue

                seen.add(value)
                values.append(value)

            if len(values) > 1:
                conflicts[column] = values[:3]

        return conflicts

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
        ignore_fields = set(
            self.schema_metadata.get_deduplication_ignore_fields()
        )

        # Match column names (case-insensitive)
        exclude_cols = [
            col
            for col in df.columns
            if any(field.lower() in col.lower() for field in ignore_fields)
        ]

        return exclude_cols

    def _collect_duplicate_groups(
        self, df: pd.DataFrame, compare_cols: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Find and process duplicates using fuzzy matching logic.

        Fuzzy matching treats empty/null values in optional fields (like 'Condition')
        as wildcards that can match any value. For example:
        - Row A: category="Height limit", condition=""
        - Row B: category="Height limit", condition="total height"
        These are considered duplicates (A's empty condition matches B's filled condition).

        IMPORTANT: Deduplication only happens within the same jurisdiction.
        Different jurisdictions are never merged, even if requirements are identical.

        Args:
            df: Full DataFrame
            compare_cols: Columns to compare for duplicates

        Returns:
            List of row indices to drop
        """
        checked = set()
        duplicate_groups: List[Dict[str, Any]] = []

        # Optional fields that support fuzzy matching (empty = wildcard)
        optional_fuzzy_fields = {"Condition", "Applies To", "Applies_To"}
        fuzzy_cols = [
            col for col in compare_cols if col in optional_fuzzy_fields
        ]
        required_cols = [
            col for col in compare_cols if col not in optional_fuzzy_fields
        ]

        # Add jurisdiction columns to ensure we never merge across jurisdictions
        jurisdiction_cols = []
        for field in [
            "State",
            "County",
            "City",
            "Municipality",
            "Jurisdiction",
        ]:
            if field in df.columns:
                jurisdiction_cols.append(field)

        # Combine jurisdiction + required fields for grouping
        grouping_cols = jurisdiction_cols + required_cols

        # Group by jurisdiction + required fields
        for _, group in df.groupby(grouping_cols, dropna=False):
            if len(group) <= 1:
                continue

            # Within each group, find fuzzy duplicates
            indices = group.index.tolist()

            for i, idx1 in enumerate(indices):
                if idx1 in checked:
                    continue

                duplicate_group = [idx1]

                for idx2 in indices[i + 1 :]:
                    if idx2 in checked:
                        continue

                    # Check if fuzzy fields match (empty matches anything)
                    is_duplicate = True
                    for col in fuzzy_cols:
                        val1 = df.at[idx1, col]
                        val2 = df.at[idx2, col]

                        # Normalize values
                        v1_empty = pd.isna(val1) or str(val1).strip() == ""
                        v2_empty = pd.isna(val2) or str(val2).strip() == ""

                        # Empty values match anything (wildcard)
                        if v1_empty or v2_empty:
                            continue

                        # Non-empty values must match exactly
                        if val1 != val2:
                            is_duplicate = False
                            break

                    if is_duplicate:
                        duplicate_group.append(idx2)

                # Process this duplicate group
                if len(duplicate_group) > 1:
                    for idx in duplicate_group:
                        checked.add(idx)

                    # Keep the most complete row
                    completeness = {
                        idx: sum(
                            1
                            for col in df.columns
                            if pd.notna(df.at[idx, col])
                            and str(df.at[idx, col]).strip()
                        )
                        for idx in duplicate_group
                    }
                    keep_idx = max(completeness, key=completeness.get)
                    drop_indices = [
                        idx for idx in duplicate_group if idx != keep_idx
                    ]

                    note = self._generate_merge_note(
                        df,
                        keep_idx,
                        duplicate_group,
                        required_cols,
                        fuzzy_cols,
                    )
                    duplicate_groups.append(
                        {
                            "keep_idx": keep_idx,
                            "drop_indices": drop_indices,
                            "duplicate_indices": duplicate_group,
                            "required_cols": required_cols,
                            "fuzzy_cols": fuzzy_cols,
                            "note": note,
                        }
                    )

        return duplicate_groups

    def _apply_duplicate_groups(
        self,
        df: pd.DataFrame,
        duplicate_groups: List[Dict[str, Any]],
    ) -> List[int]:
        """Apply duplicate groups to a DataFrame, annotating kept rows and returning dropped indices."""
        rows_to_drop: List[int] = []
        for group in duplicate_groups:
            keep_idx = group["keep_idx"]
            drop_indices = group["drop_indices"]
            note = group["note"]

            rows_to_drop.extend(drop_indices)
            current_note = df.at[keep_idx, "Notes"]
            df.at[keep_idx, "Notes"] = (
                f"{current_note}; {note}" if current_note else note
            )

        return rows_to_drop

    def _generate_merge_note(
        self,
        df: pd.DataFrame,
        keep_idx: int,
        duplicate_indices: List[int],
        required_cols: List[str],
        fuzzy_cols: List[str],
    ) -> str:
        """
        Generate detailed merge note showing which fields matched.

        Args:
            df: Full DataFrame
            keep_idx: Index of row being kept
            duplicate_indices: All duplicate indices (including kept)
            required_cols: Required match columns
            fuzzy_cols: Fuzzy match columns

        Returns:
            Formatted note string
        """
        num_merged = len(duplicate_indices) - 1

        # Build field values string (limit to first 2 required fields for readability)
        field_parts = []
        for col in required_cols[:2]:
            val = df.at[keep_idx, col]
            if pd.notna(val) and str(val).strip():
                # Truncate long values
                val_str = str(val)
                if len(val_str) > 30:
                    val_str = val_str[:27] + "..."
                field_parts.append(f"{col}='{val_str}'")

        fields_str = (
            ", ".join(field_parts) if field_parts else "all key fields"
        )

        # Check if fuzzy matching occurred (any duplicate had different fuzzy field values)
        fuzzy_used = False
        if fuzzy_cols:
            for col in fuzzy_cols:
                values = set()
                for idx in duplicate_indices:
                    val = df.at[idx, col]
                    if pd.notna(val) and str(val).strip():
                        values.add(str(val))
                # Fuzzy matching occurred if we have multiple non-empty values OR mix of empty/non-empty
                if len(values) > 1 or (
                    len(values) == 1
                    and any(
                        pd.isna(df.at[idx, col])
                        or str(df.at[idx, col]).strip() == ""
                        for idx in duplicate_indices
                    )
                ):
                    fuzzy_used = True
                    break

        # Format note
        if fuzzy_used:
            return f"Merged {num_merged} duplicate(s) on: {fields_str} [fuzzy: {', '.join(fuzzy_cols)}]"
        else:
            return f"Merged {num_merged} duplicate(s) on: {fields_str}"
