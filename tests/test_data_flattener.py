"""Compiled cells: field names kept, lists and objects rendered by one rule, units normalized."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.compilation.data_flattener import DataFlattener, to_cell
from psweep.utils.schema_metadata import SchemaMetadata


class _FakeSchema:
    def __init__(self, cfg: dict):
        self._cfg = cfg

    def get_flattening_config(self) -> dict:
        return self._cfg


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        (["site plan", "one-line diagram"], "site plan, one-line diagram"),
        ([300, True], "300, True"),
        ([], ""),
        ([{"rate": 1}], '[{"rate":1}]'),
        ([["a"]], '[["a"]]'),
        ({"amount": 300, "unit": "café"}, '{"amount":300,"unit":"café"}'),
        ("text", "text"),
        (2.5, 2.5),
        (None, None),
    ],
)
def test_to_cell_joins_primitive_lists_and_keeps_nested_values_as_json(value, cell):
    assert to_cell(value) == cell


def test_flatten_item_keeps_field_names():
    row = DataFlattener(_FakeSchema({})).flatten_item(
        {
            "rate_name": "R-1",
            "location": {"city": "Mesa", "zip": "85201", "areas": ["N", "S"]},
            "tags": ["a", "b"],
            "charges": [{"rate": 1}],
            "notes": None,
            "blank": "  ",
        }
    )
    assert row == {
        "rate_name": "R-1",
        "location_city": "Mesa",
        "location_zip": "85201",
        "location_areas": "N, S",
        "tags": "a, b",
        "charges": '[{"rate":1}]',
        "notes": "",
        "blank": "",
    }


def test_rows_mode_context_values_render_like_item_values(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "utility": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "counties": {"type": "array", "items": {"type": "string"}},
                    "contact": {"type": "object"},
                },
            },
            "rates": {
                "type": "array",
                "items": {"type": "object", "properties": {"rate_name": {"type": "string"}}},
            },
        },
        "$metadata": {
            "extraction": {"main_data_array": "rates", "context_objects": ["utility"]},
            "identity": {"deduplication": {"key_fields": ["rate_name"]}},
        },
    }
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    payload = {
        "utility": {"name": "Metro", "counties": ["Pima", "Pinal"], "contact": {"phone": "555"}},
        "rates": [{"rate_name": "R-1"}],
    }
    (records / "a.json").write_text(json.dumps({"payload": payload}), encoding="utf-8")
    compiler = DataCompiler(SchemaMetadata(tmp_path / "schema.json"), verbose=False)
    frame, _ = compiler.compile_from_directory(records)
    row = frame.iloc[0]
    assert (row["name"], row["counties"], row["contact"]) == (
        "Metro",
        "Pima, Pinal",
        '{"phone":"555"}',
    )


def test_unit_normalization_applies_only_to_named_fields():
    flat = DataFlattener(
        _FakeSchema({"unit_fields": ["units"], "unit_normalizations": {"ft": "feet"}})
    )
    df = pd.DataFrame([{"units": "ft", "other": "ft"}, {"units": "psi", "other": "x"}])
    out = flat.normalize_units(df)
    assert out["units"].tolist() == ["feet", "psi"]
    assert out["other"].tolist() == ["ft", "x"]


def test_no_unit_config_is_a_noop():
    df = pd.DataFrame([{"units": "ft"}])
    assert DataFlattener(_FakeSchema({})).normalize_units(df)["units"].tolist() == ["ft"]
