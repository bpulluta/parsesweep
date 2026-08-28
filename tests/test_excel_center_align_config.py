"""Tests for config-driven Excel column centering.

The centering heuristic (short columns + a name list) is now overridable via
``compilation.output.center_align_columns`` / ``center_align_max_length``. When
neither is configured, behavior must be identical to the built-in defaults.
"""

import json

import pandas as pd
import pytest

from psweep.compilation.excel_formatter import (
    DEFAULT_CENTER_ALIGN_COLUMN_NAMES,
    DEFAULT_CENTER_ALIGN_MAX_LENGTH,
    ExcelFormatter,
)
from psweep.exceptions import SchemaMetadataError
from psweep.utils.schema_metadata import SchemaMetadata


def _df():
    """A frame with one long-text column and one short named column."""
    return pd.DataFrame(
        {
            "state": ["Virginia", "Texas"],
            "description": ["x" * 80, "y" * 80],
        }
    )


class TestFormatterDefaults:
    def test_default_centers_short_and_named_not_long(self):
        cols = ExcelFormatter()._identify_center_aligned_columns(_df())
        # "state" (short + named) centered = column A; "description" (long) not.
        assert cols == ["A"]

    def test_default_name_list_is_the_module_constant(self):
        # A long column whose name is in the default list is still centered.
        df = pd.DataFrame({"unit": ["z" * 80, "z" * 80]})
        assert "unit" in DEFAULT_CENTER_ALIGN_COLUMN_NAMES
        cols = ExcelFormatter()._identify_center_aligned_columns(df)
        assert cols == ["A"]


class TestFormatterOverrides:
    def test_explicit_columns_replace_default_names(self):
        # Override so only "description" is force-centered; "state" is short so
        # it still centers on length — use a long value to isolate the name list.
        df = pd.DataFrame(
            {
                "state": ["s" * 80, "s" * 80],  # long, was named-centered
                "description": ["d" * 80, "d" * 80],  # long, now force-centered
            }
        )
        cols = ExcelFormatter()._identify_center_aligned_columns(
            df, center_align_columns=["description"]
        )
        # "state" no longer in the (overridden) name list and is long -> not
        # centered; "description" is in the override -> centered.
        assert cols == ["B"]

    def test_empty_list_disables_name_based_centering(self):
        df = pd.DataFrame({"state": ["s" * 80, "s" * 80]})
        cols = ExcelFormatter()._identify_center_aligned_columns(
            df, center_align_columns=[]
        )
        assert cols == []

    def test_case_insensitive_name_match(self):
        df = pd.DataFrame({"Region": ["r" * 80, "r" * 80]})
        cols = ExcelFormatter()._identify_center_aligned_columns(
            df, center_align_columns=["REGION"]
        )
        assert cols == ["A"]

    def test_zero_threshold_disables_length_centering(self):
        df = pd.DataFrame({"short": ["a", "b"]})  # avg length 1
        cols = ExcelFormatter()._identify_center_aligned_columns(
            df, center_align_columns=[], center_align_max_length=0
        )
        assert cols == []

    def test_threshold_default_is_the_module_constant(self):
        assert DEFAULT_CENTER_ALIGN_MAX_LENGTH == 30.0


def _schema(output_block):
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "extraction": {
                "main_data_array": "items",
                "identifier_fields": ["id"],
            },
            "compilation": {"output": output_block},
        },
        "type": "object",
        "properties": {"items": {"type": "array"}},
    }


class TestSchemaMetadataCenterAlign:
    def test_accessors_none_when_unset(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps(_schema({})), encoding="utf-8")
        meta = SchemaMetadata(path)
        assert meta.get_center_align_columns() is None
        assert meta.get_center_align_max_length() is None

    def test_accessors_return_configured_values(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                _schema(
                    {
                        "center_align_columns": ["region", "status"],
                        "center_align_max_length": 15,
                    }
                )
            ),
            encoding="utf-8",
        )
        meta = SchemaMetadata(path)
        assert meta.get_center_align_columns() == ["region", "status"]
        assert meta.get_center_align_max_length() == 15

    def test_invalid_columns_type_raises(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(_schema({"center_align_columns": "region"})),
            encoding="utf-8",
        )
        with pytest.raises(
            SchemaMetadataError, match="center_align_columns"
        ):
            SchemaMetadata(path)

    def test_negative_max_length_raises(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(_schema({"center_align_max_length": -5})),
            encoding="utf-8",
        )
        with pytest.raises(
            SchemaMetadataError, match="center_align_max_length"
        ):
            SchemaMetadata(path)

    def test_bool_max_length_raises(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(_schema({"center_align_max_length": True})),
            encoding="utf-8",
        )
        with pytest.raises(
            SchemaMetadataError, match="center_align_max_length"
        ):
            SchemaMetadata(path)
