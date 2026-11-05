"""Schema-driven output shaping in DataCompiler._prepare_output_dataframe.

Input frames carry schema field names; output headers are schema titles (else
field names). Coercion, the schema column set, and exact-header exclude/order
are all driven by the schema, so the same behavior holds for any domain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

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
                        "reasoning": {"type": "string", "title": "Reasoning"},
                        # Declared but never emitted -> reindex adds it
                        "missing_field": {"type": "string"},
                    },
                },
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema), encoding="utf-8")


def _schema_path(tmp_path: Path) -> Path:
    sp = tmp_path / "schema.json"
    _schema(sp)
    return sp


def _compiler(tmp_path: Path, *, exclude=None) -> DataCompiler:
    sp = tmp_path / "schema.json"
    _schema(sp, exclude=exclude)
    return DataCompiler(schema_metadata=SchemaMetadata(sp), verbose=False, debug=False)


def _prep(tmp_path: Path, rows, *, exclude=None) -> pd.DataFrame:
    return _compiler(tmp_path, exclude=exclude)._prepare_output_dataframe(pd.DataFrame(rows))


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
            {"feature": "a", "value": "500", "range_low": "", "year": 2012},
            {"feature": "b", "value": "07:00", "range_low": "100", "year": 2013},
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
            {"feature": "a", "year": 2012},
            {"feature": "b", "year": None},  # forces float promotion pre-fix
        ],
    )
    assert str(out["year"].dtype) == "Int64"
    assert out["year"].tolist() == [2012, pd.NA]  # no 2012.0


def test_empty_strings_unified_to_na(tmp_path: Path):
    out = _prep(tmp_path, [{"feature": "a", "value": ""}])
    assert out["value"].isna().all()  # "" became NA, not literal ""


def test_headers_come_from_schema_titles(tmp_path: Path):
    out = _prep(tmp_path, [{"feature": "a", "reasoning": "because"}])
    assert "Reasoning" in out.columns
    assert "reasoning" not in out.columns
    assert list(out.columns[:2]) == ["id", "year"]


def test_reindex_adds_declared_but_unemitted_field(tmp_path: Path):
    out = _prep(tmp_path, [{"feature": "a", "value": "1"}])
    assert "missing_field" in out.columns  # schema field surfaced as a column
    assert out["missing_field"].isna().all()


def test_exclude_and_order_use_exact_output_headers(tmp_path: Path):
    compiler = DataCompiler(
        SchemaMetadata(
            _schema_path(tmp_path),
            {
                "compilation": {
                    "output": {
                        "exclude_fields": ["Reasoning"],
                        "column_order": ["value", "feature"],
                    },
                }
            },
        ),
        verbose=False,
    )
    out = compiler._prepare_output_dataframe(
        pd.DataFrame([{"feature": "a", "value": "1", "reasoning": "because"}])
    )
    assert "Reasoning" not in out.columns
    assert list(out.columns[:2]) == ["value", "feature"]


@pytest.mark.parametrize("key", ["exclude_fields", "column_order"])
def test_unknown_output_header_is_rejected(tmp_path: Path, key):
    # The field name is not the header once a title is set.
    compiler = DataCompiler(
        SchemaMetadata(
            _schema_path(tmp_path),
            {
                "compilation": {
                    "output": {key: ["reasoning"]},
                }
            },
        ),
        verbose=False,
    )
    with pytest.raises(ValueError, match="unknown output header"):
        compiler._prepare_output_dataframe(pd.DataFrame([{"feature": "a"}]))


def test_array_field_preserved_as_its_own_column(tmp_path: Path):
    # applicable_values arrives pre-joined from the flattener; stays its own col.
    out = _prep(
        tmp_path,
        [{"feature": "a", "value": "", "applicable_values": "A, B"}],
    )
    assert out["applicable_values"].tolist() == ["A, B"]
    assert out["value"].isna().all()  # value stays empty/numeric, not overloaded


def _schema_with_fips(path: Path) -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "Test",
            "version": "1.0.0",
            "extraction": {
                "main_data_array": "items",
                "context_objects": ["ctx"],
                "document_type": "Test",
            },
            "identity": {"deduplication": {"key_fields": ["feature"]}},
            "compilation": {
                "normalization": {"county_fips": {"state": "state", "county": "county"}}
            },
        },
        "type": "object",
        "properties": {
            "ctx": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "state": {"type": "string"},
                    "county": {"type": "string"},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"feature": {"type": "string"}},
                },
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema), encoding="utf-8")


def test_county_fips_derived_when_enabled(tmp_path: Path):
    sp = tmp_path / "schema.json"
    _schema_with_fips(sp)
    compiler = DataCompiler(schema_metadata=SchemaMetadata(sp), verbose=False, debug=False)
    out = compiler._prepare_output_dataframe(
        pd.DataFrame(
            [
                {"state": "NV", "county": "Churchill", "feature": "a"},
                {"state": "CA", "county": "Imperial County", "feature": "b"},
            ]
        )
    )
    assert "county_fips" in out.columns
    assert out["county_fips"].tolist() == ["32001", "06025"]


def test_county_fips_absent_when_not_enabled(tmp_path: Path):
    # Default schema (no normalization.county_fips) -> no county_fips column.
    out = _prep(tmp_path, [{"feature": "a"}])
    assert "county_fips" not in out.columns
