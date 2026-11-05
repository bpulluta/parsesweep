#!/usr/bin/env python3
"""Unit tests for item_matcher utilities."""

import logging

import pytest
import pandas as pd
from psweep.utils.item_matcher import get_nested_value, map_key_fields_to_columns


class TestGetNestedValue:
    """Test get_nested_value function."""

    @pytest.mark.parametrize(
        ("obj", "path", "expected"),
        [
            ({"name": "Test"}, "name", "Test"),
            ({"metadata": {"jurisdiction": {"state": "CA"}}}, "metadata.jurisdiction.state", "CA"),
            ({"name": "Test"}, "metadata.missing", None),
            (None, "path", None),
            ({"name": "Test"}, "", None),
            ({"metadata": {"id": "123"}}, "metadata.jurisdiction.state", None),
        ],
    )
    def test_get_nested_value(self, obj, path, expected):
        assert get_nested_value(obj, path) == expected


class TestMapKeyFieldsToColumns:
    """Test map_key_fields_to_columns function."""

    def test_exact_match(self):
        """Test exact column name match."""
        df = pd.DataFrame(columns=["State", "Rate Name"])
        mapped = map_key_fields_to_columns(df, ["State", "Rate Name"])
        assert mapped == ["State", "Rate Name"]

    def test_case_insensitive(self):
        """Test case-insensitive matching."""
        df = pd.DataFrame(columns=["State", "rate name"])
        mapped = map_key_fields_to_columns(df, ["state", "Rate Name"])
        assert mapped == ["State", "rate name"]

    def test_underscore_to_space(self):
        """Test snake_case to space conversion."""
        df = pd.DataFrame(columns=["Rate Name", "Charge Type"])
        mapped = map_key_fields_to_columns(df, ["rate_name", "charge_type"])
        assert mapped == ["Rate Name", "Charge Type"]

    def test_camel_case_to_space(self):
        """Test camelCase schema fields against spaced column names."""
        df = pd.DataFrame(columns=["Reference Number", "Make"])
        mapped = map_key_fields_to_columns(df, ["referenceNumber", "make"])
        assert mapped == ["Reference Number", "Make"]

    def test_initialism_columns_are_collapsed(self):
        """Test split-initialism column names against compact schema fields."""
        df = pd.DataFrame(columns=["Rated Capacity K W", "Facility State"])
        mapped = map_key_fields_to_columns(df, ["ratedCapacityKW", "facilityState"])
        assert mapped == ["Rated Capacity K W", "Facility State"]

    def test_nested_field_extraction(self):
        """Test extraction of last part from nested field."""
        df = pd.DataFrame(columns=["State", "City"])
        mapped = map_key_fields_to_columns(df, ["jurisdiction.state", "jurisdiction.city"])
        assert mapped == ["State", "City"]

    def test_missing_fields(self):
        """Test when some fields don't match."""
        df = pd.DataFrame(columns=["State"])
        mapped = map_key_fields_to_columns(df, ["State", "Missing"])
        # Only State should be mapped
        assert mapped == ["State"]

    def test_missing_fields_can_skip_warning(self, caplog):
        """Test that missing-field warnings can be suppressed for comparison-only callers."""
        df = pd.DataFrame(columns=["State"])
        with caplog.at_level(logging.WARNING):
            mapped = map_key_fields_to_columns(df, ["State", "Missing"], warn_on_missing=False)

        assert mapped == ["State"]
        assert "Key field 'Missing' not found in DataFrame columns" not in caplog.text

    def test_empty_dataframe(self):
        """Test with empty DataFrame."""
        df = pd.DataFrame()
        mapped = map_key_fields_to_columns(df, ["field1"])
        assert mapped == []

    def test_exact_schema_column_precedes_formatted_alias(self):
        """Canonical raw paths win; formatted columns remain a wide fallback."""
        df = pd.DataFrame(columns=["Rate Name", "rate_name"])
        mapped = map_key_fields_to_columns(df, ["rate_name"])
        assert mapped == ["rate_name"]

    def test_context_path_does_not_collide_with_item_leaf(self):
        """Context and item identities remain separate despite matching leaves."""
        df = pd.DataFrame(columns=["url", "source.url"])
        assert map_key_fields_to_columns(df, ["source.url", "url"]) == ["source.url", "url"]

    def test_repeated_key_field_maps_consistently(self):
        """Normalizing columns once must not change results across key fields."""
        df = pd.DataFrame(columns=["State", "Rate Name"])
        mapped = map_key_fields_to_columns(df, ["state", "rate_name", "State"])
        assert mapped == ["State", "Rate Name", "State"]
