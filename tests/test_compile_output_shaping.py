"""Universal, schema-driven output shaping in DataCompiler._prepare_output_dataframe.

These lock the Phase-A tidiness foundation (domain-agnostic, no field-name
hardcoding): schema-type coercion, unified missing values, the stable
schema-projection column set, and normalized (case/format-insensitive) exclude
matching. Everything is driven by the schema, so the same behavior holds for any
domain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from psweep.compilation.data_compiler import DataCompiler
from psweep.utils.schema_metadata import SchemaMetadata


def _schema(path: Path, *, exclude=None) -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "Test",
            "version": "1.0.0",
            "extraction": {
                "main_data_array": "items",
                "context_objects": ["ctx"],
                "identifier_fields": ["ctx.id"],
                "document_type": "Test",
            },
            "identity": {"deduplication": {"key_fields": ["feature"]}},
            "compilation": {"output": {"exclude_fields": exclude or []}},
        },
        "type": "object",
        "properties": {
            "ctx": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    # Numeric-but-string-allowed (like year): downcast via float rule
                    "year": {"type": ["integer", "string", "null"]},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "feature": {"type": "string"},
                        # Mixed by schema (carries codes/times) -> NOT coerced
                        "value": {"type": ["number", "string", "null"]},
                        # Pure numeric -> coerced
                        "range_low": {"type": ["number", "null"]},
                        "applicable_values": {"type": ["array", "null"]},
                        "reasoning": {"type": "string"},
                        # Declared but never emitted -> reindex adds it
                        "missing_field": {"type": "string"},
                    },
                },
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema), encoding="utf-8")


def _compiler(tmp_path: Path, *, exclude=None) -> DataCompiler:
    sp = tmp_path / "schema.json"
    _schema(sp, exclude=exclude)
    return DataCompiler(
        schema_metadata=SchemaMetadata(sp), verbose=False, debug=False
    )


def _prep(tmp_path: Path, rows, *, exclude=None) -> pd.DataFrame:
    # Input columns are the flattener's humanized names (pre-rename).
    return _compiler(tmp_path, exclude=exclude)._prepare_output_dataframe(
        pd.DataFrame(rows)
    )


def test_get_field_types_reads_schema_types(tmp_path: Path):
    sp = tmp_path / "schema.json"
    _schema(sp)
    types = SchemaMetadata(sp).get_field_types()
    assert types["value"] == {"number", "string", "null"}
    assert types["range_low"] == {"number", "null"}
    assert types["year"] == {"integer", "string", "null"}
    assert types["feature"] == {"string"}


def test_pure_numeric_field_is_coerced_but_mixed_value_is_not(tmp_path: Path):
    out = _prep(
        tmp_path,
        [
            {"Feature": "a", "Value": "500", "Range Low": "", "Year": 2012},
            {"Feature": "b", "Value": "07:00", "Range Low": "100", "Year": 2013},
        ],
    )
    # range_low (pure numeric) -> numeric; blank -> NA
    assert pd.api.types.is_integer_dtype(out["range_low"])  # Int64 after downcast
    assert out["range_low"].tolist() == [pd.NA, 100]
    # value allows string in schema -> left untouched (07:00 survives)
    assert "07:00" in out["value"].tolist()


def test_integer_valued_float_column_downcast_kills_2012_point_0(tmp_path: Path):
    out = _prep(
        tmp_path,
        [
            {"Feature": "a", "Year": 2012},
            {"Feature": "b", "Year": None},  # forces float promotion pre-fix
        ],
    )
    assert str(out["year"].dtype) == "Int64"
    assert out["year"].tolist() == [2012, pd.NA]  # no 2012.0


def test_empty_strings_unified_to_na(tmp_path: Path):
    out = _prep(tmp_path, [{"Feature": "a", "Value": ""}])
    assert out["value"].isna().all()  # "" became NA, not literal ""


def test_reindex_adds_declared_but_unemitted_field(tmp_path: Path):
    out = _prep(tmp_path, [{"Feature": "a", "Value": "1"}])
    assert "missing_field" in out.columns  # schema field surfaced as a column
    assert out["missing_field"].isna().all()


def test_exclude_matches_normalized_name_regardless_of_casing(tmp_path: Path):
    # Config says "Reasoning"; the column is "reasoning" after rename -> excluded.
    out = _prep(
        tmp_path, [{"Feature": "a", "Reasoning": "because"}], exclude=["Reasoning"]
    )
    assert "reasoning" not in out.columns


def test_array_field_preserved_as_its_own_column(tmp_path: Path):
    # applicable_values arrives pre-joined from the flattener; stays its own col.
    out = _prep(
        tmp_path,
        [{"Feature": "a", "Value": "", "Applicable Values": "A, B"}],
    )
    assert out["applicable_values"].tolist() == ["A, B"]
    assert out["value"].isna().all()  # value stays empty/numeric, not overloaded
