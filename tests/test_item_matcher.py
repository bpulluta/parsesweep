#!/usr/bin/env python3
"""
Unit tests for item_matcher utilities.

Tests the extracted item matching logic used by both consolidation
and QA/QC comparison.
"""
import pytest
import pandas as pd
from streamline_extract.utils.item_matcher import (
    get_nested_value,
    create_item_key,
    create_item_index,
    map_key_fields_to_columns,
    match_items_across_sources,
    extract_key_tokens,
    normalize_for_matching
)


class TestGetNestedValue:
    """Test get_nested_value function."""
    
    def test_simple_path(self):
        """Test simple one-level path."""
        obj = {"name": "Test"}
        assert get_nested_value(obj, "name") == "Test"
    
    def test_nested_path(self):
        """Test multi-level nested path."""
        obj = {"metadata": {"jurisdiction": {"state": "CA"}}}
        assert get_nested_value(obj, "metadata.jurisdiction.state") == "CA"
    
    def test_missing_path(self):
        """Test path that doesn't exist."""
        obj = {"name": "Test"}
        assert get_nested_value(obj, "metadata.missing") is None
    
    def test_none_object(self):
        """Test with None object."""
        assert get_nested_value(None, "path") is None
    
    def test_empty_path(self):
        """Test with empty path."""
        obj = {"name": "Test"}
        assert get_nested_value(obj, "") is None
    
    def test_partial_path_exists(self):
        """Test when partial path exists but full path doesn't."""
        obj = {"metadata": {"id": "123"}}
        assert get_nested_value(obj, "metadata.jurisdiction.state") is None


class TestCreateItemKey:
    """Test create_item_key function."""
    
    def test_single_identifier(self):
        """Test with single identifier field."""
        item = {"id": "123"}
        key = create_item_key(item, ["id"])
        assert key == ("123",)
    
    def test_multiple_identifiers(self):
        """Test with multiple identifier fields - values normalized to lowercase."""
        item = {"id": "123", "name": "Test"}
        key = create_item_key(item, ["id", "name"])
        assert key == ("123", "test")  # Normalized to lowercase for case-insensitive matching
    
    def test_nested_identifier(self):
        """Test with nested identifier field."""
        item = {"metadata": {"id": "123"}, "name": "Test"}
        key = create_item_key(item, ["metadata.id", "name"])
        assert key == ("123", "test")  # Normalized to lowercase
    
    def test_missing_field_in_key(self):
        """Test when identifier field is missing."""
        item = {"name": "Test"}
        key = create_item_key(item, ["id", "name"])
        assert key == (None, "test")  # Normalized to lowercase
    
    def test_whitespace_normalization(self):
        """Test that string values are stripped and lowercased."""
        item = {"name": "  Test  "}
        key = create_item_key(item, ["name"])
        assert key == ("test",)  # Stripped and lowercased

    def test_fuzzy_fields_normalization(self):
        """Test fuzzy fields use token-based normalization."""
        item1 = {"category": "Working hours", "applies_to": "site preparation for drilling"}
        item2 = {"category": "Working hours", "applies_to": "work in preparation of the site for drilling"}
        
        # With fuzzy_fields, both should produce the same key
        key1 = create_item_key(item1, ["category", "applies_to"], fuzzy_fields=["applies_to"])
        key2 = create_item_key(item2, ["category", "applies_to"], fuzzy_fields=["applies_to"])
        
        assert key1 == key2  # Same key because tokens match
    
    def test_fuzzy_fields_vs_standard(self):
        """Test that fuzzy_fields produces different keys than standard normalization."""
        item1 = {"applies_to": "site preparation for drilling"}
        item2 = {"applies_to": "work in preparation of the site for drilling"}
        
        # Without fuzzy_fields, keys are different
        key1_standard = create_item_key(item1, ["applies_to"])
        key2_standard = create_item_key(item2, ["applies_to"])
        assert key1_standard != key2_standard
        
        # With fuzzy_fields, keys are the same
        key1_fuzzy = create_item_key(item1, ["applies_to"], fuzzy_fields=["applies_to"])
        key2_fuzzy = create_item_key(item2, ["applies_to"], fuzzy_fields=["applies_to"])
        assert key1_fuzzy == key2_fuzzy


