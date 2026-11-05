"""DataFlattener keeps schema field names and only normalizes configured unit fields."""

from __future__ import annotations

import pandas as pd

from psweep.compilation.data_flattener import DataFlattener


class _FakeSchema:
    def __init__(self, cfg: dict):
        self._cfg = cfg

    def get_flattening_config(self) -> dict:
        return self._cfg


def test_flatten_item_keeps_field_names():
    row = DataFlattener(_FakeSchema({})).flatten_item(
        {
            "rate_name": "R-1",
            "location": {"city": "Mesa", "zip": "85201"},
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
        "tags": "a, b",
        "charges": '[{"rate":1}]',
        "notes": "",
        "blank": "",
    }


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
