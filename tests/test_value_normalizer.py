#!/usr/bin/env python3
"""
Unit tests for value_normalizer utilities.

Tests the value normalization and comparison logic used by QA/QC.
"""
import pytest
from psweep.utils.value_normalizer import (
    normalize_value,
    is_numeric_value,
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