class TestTokenNormalization:
    """Test token-based normalization functions."""
    
    def test_extract_key_tokens_basic(self):
        """Test basic token extraction."""
        tokens = extract_key_tokens("site preparation for drilling")
        assert tokens == {"site", "preparation", "drilling"}
    
    def test_extract_key_tokens_removes_stop_words(self):
        """Test that stop words are removed."""
        tokens = extract_key_tokens("work in preparation of the site for drilling")
        # 'work', 'in', 'of', 'the', 'for' are stop words
        assert tokens == {"preparation", "site", "drilling"}
    
    def test_normalize_for_matching(self):
        """Test that normalize_for_matching produces sorted token string."""
        text1 = "site preparation for drilling"
        text2 = "work in preparation of the site for drilling"
        
        # Both should normalize to the same string
        assert normalize_for_matching(text1) == normalize_for_matching(text2)
        assert normalize_for_matching(text1) == "drilling preparation site"
    
    def test_extract_key_tokens_empty(self):
        """Test empty string produces empty set."""
        assert extract_key_tokens("") == set()
        assert extract_key_tokens(None) == set()


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
    
    def test_empty_dataframe(self):
        """Test with empty DataFrame."""
        df = pd.DataFrame()
        mapped = map_key_fields_to_columns(df, ["field1"])
        assert mapped == []


class TestMatchItemsAcrossSources:
    """Test match_items_across_sources function."""
    
    def test_perfect_match(self):
        """Test when all items match between sources."""
        items_a = [
            {"id": "1", "value": "A1"},
            {"id": "2", "value": "A2"}
        ]
        items_b = [
            {"id": "1", "value": "B1"},
            {"id": "2", "value": "B2"}
        ]
        
        matches = match_items_across_sources(items_a, items_b, ["id"])
        
        assert len(matches) == 2
        assert matches[("1",)]["source_a"]["value"] == "A1"
        assert matches[("1",)]["source_b"]["value"] == "B1"
        assert matches[("2",)]["source_a"]["value"] == "A2"
        assert matches[("2",)]["source_b"]["value"] == "B2"
    
    def test_items_only_in_source_a(self):
        """Test items present only in first source."""
        items_a = [
            {"id": "1", "value": "A1"},
            {"id": "2", "value": "A2"}
        ]
        items_b = [
            {"id": "1", "value": "B1"}
        ]
        
        matches = match_items_across_sources(items_a, items_b, ["id"])
        
        assert len(matches) == 2
        assert matches[("1",)]["source_a"] is not None
        assert matches[("1",)]["source_b"] is not None
        assert matches[("2",)]["source_a"] is not None
        assert matches[("2",)]["source_b"] is None
    
    def test_items_only_in_source_b(self):
        """Test items present only in second source."""
        items_a = [
            {"id": "1", "value": "A1"}
        ]
        items_b = [
            {"id": "1", "value": "B1"},
            {"id": "2", "value": "B2"}
        ]
        
        matches = match_items_across_sources(items_a, items_b, ["id"])
        
        assert len(matches) == 2
        assert matches[("1",)]["source_a"] is not None
        assert matches[("1",)]["source_b"] is not None
        assert matches[("2",)]["source_a"] is None
        assert matches[("2",)]["source_b"] is not None
    
    def test_composite_key_matching(self):
        """Test matching with composite keys - keys are normalized to lowercase."""
        items_a = [
            {"state": "CA", "city": "LA", "value": 1}
        ]
        items_b = [
            {"state": "CA", "city": "LA", "value": 2},
            {"state": "CA", "city": "SF", "value": 3}
        ]
        
        matches = match_items_across_sources(items_a, items_b, ["state", "city"])
        
        assert len(matches) == 2
        # Keys are normalized to lowercase
        assert matches[("ca", "la")]["source_a"]["value"] == 1
        assert matches[("ca", "la")]["source_b"]["value"] == 2
        assert matches[("ca", "sf")]["source_a"] is None
        assert matches[("ca", "sf")]["source_b"]["value"] == 3
    
    def test_empty_sources(self):
        """Test with empty item lists."""
        matches = match_items_across_sources([], [], ["id"])
        assert len(matches) == 0
