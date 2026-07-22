#!/usr/bin/env python3
"""
Value normalization and comparison utilities.

Provides smart comparison logic that handles different representations
of the same value (e.g., "$5.00" vs 5.0, "CA" vs "California").

Used by QA/QC comparison engine and potentially by consolidation.
"""

from typing import Any, Optional
import re
import logging

logger = logging.getLogger(__name__)


def is_numeric_value(value: Any) -> bool:
    """
    Check if a value contains numeric content.

    Used for focused QA/QC comparison to prioritize numeric values
    over text-only values like "Required", "Exempt", etc.

    Args:
        value: Value to check

    Returns:
        True if value contains at least one digit

    Examples:
        >>> is_numeric_value("30")
        True
        >>> is_numeric_value("$15.00")
        True
        >>> is_numeric_value("100 feet")
        True
        >>> is_numeric_value("Required")
        False
        >>> is_numeric_value("Exempt")
        False
        >>> is_numeric_value(None)
        False
        >>> is_numeric_value("")
        False
    """
    if value is None:
        return False

    value_str = str(value).strip()
    if not value_str:
        return False

    # Check if value contains any digit
    return bool(re.search(r"\d", value_str))


def normalize_value(value: Any) -> Any:
    """
    Normalize a value for comparison purposes.

    Handles:
    - Numeric strings with currency symbols: "$5.00" → 5.0
    - Whitespace normalization: "  text  " → "text"
    - None/empty values → None
    - Numbers → numeric type

    Args:
        value: Value to normalize

    Returns:
        Normalized value

    Examples:
        >>> normalize_value("$5.00")
        5.0
        >>> normalize_value("  text  ")
        "text"
        >>> normalize_value("")
        None
        >>> normalize_value(None)
        None
    """
    # Handle None and empty values
    if value is None:
        return None

    # Handle strings
    if isinstance(value, str):
        # Strip whitespace
        value = value.strip()

        # Empty string → None
        if not value:
            return None

        # Try to parse as currency
        currency_match = re.match(r"^\$?\s*([\d,]+\.?\d*)$", value)
        if currency_match:
            try:
                # Remove commas and convert to float
                num_str = currency_match.group(1).replace(",", "")
                return float(num_str)
            except ValueError:
                pass

        # Try to parse as number
        try:
            # Check if it's an integer
            if "." not in value and "," not in value:
                return int(value)
            else:
                # Float (handle commas)
                return float(value.replace(",", ""))
        except ValueError:
            pass

        # Return normalized string (lowercase for case-insensitive comparison)
        return value.lower()

    # Return as-is for other types
    return value


def compare_values(val_a: Any, val_b: Any, fuzzy: bool = False) -> bool:
    """
    Compare two values with smart normalization.

    Args:
        val_a: First value
        val_b: Second value
        fuzzy: If True, treat None/empty as wildcard that matches anything

    Returns:
        True if values are considered equal

    Examples:
        >>> compare_values("$5.00", 5.0)
        True
        >>> compare_values("Text", "text")
        True
        >>> compare_values(None, "something", fuzzy=True)
        True
        >>> compare_values(None, "something", fuzzy=False)
        False
    """
    # Normalize both values
    norm_a = normalize_value(val_a)
    norm_b = normalize_value(val_b)

    # Fuzzy matching: None matches anything
    if fuzzy:
        if norm_a is None or norm_b is None:
            return True

    # Standard comparison
    return norm_a == norm_b


def is_empty_value(value: Any) -> bool:
    """
    Check if a value is considered empty/null.

    Args:
        value: Value to check

    Returns:
        True if value is None, empty string, or whitespace-only

    Examples:
        >>> is_empty_value(None)
        True
        >>> is_empty_value("")
        True
        >>> is_empty_value("   ")
        True
        >>> is_empty_value("text")
        False
    """
    if value is None:
        return True

    if isinstance(value, str):
        return not value.strip()

    return False


def normalize_numeric(value: Any) -> Optional[float]:
    """
    Normalize value to numeric type if possible.

    Handles currency formatting, percentages, and various number formats.

    Args:
        value: Value to normalize

    Returns:
        Float value or None if not numeric

    Examples:
        >>> normalize_numeric("$1,234.56")
        1234.56
        >>> normalize_numeric("50%")
        0.5
        >>> normalize_numeric("text")
        None
    """
    if value is None:
        return None

    # Already a number
    if isinstance(value, (int, float)):
        return float(value)

    # Try to parse string
    if isinstance(value, str):
        value = value.strip()

        if not value:
            return None

        # Handle percentage
        if value.endswith("%"):
            try:
                return float(value[:-1].replace(",", "")) / 100.0
            except ValueError:
                return None

        # Handle currency and numbers with commas
        cleaned = value.replace("$", "").replace(",", "").strip()

        try:
            return float(cleaned)
        except ValueError:
            return None

    return None


def normalize_text(value: Any) -> Optional[str]:
    """
    Normalize value to text for comparison.

    Args:
        value: Value to normalize

    Returns:
        Normalized lowercase string, or None if empty

    Examples:
        >>> normalize_text("  Hello World  ")
        "hello world"
        >>> normalize_text(None)
        None
        >>> normalize_text("")
        None
    """
    if value is None:
        return None

    # Convert to string and normalize
    text = str(value).strip()

    if not text:
        return None

    return text.lower()
