#!/usr/bin/env python3
"""
Value normalization and comparison utilities.

Provides smart comparison logic that handles different representations
of the same value (e.g., "$5.00" vs 5.0, "CA" vs "California").

Used by QA/QC comparison engine and potentially by compilation.
"""

from typing import Any
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
