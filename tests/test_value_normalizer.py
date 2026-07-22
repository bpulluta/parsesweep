#!/usr/bin/env python3
"""
Unit tests for value_normalizer utilities.

Tests the value normalization and comparison logic used by QA/QC.
"""
import pytest
from psweep.utils.value_normalizer import (
    normalize_value,
    compare_values,
    is_empty_value,
    is_numeric_value,
    normalize_numeric,
    normalize_text
)


class TestNormalizeValue:
    """Test normalize_value function."""
    
    def test_none_value(self):
        """Test None normalization."""
        assert normalize_value(None) is None
    
    def test_empty_string(self):
        """Test empty string normalization."""
        assert normalize_value("") is None
        assert normalize_value("   ") is None
    
    def test_whitespace_stripping(self):
        """Test whitespace is stripped."""
        assert normalize_value("  text  ") == "text"
    
    def test_currency_parsing(self):
        """Test currency string parsing."""
        assert normalize_value("$5.00") == 5.0
        assert normalize_value("$1,234.56") == 1234.56
        assert normalize_value("$10") == 10.0
    
    def test_number_parsing(self):
        """Test number string parsing."""
        assert normalize_value("123") == 123
        assert normalize_value("123.45") == 123.45
        assert normalize_value("1,234") == 1234.0
    
    def test_text_normalization(self):
        """Test text is lowercased."""
        assert normalize_value("Text") == "text"
        assert normalize_value("UPPERCASE") == "uppercase"
    
    def test_numeric_passthrough(self):
        """Test numeric types pass through."""
        assert normalize_value(5) == 5
        assert normalize_value(5.5) == 5.5


class TestCompareValues:
    """Test compare_values function."""
    
    def test_exact_match(self):
        """Test exact value matching."""
        assert compare_values("text", "text") is True
        assert compare_values(5, 5) is True
    
    def test_case_insensitive(self):
        """Test case-insensitive text comparison."""
        assert compare_values("Text", "text") is True
        assert compare_values("UPPERCASE", "uppercase") is True
    
    def test_currency_number_equivalence(self):
        """Test that currency strings match numbers."""
        assert compare_values("$5.00", 5.0) is True
        assert compare_values("$1,234.56", 1234.56) is True
    
    def test_whitespace_ignored(self):
        """Test whitespace is ignored."""
        assert compare_values("  text  ", "text") is True
    
    def test_different_values(self):
        """Test different values don't match."""
        assert compare_values("text1", "text2") is False
        assert compare_values(5, 10) is False
    
    def test_none_strict_mode(self):
        """Test None comparison in strict mode (fuzzy=False)."""
        assert compare_values(None, None, fuzzy=False) is True
        assert compare_values(None, "value", fuzzy=False) is False
        assert compare_values("value", None, fuzzy=False) is False
    
    def test_none_fuzzy_mode(self):
        """Test None acts as wildcard in fuzzy mode."""
        assert compare_values(None, "anything", fuzzy=True) is True
        assert compare_values("anything", None, fuzzy=True) is True
        assert compare_values(None, None, fuzzy=True) is True


class TestIsEmptyValue:
    """Test is_empty_value function."""
    
    def test_none_is_empty(self):
        """Test None is considered empty."""
        assert is_empty_value(None) is True
    
    def test_empty_string_is_empty(self):
        """Test empty string is empty."""
        assert is_empty_value("") is True
    
    def test_whitespace_is_empty(self):
        """Test whitespace-only is empty."""
        assert is_empty_value("   ") is True
        assert is_empty_value("\t\n") is True
    
    def test_text_not_empty(self):
        """Test text is not empty."""
        assert is_empty_value("text") is False
        assert is_empty_value(" text ") is False
    
    def test_number_not_empty(self):
        """Test numbers are not empty."""
        assert is_empty_value(0) is False
        assert is_empty_value(5) is False


