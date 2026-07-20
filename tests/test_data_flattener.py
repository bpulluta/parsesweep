"""DataFlattener is domain-neutral and config-gated.

The flattener used to hardcode energy/tariff field names (charge_type, season,
rate) and unit conversions (feet/inches/dBA). These tests prove that (a) with no
schema config the documented defaults still apply, and (b) a NON-energy schema
can redirect grouping, distinguishers, value/unit detection, and unit
normalization entirely through `consolidation.flattening` config — no code change.
"""
from __future__ import annotations

import pandas as pd

from streamline_extract.consolidation.data_flattener import DataFlattener


class _FakeSchema:
    """Minimal stand-in exposing only get_flattening_config()."""

    def __init__(self, cfg: dict):
        self._cfg = cfg

    def get_flattening_config(self) -> dict:
        return self._cfg


def test_defaults_preserve_legacy_energy_behavior():
    """No schema config -> legacy grouping + unit normalization defaults hold."""
    flat = DataFlattener()
    row = flat.flatten_item(
        {
            "charges": [
                {"charge_type": "Demand", "season": "Summer", "rate": 12},
                {"charge_type": "Demand", "season": "Winter", "rate": 8},
            ]
        }
    )
    # Grouped by charge_type, disambiguated by season (a default distinguisher).
    assert row["Charges Demand Summer Rate"] == 12
    assert row["Charges Demand Winter Rate"] == 8

    df = pd.DataFrame([{"Unit": "ft"}, {"Unit": "'"}])
    out = flat.normalize_units(df)
    assert out["Unit"].tolist() == ["feet", "feet"]


def test_config_redirects_grouping_and_distinguisher():
    """A non-energy schema drives grouping/distinguisher via config alone."""
    cfg = {
        "type_fields": ["part_type"],
        "distinguishing_fields": ["grade"],
    }
    flat = DataFlattener(schema_metadata=_FakeSchema(cfg))
    row = flat.flatten_item(
        {
            "specs": [
                {"part_type": "Bolt", "grade": "A", "torque": 5},
                {"part_type": "Bolt", "grade": "B", "torque": 9},
            ]
        }
    )
    assert row["Specs Bolt A Torque"] == 5
    assert row["Specs Bolt B Torque"] == 9
    # The legacy energy field names no longer drive anything.
    assert not any("Demand" in c for c in row)


def test_config_drives_unit_normalization_map():
    """unit_normalizations is fully replaceable for a domain's own units."""
    cfg = {"unit_normalizations": {"psi": "pounds_per_sq_inch"}}
    flat = DataFlattener(schema_metadata=_FakeSchema(cfg))
    df = pd.DataFrame([{"Unit": "psi"}, {"Unit": "ft"}])
    out = flat.normalize_units(df)
    # Domain map applied; the old feet/inches defaults are gone.
    assert out["Unit"].tolist() == ["pounds_per_sq_inch", "ft"]


def test_empty_unit_map_opts_out_of_normalization():
    """An explicit empty map disables unit normalization entirely."""
    flat = DataFlattener(schema_metadata=_FakeSchema({"unit_normalizations": {}}))
    df = pd.DataFrame([{"Unit": "ft"}])
    out = flat.normalize_units(df)
    assert out["Unit"].tolist() == ["ft"]


def test_expand_thresholds_are_configurable():
    """expand_max_items forces a summary instead of expanded columns."""
    cfg = {"expand_max_items": 1, "type_fields": ["kind"], "value_fields": ["v"]}
    flat = DataFlattener(schema_metadata=_FakeSchema(cfg))
    row = flat.flatten_item(
        {"rows": [{"kind": "A", "v": 1}, {"kind": "B", "v": 2}]}
    )
    # 2 items > max of 1 -> summarized into a single readable string column.
    assert "Rows" in row
    assert "A: 1" in row["Rows"] and "B: 2" in row["Rows"]
