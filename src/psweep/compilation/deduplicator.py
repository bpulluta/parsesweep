#!/usr/bin/env python3
"""
Row deduplication for compiled data.

Intelligently removes truly duplicate rows while preserving unique data.
"""

import pandas as pd
from typing import Any, Dict, List, Optional
import logging

from ..exceptions import SchemaMetadataError
from ..utils.item_matcher import map_key_fields_to_columns
from ..utils.schema_metadata import HIGH_SEVERITY_TOKENS, MEDIUM_SEVERITY_TOKENS

logger = logging.getLogger(__name__)


# Provenance / lineage column display names — single source of truth shared
# with data_compiler, which populates these columns from extraction lineage.
COL_RUN_ID = "Run Id"
COL_ARTIFACT_ID = "Artifact Id"
COL_ERROR_COUNT = "Error Count"
COL_ERROR_CATEGORIES = "Error Categories"
COL_ERROR_MESSAGES = "Error Messages"
COL_NOTES = "Notes"

PROVENANCE_COLUMNS = frozenset(
    {
        COL_RUN_ID,
        COL_ARTIFACT_ID,
        COL_ERROR_COUNT,
        COL_ERROR_CATEGORIES,
        COL_ERROR_MESSAGES,
        COL_NOTES,
    }
)


class Deduplicator:
    """
    Remove duplicate rows from compiled data.

    Uses schema metadata to intelligently identify which fields to
    ignore during deduplication (v2.0+ requires metadata).

    Intelligently identifies and removes rows that are truly identical
    in all identifying fields, while excluding metadata columns from
    comparison and preserving the most complete row from each duplicate group.
    """

    def __init__(self, schema_metadata):
        """
        Initialize deduplicator.

        Parameters
        ----------
        schema_metadata
            SchemaMetadata instance (required in v2.0+)

        Raises
        ------
        SchemaMetadataError
            If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "Deduplicator requires schema metadata.\n"
                "Schema metadata is required as of ParseSweep v2.0."
            )
        self.schema_metadata = schema_metadata
        self.field_severity_hints = (
            schema_metadata.get_compilation_field_severity_hints()
        )
        # Annotation column: configurable via compilation.output.annotation_column,
        # defaults to "Notes" which is the ParseSweep framework standard.
        output_cfg = schema_metadata.metadata.get("compilation", {}).get("output", {})
        self._annotation_col: str = output_cfg.get("annotation_column", "Notes")

    def deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove duplicate rows using key_fields from schema metadata.

        Uses schema-defined key_fields to identify duplicates. Two rows are
        duplicates if they match on all key_fields, regardless of differences
        in other fields (like State formatting, Section references, etc.).

        Supports fuzzy matching: treats empty/null values in optional fields
        (like 'condition') as wildcards that match any value.

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to deduplicate

        Returns
        -------
        pd.DataFrame
            DataFrame with duplicates removed

        Examples
        --------
        >>> dedup = Deduplicator(schema_metadata)
        >>> clean_df = dedup.deduplicate(df)
        """
        if df.empty:
            return df

        # Ensure annotation column exists
        if self._annotation_col not in df.columns:
            df[self._annotation_col] = ""

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

        # Apply semantic dedup: remove approval cross-feature redundancies
        # (e.g., zoning_districts row when permit_approval row exists for same applies_to+obligation)
        semantic_drops = self._deduplicate_approval_redundancy(df)
        if semantic_drops:
            df = df.drop(index=semantic_drops)
            total_dropped = len(rows_to_drop) + len(semantic_drops)
            print(
                f"  ✓ Removed {len(rows_to_drop)} key-field duplicates + {len(semantic_drops)} approval redundancies ({total_dropped} total)"
            )
        else:
            if rows_to_drop:
                print(f"  ✓ Removed {len(rows_to_drop)} duplicate entries")
            else:
                print("  ✓ No duplicates found")

        return df

    def preview_deduplication(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Preview deduplication effects without mutating or writing outputs."""
        preview_df = df.copy(deep=True)
        if self._annotation_col not in preview_df.columns:
            preview_df[self._annotation_col] = ""

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
                    f"None of the key_fields {key_fields} were found in the compiled rows"
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

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to analyze

        Returns
        -------
        List[str]
            List of column names that are metadata
        """
        # Use metadata-specified ignore fields (required in v2.0+)
        ignore_fields = set(
            self.schema_metadata.get_deduplication_ignore_fields()
        )

        # Match column names using normalized token equality (not substring containment).
        # Substring matching causes false positives: e.g. "type" would exclude "Rate Type".
        def _col_key(name: str) -> str:
            return name.lower().replace(" ", "_").replace("-", "_")

        ignore_keys = {_col_key(f) for f in ignore_fields}
        exclude_cols = [
            col for col in df.columns if _col_key(col) in ignore_keys
        ]

        return exclude_cols

    def _collect_duplicate_groups(
        self, df: pd.DataFrame, compare_cols: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Find duplicate row groups using schema-driven matching.

        Two rows are duplicates when they agree on all key fields (required +
        fuzzy).  Required fields use exact case-insensitive matching.  Fuzzy
        fields (declared in ``identity.deduplication.fuzzy_key_fields``) treat
        an empty/null value as a wildcard that matches any other value, and
        compare non-empty values case-insensitively.

        Partition fields (``identity.deduplication.partition_fields``) are
        prepended to the groupby key so rows from different partitions are
        never merged even when all content fields match.

        Parameters
        ----------
        df : pd.DataFrame
            Full DataFrame
        compare_cols : List[str]
            Compiled-output column names to compare (mapped from schema key_fields)

        Returns
        -------
        List[Dict[str, Any]]
            Duplicate groups with keep/drop indices and merge metadata
        """
        checked: set = set()
        duplicate_groups: List[Dict[str, Any]] = []

        # --- Schema-driven field classification ---
        # Fuzzy fields: schema declares which key fields use empty-as-wildcard matching.
        # All other compare_cols are required (exact case-insensitive match via groupby).
        raw_fuzzy = self.schema_metadata.get_deduplication_fuzzy_fields()
        fuzzy_field_cfg = self.schema_metadata.get_deduplication_fuzzy_field_config()
        fuzzy_col_names = map_key_fields_to_columns(df, raw_fuzzy) if raw_fuzzy else []
        # Build col -> config mapping (col names may differ from field names after mapping)
        raw_fuzzy_lower = {f.lower().replace(" ", "_"): cfg for f, cfg in fuzzy_field_cfg.items()}
        fuzzy_col_cfg: dict = {
            col: raw_fuzzy_lower.get(col.lower().replace(" ", "_"), {})
            for col in fuzzy_col_names
        }
        fuzzy_col_set = set(fuzzy_col_names)
        fuzzy_cols = [col for col in compare_cols if col in fuzzy_col_set]
        required_cols = [col for col in compare_cols if col not in fuzzy_col_set]

        # --- Schema-driven partition fields ---
        # Rows in different partitions are never merged (e.g. different counties).
        # Partitioning is explicit and schema-driven via
        # identity.deduplication.partition_fields.
        raw_partition = self.schema_metadata.get_deduplication_partition_fields()
        partition_col_names = map_key_fields_to_columns(df, raw_partition) if raw_partition else []
        partition_cols = [col for col in partition_col_names if col in df.columns]

        # --- Groupby: partition + required fields (all normalized to lowercase) ---
        grouping_cols = partition_cols + required_cols
        if not grouping_cols:
            return duplicate_groups

        df_norm = df.copy(deep=False)
        for col in grouping_cols:
            if col in df_norm.columns and df_norm[col].dtype == object:
                df_norm[col] = df_norm[col].map(
                    lambda value: (
                        str(value).strip().lower()
                        if value is not None and value == value
                        else ""
                    )
                )

        for _, group in df_norm.groupby(grouping_cols, dropna=False):
            if len(group) <= 1:
                continue

            indices = group.index.tolist()

            for i, idx1 in enumerate(indices):
                if idx1 in checked:
                    continue

                duplicate_group = [idx1]

                for idx2 in indices[i + 1:]:
                    if idx2 in checked:
                        continue

                    if self._fuzzy_fields_match(df, idx1, idx2, fuzzy_cols, fuzzy_col_cfg):
                        duplicate_group.append(idx2)

                if len(duplicate_group) > 1:
                    for idx in duplicate_group:
                        checked.add(idx)

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
                    drop_indices = [idx for idx in duplicate_group if idx != keep_idx]

                    note = self._generate_merge_note(
                        df, keep_idx, duplicate_group, required_cols, fuzzy_cols
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

    # Stop-words stripped when comparing fuzzy key-field *values* under the
    # "words" normalize mode (e.g. duplicate hour ranges). Deliberately narrow
    # and distinct from item_matcher.STOP_WORDS (name-token matching) and
    # value_normalizer._UNIT_STOPWORDS (measurement-unit relational words):
    # widening it would silently merge rows that differ in meaningful content.
    _WORDS_NORMALIZE_STOPWORDS = frozenset(
        {"a", "an", "the", "of", "in", "at", "by", "for", "to", "from", "hours"}
    )

    @classmethod
    def _fuzzy_fields_match(
        cls,
        df: pd.DataFrame,
        idx1: int,
        idx2: int,
        fuzzy_cols: List[str],
        fuzzy_col_cfg: Optional[dict] = None,
    ) -> bool:
        """Return True when all fuzzy fields are compatible between two rows.

        Compatibility rules (applied per field):
        - Either value empty/null → wildcard, always compatible.
        - Both non-empty → compare after applying the field's ``normalize`` mode:
          - ``"words"`` — lowercase, collapse whitespace, strip basic plural 's',
            remove common stop-words.  Generic text normalization; no domain
            vocabulary.  Handles phrasing like "between 7 AM and 7 PM" vs
            "Between the hours of 7 AM and 7 PM".
          - ``None`` (default) — exact case-insensitive equality after strip.
        """
        cfg = fuzzy_col_cfg or {}
        for col in fuzzy_cols:
            v1 = df.at[idx1, col]
            v2 = df.at[idx2, col]
            e1 = pd.isna(v1) or str(v1).strip() == ""
            e2 = pd.isna(v2) or str(v2).strip() == ""
            if e1 or e2:
                continue
            s1 = str(v1).strip().lower()
            s2 = str(v2).strip().lower()
            normalize = cfg.get(col, {}).get("normalize")
            if normalize == "words":
                sw = cls._WORDS_NORMALIZE_STOPWORDS
                s1 = " ".join(t.rstrip("s") for t in s1.split() if t not in sw)
                s2 = " ".join(t.rstrip("s") for t in s2.split() if t not in sw)
            if s1 != s2:
                return False
        return True

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
            current_note = df.at[keep_idx, self._annotation_col]
            df.at[keep_idx, self._annotation_col] = (
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

        Parameters
        ----------
        df : pd.DataFrame
            Full DataFrame
        keep_idx : int
            Index of row being kept
        duplicate_indices : List[int]
            All duplicate indices (including kept)
        required_cols : List[str]
            Required match columns
        fuzzy_cols : List[str]
            Fuzzy match columns

        Returns
        -------
        str
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

    def _deduplicate_approval_redundancy(self, df: pd.DataFrame) -> List[int]:
        """
        Semantic dedup: remove cross-feature redundancies (schema-driven).

        Entirely opt-in and domain-neutral. When a schema declares
        ``identity.deduplication.cross_feature_redundancy``, rows in the same
        ``group_by`` bucket whose ``feature_field`` values are all members of
        ``redundant_features`` are collapsed to the single most authoritative
        one (per ``authority_ranking``); the rest are dropped and the kept row
        is annotated. When the block is absent this is a no-op.

        For example, an ordinance schema may declare that a "zoning_districts"
        matrix row and a "permit_approval" row for the same applies_to +
        obligation encode the same approval gate, keeping the more explicit
        "permit_approval".

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame after key-field deduplication

        Returns
        -------
        List[int]
            Indices of rows to drop
        """
        rows_to_drop: List[int] = []

        cfg = self.schema_metadata.get_cross_feature_redundancy_config()
        if not cfg:
            return rows_to_drop

        group_by = cfg.get("group_by") or []
        feature_field = cfg.get("feature_field")
        authority = cfg.get("authority_ranking") or []
        redundant = set(cfg.get("redundant_features") or [])

        required_cols = set(group_by) | {feature_field}
        if not group_by or not feature_field or len(redundant) < 2:
            return rows_to_drop
        if any(col not in df.columns for col in required_cols):
            return rows_to_drop

        def _authority_rank(feature: str) -> int:
            return authority.index(feature) if feature in authority else len(authority)

        # Group once; iterate the grouped indices directly (no per-group mask
        # recompute over the full frame).
        for group_indices in df.groupby(group_by, dropna=False).groups.values():
            group_indices = list(group_indices)
            if len(group_indices) <= 1:
                continue

            features_in_group = df.loc[group_indices, feature_field].dropna().unique()
            redundant_present = [f for f in features_in_group if f in redundant]
            if len(redundant_present) < 2:
                continue

            sorted_features = sorted(redundant_present, key=_authority_rank)
            keep_feature = sorted_features[0]
            group_features = df.loc[group_indices, feature_field]
            keep_indices = [
                idx for idx in group_indices
                if group_features.at[idx] == keep_feature
            ]
            if not keep_indices:
                continue
            keep_idx = keep_indices[0]

            for drop_feature in sorted_features[1:]:
                for drop_idx in group_indices:
                    if group_features.at[drop_idx] != drop_feature:
                        continue
                    if drop_idx in keep_indices:
                        continue
                    rows_to_drop.append(drop_idx)
                    current_note = df.at[keep_idx, self._annotation_col]
                    annotation = (
                        f"Removed redundant '{drop_feature}' row "
                        f"(kept more authoritative '{keep_feature}')"
                    )
                    df.at[keep_idx, self._annotation_col] = (
                        f"{current_note}; {annotation}"
                        if current_note
                        else annotation
                    )

        return rows_to_drop