class TestIsNumericValue:
    """Test is_numeric_value function for QA/QC filtering."""
    
    def test_none_not_numeric(self):
        """Test None is not numeric."""
        assert is_numeric_value(None) is False
    
    def test_empty_string_not_numeric(self):
        """Test empty string is not numeric."""
        assert is_numeric_value("") is False
        assert is_numeric_value("   ") is False
    
    def test_pure_number_is_numeric(self):
        """Test pure numbers are numeric."""
        assert is_numeric_value("30") is True
        assert is_numeric_value("1") is True
        assert is_numeric_value("100") is True
        assert is_numeric_value(30) is True
        assert is_numeric_value(1.5) is True
    
    def test_currency_is_numeric(self):
        """Test currency values are numeric."""
        assert is_numeric_value("$15.00") is True
        assert is_numeric_value("$1,234.56") is True
    
    def test_number_with_text_is_numeric(self):
        """Test numbers with text units are numeric."""
        assert is_numeric_value("100 feet") is True
        assert is_numeric_value("500 kW") is True
        assert is_numeric_value("30 days") is True
        assert is_numeric_value("one year") is False  # Word "one" has no digits
    
    def test_percentage_is_numeric(self):
        """Test percentages are numeric."""
        assert is_numeric_value("2.5%") is True
        assert is_numeric_value("50%") is True
    
    def test_text_only_not_numeric(self):
        """Test text-only values are not numeric."""
        assert is_numeric_value("Required") is False
        assert is_numeric_value("Exempt") is False
        assert is_numeric_value("Not significantly degrade") is False
        assert is_numeric_value("Best alternative") is False
        assert is_numeric_value("Benefits outweigh losses") is False
    
    def test_dates_are_numeric(self):
        """Test date formats are numeric (contain digits)."""
        assert is_numeric_value("2013-10-01") is True
        assert is_numeric_value("October 1, 2013") is True


class TestNormalizeNumeric:
    """Test normalize_numeric function."""
    
    def test_none_value(self):
        """Test None returns None."""
        assert normalize_numeric(None) is None
    
    def test_empty_string(self):
        """Test empty string returns None."""
        assert normalize_numeric("") is None
        assert normalize_numeric("   ") is None
    
    def test_already_numeric(self):
        """Test already numeric values."""
        assert normalize_numeric(5) == 5.0
        assert normalize_numeric(5.5) == 5.5
    
    def test_currency_parsing(self):
        """Test currency string parsing."""
        assert normalize_numeric("$5.00") == 5.0
        assert normalize_numeric("$1,234.56") == 1234.56
    
    def test_percentage_parsing(self):
        """Test percentage parsing."""
        assert normalize_numeric("50%") == 0.5
        assert normalize_numeric("100%") == 1.0
        assert normalize_numeric("25.5%") == 0.255
    
    def test_comma_thousands(self):
        """Test comma thousands separator."""
        assert normalize_numeric("1,234") == 1234.0
        assert normalize_numeric("1,234,567.89") == 1234567.89
    
    def test_non_numeric_text(self):
        """Test non-numeric text returns None."""
        assert normalize_numeric("text") is None
        assert normalize_numeric("abc123") is None


class TestNormalizeText:
    """Test normalize_text function."""
    
    def test_none_value(self):
        """Test None returns None."""
        assert normalize_text(None) is None
    
    def test_empty_string(self):
        """Test empty string returns None."""
        assert normalize_text("") is None
        assert normalize_text("   ") is None
    
    def test_whitespace_stripping(self):
        """Test whitespace is stripped."""
        assert normalize_text("  text  ") == "text"
    
    def test_lowercase_conversion(self):
        """Test text is lowercased."""
        assert normalize_text("TEXT") == "text"
        assert normalize_text("MixedCase") == "mixedcase"
    
    def test_number_to_text(self):
        """Test numbers convert to text."""
        assert normalize_text(123) == "123"
        assert normalize_text(45.6) == "45.6"


class TestValueNormalizationEdgeCases:
    """Test edge cases in value normalization."""
    
    def test_special_characters(self):
        """Test handling of special characters."""
        # Currency with spaces
        assert normalize_value("$ 5.00") == 5.0
        
        # Negative numbers
        assert normalize_numeric("-5") == -5.0
        assert normalize_numeric("-$5.00") == -5.0  # Actually supported!
    
    def test_scientific_notation(self):
        """Test scientific notation (if supported)."""
        # Currently not explicitly supported, but Python's float() handles it
        assert normalize_numeric("1.5e3") == 1500.0
    
    def test_unicode_whitespace(self):
        """Test unicode whitespace handling."""
        # Non-breaking space
        assert is_empty_value("\u00A0") is True
