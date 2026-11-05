"""Compile extraction records into one analysis-ready table."""

import hashlib
import json
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import pandas as pd
from jsonschema import ValidationError

from .csv_exporter import CsvExporter
from .data_flattener import DataFlattener, to_cell
from .deduplicator import (
    COL_ARTIFACT_ID,
    COL_ERROR_CATEGORIES,
    COL_ERROR_COUNT,
    COL_ERROR_MESSAGES,
    COL_RUN_ID,
    PROVENANCE_COLUMNS,
    Deduplicator,
)
from .excel_formatter import ExcelFormatter
from .input_provenance import list_compile_record_files, read_compile_record
from .roster import compile_roster
from psweep.discovery.targets import iter_targets
from psweep.exceptions import SchemaMetadataError
from psweep.extraction.roster import load_roster
from psweep.extraction.schema_utils import build_schema_validator
from psweep.utils.file_io import artifact_sha256
from psweep.utils.normalizers import add_county_fips_column, normalize_state_column


EXCEL_MAX_DATA_ROWS = 1_048_575
EXCEL_MAX_CELL_CHARACTERS = 32_767


def apply_value_normalizations(df: pd.DataFrame, metadata) -> pd.DataFrame:
    """Rows-mode value normalization (units, state names) on field-name columns."""
    df = DataFlattener(metadata).normalize_units(df)
    state_column = metadata.get_state_normalization_column()
    if state_column:
        normalize_state_column(df, state_column)
    return df


