#!/usr/bin/env python3
"""
Universal DataCompiler - Works with ANY schema automatically.

Clean, simple, and intelligent. No configuration needed.
"""

import json
import pandas as pd
from pathlib import Path
from typing import Dict, Any, Tuple
from .schema_detector import SchemaDetector
from .data_flattener import DataFlattener
from .deduplicator import (
    Deduplicator,
    COL_RUN_ID,
    COL_ARTIFACT_ID,
    COL_ERROR_COUNT,
    COL_ERROR_CATEGORIES,
    COL_ERROR_MESSAGES,
)
from .excel_formatter import ExcelFormatter
from .csv_exporter import CsvExporter
from ..exceptions import SchemaMetadataError
from ..utils.normalizers import normalize_state_column


class DataCompiler:
    """
    Universal data compiler that automatically handles any extraction schema.

    Simply point it at extracted JSON files and it:

    - Detects schema structure automatically
    - Intelligently flattens nested data for spreadsheets
    - Creates readable, analysis-ready output
    - Handles both simple and complex nested structures

    No configuration required - just works.
    """

    def __init__(self, schema_metadata, verbose=True, debug=False):
        """
        Initialize compiler.

        Parameters
        ----------
        schema_metadata
            SchemaMetadata instance (required in v2.0+)
        verbose
            Whether to print informational messages (default: True)
        debug
            Whether to print debug-level details (default: False)

        Raises
        ------
        SchemaMetadataError
            If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "DataCompiler requires schema metadata.\n"
                "Schema metadata is required as of ParseSweep v2.0."
            )

        self.schema_info = None
        self.schema_metadata = schema_metadata
        self.verbose = verbose
        self.debug = debug
        self.file_count = 0
        self.total_items = 0
        self.duplicates_removed = 0
        self.detector = SchemaDetector(schema_metadata=schema_metadata)
        self.flattener = DataFlattener(schema_metadata=schema_metadata)
        self.deduplicator = Deduplicator(schema_metadata=schema_metadata)
        self.excel_formatter = ExcelFormatter()
        self.csv_exporter = CsvExporter()

    def compile_from_directory(
        self,
        json_dir: Path,
        *,
        apply_deduplication: bool = True,
    ) -> Tuple[pd.DataFrame, Dict]:
        """
        Load all JSON files and compile into DataFrame.

        Searches recursively through nested subdirectories.

        Returns
        -------
        Tuple[pd.DataFrame, Dict]
            Tuple of (DataFrame, schema_info dict)
        """
        # Always search recursively so partitioned/nested extraction outputs are
        # treated identically to flat outputs.
        # Deliberately skip validation/ and run_manifests/ sidecars.
        _excluded = {"validation", "run_manifests", "qa_qc"}
        json_files = [
            path
            for path in json_dir.rglob("*.json")
            if not _excluded.intersection(path.relative_to(json_dir).parts)
        ]
        if not json_files:
            print(f"No JSON files found in {json_dir}")
            return pd.DataFrame(), {}

        # Analyze first canonical extraction-record file to understand schema structure.
        sample_data = None
        for sample_file in sorted(json_files):
            with open(sample_file) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(
                loaded.get("payload"), dict
            ):
                sample_data = loaded["payload"]
                break

        if sample_data is None:
            if self.verbose:
                print(
                    f"No canonical extraction-record files found in {json_dir}"
                )
            return pd.DataFrame(), {}

        # Detect schema type and structure
        self.schema_info = self.detector.detect_structure(sample_data)

        if self.verbose:
            print(f"📋 Schema detected: {self.schema_info['type']}")
            print(f"   Main entity: {self.schema_info['main_array_key']}")
            print(
                f"   Identifier fields: {', '.join(self.schema_info['id_fields'])}"
            )

        if self.debug:
            print("\n[DEBUG] Full schema metadata:")
            metadata = self.schema_metadata.metadata
            print(f"  Domain: {metadata.get('domain', 'N/A')}")
            print(f"  Version: {metadata.get('version', 'N/A')}")
            if "extraction" in metadata:
                print("  Extraction config:")
                for key, value in metadata["extraction"].items():
                    print(f"    {key}: {value}")
            if "identity" in metadata and "deduplication" in metadata["identity"]:
                dedup = metadata["identity"]["deduplication"]
                print("  Deduplication config:")
                print(f"    Key fields: {dedup.get('key_fields', [])}")
                print(f"    Ignore fields: {dedup.get('ignore_fields', [])}")

        # Extract data from all files
        rows = []
        self.file_count = len(json_files)

        for idx, json_file in enumerate(sorted(json_files), 1):
            if self.debug:
                print(
                    f"\n[DEBUG] Processing file {idx}/{self.file_count}: {json_file.name}"
                )

            with open(json_file) as f:
                raw_data = json.load(f)

            # Canonical extraction-record contract stores extractable data in payload.
            if "payload" in raw_data and isinstance(raw_data["payload"], dict):
                data = raw_data["payload"]
                context_source = data
            else:
                continue

            # Extract identifier/context fields
            context = self.detector.extract_context(
                context_source, self.schema_info
            )
            context.update(self._extract_lineage_context(raw_data))

            # Extract main array items
            main_array = data.get(self.schema_info["main_array_key"], [])

            # Filter out invalid items (e.g., strings instead of dicts)
            if main_array:
                main_array = [
                    item for item in main_array if isinstance(item, dict)
                ]

            # Check if items have a nested array that should be flattened
            # Pattern: parent_array → nested_array (e.g., rate_schedules → charges)
            nested_array_key = None
            if main_array and isinstance(main_array[0], dict):
                # Find first array field in the item (if any)
                for key, value in main_array[0].items():
                    if (
                        isinstance(value, list)
                        and value
                        and isinstance(value[0], dict)
                    ):
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
                    print(
                        f"  Extracted {file_items} nested items from {len(main_array)} parent items"
                    )
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

        # Normalize state names to 2-letter abbreviations for consistency.
        # Config-gated: defaults to a column named "State" (legacy behavior);
        # a schema can point at a different column or opt out entirely.
        state_column = self.schema_metadata.get_state_normalization_column()
        if state_column:
            normalize_state_column(df, state_column)

        # Track stats
        self.total_items = len(df)

        # Row-level deduplication (schema-driven, universal)
        self.duplicates_removed = 0
        if apply_deduplication:
            df_before = len(df)
            df = self.deduplicator.deduplicate(df)
            self.duplicates_removed = df_before - len(df)

            if self.verbose and self.duplicates_removed > 0:
                key_fields = (
                    self.schema_metadata.metadata.get("identity", {})
                    .get("deduplication", {})
                    .get("key_fields", [])
                )
                print(
                    f"\n🔄 Removed {self.duplicates_removed} duplicate(s) based on key fields: {key_fields}"
                )

        if self.debug:
            print("\n[DEBUG] DataFrame info:")
            print(f"  Shape: {df.shape[0]} rows × {df.shape[1]} columns")
            print(
                f"  Memory usage: {df.memory_usage(deep=True).sum() / 1024:.1f} KB"
            )
            print("  Column dtypes:")
            for col, dtype in df.dtypes.items():
                print(f"    {col}: {dtype}")

        return df, self.schema_info

    def _extract_lineage_context(
        self, raw_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Extract lineage context fields from extraction wrappers for compiled outputs."""
        context: Dict[str, Any] = {}

        lineage = (
            raw_data.get("lineage")
            if isinstance(raw_data.get("lineage"), dict)
            else None
        )
        if not lineage:
            return context

        run_id = lineage.get("run_id")
        artifact_id = lineage.get("artifact_id")

        if run_id:
            context[COL_RUN_ID] = run_id
        if artifact_id:
            context[COL_ARTIFACT_ID] = artifact_id

        quality = (
            raw_data.get("quality")
            if isinstance(raw_data.get("quality"), dict)
            else {}
        )
        errors = (
            quality.get("errors")
            if isinstance(quality.get("errors"), list)
            else []
        )
        if errors:
            context[COL_ERROR_COUNT] = len(errors)
            context[COL_ERROR_CATEGORIES] = "; ".join(
                sorted(
                    {
                        error.get("category", "internal")
                        for error in errors
                        if isinstance(error, dict)
                    }
                )
            )
            context[COL_ERROR_MESSAGES] = " | ".join(
                error.get("message", "Unknown error")
                for error in errors
                if isinstance(error, dict)
            )

        return context

    def _apply_exclude_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Remove columns named in schema output.exclude_fields.

        Schema-driven and universal. Matching is case/format-insensitive: both the
        configured names and the DataFrame columns are normalized to snake_case
        before comparison, so a field is named once regardless of casing and there
        is no need to list both "Applicable Values" and "applicable_values".
        """
        exclude_fields = self.schema_metadata.get_output_exclude_fields()

        if not exclude_fields:
            return df

        norm = self.schema_metadata._to_snake_case
        excluded = {norm(str(name)) for name in exclude_fields}
        cols_to_drop = [col for col in df.columns if norm(str(col)) in excluded]

        if cols_to_drop:
            if self.verbose:
                print(
                    f"\n🚫 Excluding {len(cols_to_drop)} column(s): {', '.join(cols_to_drop)}"
                )
            df = df.drop(columns=cols_to_drop)

        return df

    def _coerce_schema_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """Coerce columns to schema-declared dtypes and unify missing values.

        Domain-agnostic and schema-driven — no field name is hardcoded:

        - Unify missing: empty strings become ``pd.NA`` so the frame has a single
          missing-value representation (item nulls were ``""``, context nulls were
          ``NaN``).
        - Fields the schema types as purely numeric (``number``/``integer``,
          optionally ``null``, but NOT ``string``) are parsed with
          ``pd.to_numeric`` so they load as real numbers instead of
          object-with-blanks. Fields that also allow ``string`` (e.g. a value
          axis still carrying codes/times pre-Tier-2) are left untouched.
        - Any float column whose non-null values are all whole numbers is
          downcast to nullable ``Int64`` — universally kills the ``2012.0``
          year artifact (an int column promoted to float by a single null).
        """
        out = df.replace("", pd.NA)
        type_map = self.schema_metadata.get_field_types()
        numeric = {"number", "integer"}
        for col in out.columns:
            types = type_map.get(col)
            if types and (types & numeric) and not (types - (numeric | {"null"})):
                out[col] = pd.to_numeric(out[col], errors="coerce")
        for col in out.columns:
            series = out[col]
            if pd.api.types.is_float_dtype(series):
                non_null = series.dropna()
                if len(non_null) and (non_null == non_null.round()).all():
                    out[col] = series.astype("Int64")
        return out

    def _prepare_output_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply schema-driven output shaping before saving exports.

        Ordering is deliberate: rename to the schema's canonical (snake_case)
        names first so every later step works in one namespace, then coerce to
        schema-declared dtypes, then exclude, then order. Universal and
        config-light — no per-domain or per-field special-casing.
        """
        prepared_df = df.copy()

        # 1. Canonical rename first (auto snake_case + explicit overrides).
        column_renames = self.schema_metadata.get_column_renames()
        if column_renames:
            prepared_df = prepared_df.rename(
                columns={
                    column: renamed
                    for column, renamed in column_renames.items()
                    if column in prepared_df.columns
                }
            )

        # 2. Reindex to the full schema projection so the column SET is stable
        #    and predictable across runs (a field the schema declares but a run
        #    didn't emit appears as an empty column, instead of vanishing). Extra
        #    non-schema columns (lineage) are preserved; excludes trim later.
        schema_fields = (
            self.schema_metadata._schema_item_properties()
            + self.schema_metadata._schema_context_properties()
        )
        for field in schema_fields:
            if field not in prepared_df.columns:
                prepared_df[field] = pd.NA

        # 3. Coerce to schema dtypes + unify missing values.
        prepared_df = self._coerce_schema_types(prepared_df)

        # 3b. Optional GIS/DS join key: derive county_fips from state + county
        #     (opt-in; only meaningful for county-level geography).
        if self.schema_metadata.get_derive_county_fips():
            from ..utils.normalizers import add_county_fips_column

            add_county_fips_column(
                prepared_df, "state", "county", "county_fips"
            )

        # 4. Exclude fields (normalized name match, so casing/format is moot).
        prepared_df = self._apply_exclude_fields(prepared_df)

        # 5. Column order (priority prefix; remainder in schema order).
        column_order = self.schema_metadata.get_column_order()
        if column_order:
            ordered_columns = [
                column
                for column in column_order
                if column in prepared_df.columns
            ]
            remaining_columns = [
                column
                for column in prepared_df.columns
                if column not in ordered_columns
            ]
            prepared_df = prepared_df[ordered_columns + remaining_columns]

        return prepared_df

    def save_excel(self, df: pd.DataFrame, output_path: Path):
        """Save DataFrame to Excel with professional formatting."""
        prepared_df = self._prepare_output_dataframe(df)
        self.excel_formatter.save(
            prepared_df,
            output_path,
            freeze_columns=self.schema_metadata.get_freeze_columns(),
            auto_width=self.schema_metadata.get_auto_width(),
            center_align_columns=self.schema_metadata.get_center_align_columns(),
            center_align_max_length=self.schema_metadata.get_center_align_max_length(),
        )

    def save_csv(self, df: pd.DataFrame, output_path: Path):
        """Save DataFrame to CSV."""
        prepared_df = self._prepare_output_dataframe(df)
        self.csv_exporter.save(prepared_df, output_path)
