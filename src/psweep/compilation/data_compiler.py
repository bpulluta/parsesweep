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


EXCEL_MAX_DATA_ROWS = 1_048_575
EXCEL_MAX_CELL_CHARACTERS = 32_767


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
        from .input_provenance import list_compile_record_files

        json_files = list_compile_record_files(json_dir)
        if not json_files:
            print(f"No JSON files found in {json_dir}")
            return pd.DataFrame(), {}

        # Structure is schema-owned; do not parse a sample record twice.
        self.schema_info = self.detector.detect_structure({})

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

        for idx, json_file in enumerate(json_files, 1):
            if self.debug:
                print(
                    f"\n[DEBUG] Processing file {idx}/{self.file_count}: {json_file.name}"
                )

            with open(json_file) as f:
                raw_data = json.load(f)

            # Canonical extraction-record contract stores extractable data in payload.
            if isinstance(raw_data, dict) and isinstance(raw_data.get("payload"), dict):
                data = raw_data["payload"]
                context_source = data
            else:
                continue

            # Extract identifier/context fields
            context = self.detector.extract_context(
                context_source, self.schema_info
            )
            context.update(self._extract_lineage_context(raw_data))

            # Refuse malformed records instead of silently dropping their rows.
            main_key = self.schema_info["main_array_key"]
            main_array = data.get(main_key, [])
            if not isinstance(main_array, list) or any(
                not isinstance(item, dict) for item in main_array
            ):
                raise ValueError(
                    f"{json_file}: {main_key} must be an array of objects"
                )

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
                # Preserve child rows and parents with no children.
                file_items = 0
                for parent_item in main_array:
                    nested_items = parent_item.get(nested_array_key, [])
                    parent_context = self.flattener.flatten_item({
                        key: value for key, value in parent_item.items()
                        if key != nested_array_key
                    })

                    if not nested_items:
                        rows.append({**context, **parent_context})
                        file_items += 1
                        continue
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
        del rows
        # Declared optional identity fields may be absent in every record;
        # represent them as null, but leave misspelled/unknown keys to fail.
        identity_fields = (
            self.schema_metadata.get_deduplication_key_fields()
            + self.schema_metadata.get_deduplication_partition_fields()
        )
        from ..utils.item_matcher import map_key_fields_to_columns

        for field in identity_fields:
            if map_key_fields_to_columns(df, [field], warn_on_missing=False):
                continue
            try:
                self.schema_metadata.resolve_schema_field(field)
            except SchemaMetadataError:
                continue
            df[self.flattener.make_column_name(field.split(".")[-1])] = None
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
        renames = self.schema_metadata.get_column_renames()
        type_map = {
            renames.get(field, renames.get(self.flattener.make_column_name(field), field)): types
            for field, types in self.schema_metadata.get_field_types().items()
        }
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
            display = self.flattener.make_column_name(field)
            output_field = column_renames.get(
                field, column_renames.get(display, self.schema_metadata._to_snake_case(field))
            )
            if output_field not in prepared_df.columns:
                prepared_df[output_field] = pd.NA

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

    @staticmethod
    def validate_export(df: pd.DataFrame, formats) -> None:
        """Reject workbook overflow before either writer touches output."""
        if "excel" not in formats:
            return
        if len(df) > EXCEL_MAX_DATA_ROWS:
            raise ValueError(
                f"Excel supports at most {EXCEL_MAX_DATA_ROWS:,} data rows; "
                "select CSV for this batch. No export was written."
            )
        for column in df.columns:
            if any(
                isinstance(value, str) and len(value) > EXCEL_MAX_CELL_CHARACTERS
                for value in df[column]
            ):
                raise ValueError(
                    f"Excel cells support at most {EXCEL_MAX_CELL_CHARACTERS:,} characters "
                    f"(column {column}); select CSV to preserve evidence. No export was written."
                )

    def export(self, df, output_dir: Path, base_name: str, formats, *, fresh=False):
        """Prepare once, preflight, then publish from a staging dir with rollback.

        All requested writers run inside a fresh temporary stage directory beside
        the real output (same parent, so promotion is a local rename). The stage
        holds a ``files/`` subdir (writer output) and a ``backup/`` subdir (live
        files moved aside during publication); every backup name is stage-owned,
        so nothing in or around the output dir is ever overwritten by a fixed
        sentinel name. Nothing live is touched until every writer has succeeded:

        - ``fresh``: the finished ``files/`` dir replaces the whole output
          directory in one rename; the prior directory is kept in ``backup/`` and
          restored if the rename fails.
        - non-fresh: each finished file is promoted into place, moving any file
          it overwrites into ``backup/`` first; a failed promotion rolls the
          already-promoted files back. Unrelated files are never disturbed.

        A caught writer or publish failure leaves prior output and unrelated
        files as they were and leaves no staging directory behind. In the rare
        case that a rollback cannot restore a file it moved aside, the stage root
        (holding that backup) is kept and the raised error reports where it is,
        rather than claiming the output is unchanged.

        Limitations: this is staged publication with rollback, not a crash-safe
        transaction. Writers run before any live file is touched, but publication
        is a sequence of renames — a process/power crash mid-publish, or a
        concurrent writer in the same output dir, can leave a brief or partial
        state; there is no locking.
        """
        import shutil
        import tempfile

        frame = self._prepare_output_dataframe(df)
        self.validate_export(frame, formats)
        output_dir = Path(output_dir)
        plan = []
        if "csv" in formats:
            plan.append((f"{base_name}.csv", self.save_csv))
        if "excel" in formats:
            plan.append((f"{base_name}.xlsx", self.save_excel))

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        stage_root = Path(
            tempfile.mkdtemp(dir=output_dir.parent, prefix=".psweep-export-")
        )
        files_dir = stage_root / "files"
        backup_dir = stage_root / "backup"
        files_dir.mkdir()
        backup_dir.mkdir()
        # Populated only if a failed rollback leaves a prior file recoverable
        # solely from its stage-owned backup; while non-empty the stage root is
        # kept so those files survive.
        preserved: list[str] = []
        try:
            staged = []
            for filename, writer in plan:
                staged_path = files_dir / filename
                writer(frame, staged_path, prepared=True)
                staged.append((filename, staged_path))
            if fresh:
                emitted = self._publish_fresh(
                    files_dir, backup_dir, output_dir, staged, preserved
                )
            else:
                emitted = self._publish_in_place(
                    backup_dir, output_dir, staged, preserved
                )
        except (OSError, ValueError) as exc:
            if preserved:
                raise ValueError(
                    "Export failed; rollback incomplete. Recovery details: "
                    f"{'; '.join(preserved)}. {exc}"
                ) from exc
            raise ValueError(
                f"Export failed; prior output unchanged. {exc}"
            ) from exc
        finally:
            if not preserved:
                shutil.rmtree(stage_root, ignore_errors=True)
        return emitted, frame

    @staticmethod
    def _publish_fresh(files_dir, backup_dir, output_dir, staged, preserved):
        """Swap the staged files dir in for the whole output dir, restoring on failure."""
        backup = None
        if output_dir.exists():
            backup = backup_dir / "old"
            output_dir.replace(backup)
        try:
            files_dir.replace(output_dir)
        except OSError:
            if backup is not None:
                try:
                    backup.replace(output_dir)
                except OSError:
                    # The backup is now the only copy of the prior output;
                    # record it so the caller keeps the stage root.
                    preserved.append(f"{output_dir.name} -> {backup}")
            raise
        return [output_dir / filename for filename, _ in staged]

    @staticmethod
    def _publish_in_place(backup_dir, output_dir, staged, preserved):
        """Promote each staged file into the output dir with per-file rollback."""
        output_dir.mkdir(parents=True, exist_ok=True)
        promoted = []  # (final_path, backup_path_or_None)
        try:
            for index, (filename, staged_path) in enumerate(staged):
                final_path = output_dir / filename
                backup_path = None
                if final_path.exists():
                    backup_path = backup_dir / f"{index}-{filename}"
                    final_path.replace(backup_path)
                # Record before promoting so a failed promotion still restores
                # the backup we just moved aside.
                promoted.append((final_path, backup_path))
                staged_path.replace(final_path)
        except OSError:
            # Undo each promotion. Both steps (remove our file, restore the
            # backup) can fail; wrap each so one failure can't abort the rest,
            # and keep any backup we could not restore from (preserved => stage
            # root is retained, so the prior file survives there).
            for final_path, backup_path in reversed(promoted):
                try:
                    final_path.unlink(missing_ok=True)
                    if backup_path is not None:
                        backup_path.replace(final_path)
                except OSError:
                    if backup_path is not None and backup_path.exists():
                        preserved.append(f"{final_path.name} -> {backup_path}")
                    else:
                        preserved.append(f"new output could not be removed: {final_path}")
            raise
        return [final_path for final_path, _ in promoted]

    def save_excel(self, df: pd.DataFrame, output_path: Path, *, prepared=False):
        """Save DataFrame to Excel with professional formatting."""
        prepared_df = df if prepared else self._prepare_output_dataframe(df)
        if not prepared:
            self.validate_export(prepared_df, ["excel"])
        self.excel_formatter.save(
            prepared_df,
            output_path,
            freeze_columns=self.schema_metadata.get_freeze_columns(),
            auto_width=self.schema_metadata.get_auto_width(),
            center_align_columns=self.schema_metadata.get_center_align_columns(),
            center_align_max_length=self.schema_metadata.get_center_align_max_length(),
        )

    def save_csv(self, df: pd.DataFrame, output_path: Path, *, prepared=False):
        """Save DataFrame to CSV."""
        prepared_df = df if prepared else self._prepare_output_dataframe(df)
        self.csv_exporter.save(prepared_df, output_path)