class DataCompiler:
    """Roster mode emits one row per discovery target; otherwise one row per item.

    ``inputs`` are the resolved compile inputs; ``targets``/``partition_by``
    activate roster mode exactly as in extraction.
    """

    def __init__(
        self, schema_metadata, inputs: dict[str, Any] | None = None, verbose=True, debug=False
    ):
        if not schema_metadata:
            raise SchemaMetadataError("DataCompiler requires schema metadata.")
        self.schema_metadata = schema_metadata
        self.inputs = inputs or {}
        self.roster = load_roster(self.inputs, schema_metadata)
        self.roster_stats: dict[str, int] | None = None
        self.verbose = verbose
        self.debug = debug
        self.file_count = 0
        self.total_items = 0
        self.duplicates_removed = 0
        self.flattener = DataFlattener(schema_metadata)
        self.deduplicator = Deduplicator(schema_metadata=schema_metadata)
        self.excel_formatter = ExcelFormatter()
        self.csv_exporter = CsvExporter()
        self.payload_validator = build_schema_validator(schema_metadata.schema)

    @property
    def mode(self) -> str:
        """Compilation mode: ``roster`` when a roster is configured, else ``rows``."""
        return "rows" if self.roster is None else "roster"

    def fingerprint(self, json_dir: Path) -> str:
        """Build fingerprint: schema bytes, compile overlay, roster targets, record bytes."""
        json_dir = Path(json_dir)
        compilation = dict(self.schema_metadata.metadata.get("compilation") or {})
        output = {
            key: value
            for key, value in (compilation.pop("output", None) or {}).items()
            if key != "default_format"
        }
        if output:
            compilation["output"] = output
        roster = None
        if self.roster is not None:
            targets = hashlib.sha256()
            for row in iter_targets(self.inputs["targets"]):
                targets.update(json.dumps(row, sort_keys=True, default=str).encode() + b"\n")
            roster = {"fields": list(self.roster.fields), "targets_sha256": targets.hexdigest()}
        identity = {
            "schema_sha256": artifact_sha256(self.schema_metadata.schema_path),
            "compilation": compilation,
            "roster": roster,
            "records": [
                [path.relative_to(json_dir).as_posix(), artifact_sha256(path)]
                for path in list_compile_record_files(json_dir)
            ],
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _records(self, files: list[Path]) -> Iterator[dict[str, Any]]:
        for path in files:
            record = read_compile_record(path)
            try:
                self.payload_validator.validate(record["payload"])
            except ValidationError as exc:
                field = ".".join(str(part) for part in exc.absolute_path) or "payload"
                raise ValueError(
                    f"{path}: invalid extraction payload at {field} ({exc.validator})"
                ) from exc
            yield record

    def compile_from_directory(
        self,
        json_dir: Path,
        *,
        apply_deduplication: bool = True,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Return the compiled frame (field-name columns) and a schema summary."""
        from psweep.security import validate_protected_paths

        validate_protected_paths({"input_dir": json_dir})
        files = list_compile_record_files(json_dir)
        if not files:
            raise ValueError(f"No extraction records found in {json_dir}")
        self.file_count = len(files)
        main = self.schema_metadata.get_main_data_array()
        schema_info = {
            "type": self.schema_metadata.metadata.get("extraction", {}).get(
                "document_type", "Document"
            ),
            "main_array_key": main,
        }
        if self.verbose:
            print(f"📋 {schema_info['type']}: {main} ({self.mode} mode)")

        if self.roster is not None:
            df, self.roster_stats = compile_roster(
                self._records(files), self.schema_metadata, self.roster, self.inputs["targets"]
            )
            self.total_items = len(df)
            self.duplicates_removed = 0
            return df, schema_info

        df = self._item_rows(files, main)
        for field in (
            self.schema_metadata.get_deduplication_key_fields()
            + self.schema_metadata.get_deduplication_partition_fields()
        ):
            column = field.split(".")[-1]
            if column not in df.columns and column in self.schema_metadata.get_output_fields():
                df[column] = None
        df = apply_value_normalizations(df, self.schema_metadata)

        self.total_items = len(df)
        self.duplicates_removed = 0
        if apply_deduplication:
            before = len(df)
            df = self.deduplicator.deduplicate(df)
            self.duplicates_removed = before - len(df)
            if self.verbose and self.duplicates_removed:
                print(f"\n🔄 Removed {self.duplicates_removed} duplicate(s)")
        if self.debug:
            print(f"\n[DEBUG] Shape: {df.shape[0]} rows × {df.shape[1]} columns")  # noqa: RUF001
        return df, schema_info

    def _item_rows(self, files: list[Path], main: str) -> pd.DataFrame:
        row_array = self.schema_metadata.get_row_array()
        contexts = self.schema_metadata.get_context_objects()
        flatten = self.flattener.flatten_item
        rows = []
        for path, record in zip(files, self._records(files), strict=True):
            payload = record["payload"]
            context = {
                key: to_cell(value)
                for name in contexts
                for key, value in (payload.get(name) or {}).items()
            }
            context.update(self._lineage_context(record))
            items = payload.get(main, [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError(f"{path}: {main} must be an array of objects")
            for item in items:
                if not row_array:
                    rows.append({**context, **flatten(item)})
                    continue
                parent = {
                    **context,
                    **flatten({k: v for k, v in item.items() if k != row_array}),
                }
                children = item.get(row_array) or []
                if not children:
                    rows.append(parent)
                for child in children:
                    child_row = flatten(child)
                    overlap = set(child_row) & set(parent)
                    if overlap:
                        raise ValueError(
                            f"{path}: nested row columns collide with parent/context: "
                            f"{', '.join(sorted(overlap))}; use distinct schema field names"
                        )
                    rows.append({**parent, **child_row})
        return pd.DataFrame(rows)

    @staticmethod
    def _lineage_context(record: dict[str, Any]) -> dict[str, Any]:
        lineage = record.get("lineage") if isinstance(record.get("lineage"), dict) else {}
        context: dict[str, Any] = {}
        if lineage.get("run_id"):
            context[COL_RUN_ID] = lineage["run_id"]
        if lineage.get("artifact_id"):
            context[COL_ARTIFACT_ID] = lineage["artifact_id"]
        quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
        errors = [e for e in quality.get("errors") or [] if isinstance(e, dict)]
        if errors:
            context[COL_ERROR_COUNT] = len(errors)
            context[COL_ERROR_CATEGORIES] = "; ".join(
                sorted({error.get("category", "internal") for error in errors})
            )
            context[COL_ERROR_MESSAGES] = " | ".join(
                error.get("message", "Unknown error") for error in errors
            )
        return context

    def _coerce_schema_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """Unify missing values as ``pd.NA``; parse purely numeric fields; whole floats → Int64."""
        out = df.replace("", pd.NA)
        numeric = {"number", "integer"}
        for column, types in self.schema_metadata.get_field_types().items():
            if column in out.columns and types & numeric and not types - (numeric | {"null"}):
                out[column] = pd.to_numeric(out[column], errors="coerce")
        for column in out.columns:
            series = out[column]
            if pd.api.types.is_float_dtype(series):
                non_null = series.dropna()
                if len(non_null) and (non_null == non_null.round()).all():
                    out[column] = series.astype("Int64")
        return out

    def _prepare_output_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Shape the export: schema-ordered columns, FIPS, title headers, exclude, order.

        ``exclude_fields`` and ``column_order`` name final output headers exactly.
        """
        metadata = self.schema_metadata
        out = df.copy()
        if self.roster is None:
            fields = metadata.get_output_fields()
            out = out.reindex(columns=fields + [c for c in out.columns if c not in fields])
            out = self._coerce_schema_types(out)
        fips = metadata.get_county_fips_columns()
        if fips:
            add_county_fips_column(out, fips["state"], fips["county"])
        out = out.rename(columns=metadata.get_column_titles())
        duplicated = sorted(set(out.columns[out.columns.duplicated()]))
        if duplicated:
            raise ValueError(f"Duplicate output headers: {', '.join(duplicated)}")

        exclude = metadata.get_output_exclude_fields()
        order = metadata.get_column_order()
        known = set(out.columns) | PROVENANCE_COLUMNS | {self.deduplicator._annotation_col}  # noqa: SLF001 - package-internal helper
        unknown = [name for name in [*exclude, *order] if name not in known]
        if unknown:
            raise ValueError(
                "compilation.output exclude_fields/column_order name unknown output "
                f"header(s): {', '.join(unknown)}"
            )
        out = out.drop(columns=[name for name in exclude if name in out.columns])
        first = [name for name in order if name in out.columns]
        return out[first + [name for name in out.columns if name not in first]]

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
        """Serialize writers and recover interrupted output publication first."""
        from psweep.compilation.publication import recover_export
        from psweep.utils.file_io import publication_lock
        from psweep.security import validate_protected_paths

        output_dir = Path(output_dir)
        validate_protected_paths({"output_dir": output_dir})
        if output_dir.is_symlink():
            raise ValueError("Compilation output must not be a symbolic link")
        output_dir = output_dir.resolve()
        if output_dir in {Path.cwd(), Path.home(), Path(output_dir.anchor)}:
            raise ValueError("Compilation requires a dedicated output directory")
        if not base_name or Path(base_name).name != base_name or base_name in {".", ".."}:
            raise ValueError("Compilation base name must be a filename, not a path")
        with publication_lock(output_dir.parent / f".{output_dir.name}-publication.lock"):
            recover_export(output_dir)
            return self._export_locked(df, output_dir, base_name, formats, fresh=fresh)

    def _export_locked(self, df, output_dir: Path, base_name: str, formats, *, fresh=False):
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

        A persistent journal lets the next locked writer recover process
        interruption. Readers must still wait for successful completion;
        multiple output files are not an atomic read snapshot. Power-loss
        durability and distributed-filesystem locks require site validation.
        """
        import shutil
        import tempfile
        from psweep.compilation.publication import (
            prepare_export,
            commit_export,
            journal_path,
            recover_export,
        )

        frame = self._prepare_output_dataframe(df)
        self.validate_export(frame, formats)
        output_dir = Path(output_dir)
        plan = []
        if "csv" in formats:
            plan.append((f"{base_name}.csv", self.save_csv))
        if "excel" in formats:
            plan.append((f"{base_name}.xlsx", self.save_excel))
        if not plan:
            raise ValueError("At least one export format is required")

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        stage_root = Path(tempfile.mkdtemp(dir=output_dir.parent, prefix=".psweep-export-"))
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
            prepare_export(output_dir, stage_root, [name for name, _ in staged], fresh)
            if fresh:
                emitted = self._publish_fresh(files_dir, backup_dir, output_dir, staged, preserved)
            else:
                emitted = self._publish_in_place(backup_dir, output_dir, staged, preserved)
            commit_export(output_dir)
        except (OSError, ValueError) as exc:
            if journal_path(output_dir).exists():
                recover_export(output_dir)
            if preserved:
                raise ValueError(
                    "Export failed; rollback incomplete. Recovery details: "
                    f"{'; '.join(preserved)}. {exc}"
                ) from exc
            raise ValueError(f"Export failed; prior output unchanged. {exc}") from exc
        finally:
            if not preserved and not journal_path(output_dir).exists() and stage_root.exists():
                shutil.rmtree(stage_root)
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
