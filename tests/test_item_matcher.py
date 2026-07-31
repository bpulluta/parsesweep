#!/usr/bin/env python3
"""
Unit tests for item_matcher utilities.

Tests the extracted item matching logic used by both compilation
and QA/QC comparison.
"""
import logging

import pytest
import pandas as pd
from psweep.utils.item_matcher import (
    get_nested_value,
    create_item_index,
    map_key_fields_to_columns,
    extract_key_tokens,
    normalize_for_matching
)


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


class TestTokenNormalization:
    """Test token-based normalization functions."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("site preparation for drilling", {"site", "preparation", "drilling"}),
            ("work in preparation of the site for drilling", {"preparation", "site", "drilling"}),
            ("", set()),
            (None, set()),
        ],
    )
    def test_extract_key_tokens(self, value, expected):
        assert extract_key_tokens(value) == expected

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            (
                "site preparation for drilling",
                "work in preparation of the site for drilling",
                "drilling preparation site",
            ),
        ],
    )
    def test_normalize_for_matching(self, left, right, expected):
        assert normalize_for_matching(left) == normalize_for_matching(right)
        assert normalize_for_matching(left) == expected


class TestCreateItemIndex:
    """Test create_item_index function."""
    
    def test_simple_index(self):
        """Test creating index with simple items."""
        items = [
            {"id": "1", "value": "A"},
            {"id": "2", "value": "B"}
        ]
        index = create_item_index(items, ["id"])
        
        assert len(index) == 2
        assert index[("1",)]["value"] == "A"
        assert index[("2",)]["value"] == "B"
    
    def test_composite_key_index(self):
        """Test index with composite key - keys are normalized to lowercase."""
        items = [
            {"state": "CA", "city": "LA", "value": 1},
            {"state": "CA", "city": "SF", "value": 2},
            {"state": "TX", "city": "LA", "value": 3}
        ]
        index = create_item_index(items, ["state", "city"])
        
        assert len(index) == 3
        # Keys are normalized to lowercase
        assert index[("ca", "la")]["value"] == 1
        assert index[("ca", "sf")]["value"] == 2
        assert index[("tx", "la")]["value"] == 3
    
    def test_duplicate_keys(self):
        """Test that later items overwrite earlier ones with same key."""
        items = [
            {"id": "1", "value": "first"},
            {"id": "1", "value": "second"}
        ]
        index = create_item_index(items, ["id"])
        
        # Later item should overwrite
        assert index[("1",)]["value"] == "second"


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
