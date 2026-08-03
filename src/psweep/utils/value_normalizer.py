#!/usr/bin/env python3
"""Value normalization and comparison utilities."""

from typing import Any, Optional, Tuple
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


def _normalize_unit_text(raw: str) -> str:
    raw = raw.strip().lower()
    raw = re.sub(r"[^\w\s%:/()-]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _looks_like_time_format_label(raw: str) -> bool:
    compact = raw.replace(" ", "")
    if re.search(r"h{1,2}:m{1,2}", compact):
        return True
    if re.search(r"([ap]\.?m\.?)", compact):
        return True
    if "12-hour" in raw or "24-hour" in raw:
        return True
    return False


def _parse_time_of_day(raw: str) -> Optional[int]:
    """Parse common time-of-day strings to minutes since midnight."""
    text = raw.strip().lower()
    if not text:
        return None

    # Normalize punctuation variants: "7 p.m." -> "7 pm"
    text = re.sub(r"([ap])\.\s*m\.?", r"\1m", text)
    text = re.sub(r"\s+", " ", text).strip()

    # 24-hour formats like 07:00, 19:30
    m_24 = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m_24:
        hour = int(m_24.group(1))
        minute = int(m_24.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None

    # 12-hour formats like 7 am, 7:30 pm
    m_12 = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text)
    if m_12:
        hour = int(m_12.group(1))
        minute = int(m_12.group(2) or "0")
        meridiem = m_12.group(3)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None
        hour_mod = 0 if hour == 12 else hour
        if meridiem == "pm":
            hour_mod += 12
        return hour_mod * 60 + minute

    return None


def canonicalize_unit(
    unit: Any, equivalence_index: Optional[dict[str, str]] = None
) -> Optional[str]:
    """Normalize unit labels to a canonical token for semantic comparison."""
    normalized = normalize_value(unit)
    if normalized is None:
        return None
    raw = _normalize_unit_text(str(normalized))
    if not raw:
        return None
    if raw in {"time", "time format", "time-format"}:
        return "time-format"
    if _looks_like_time_format_label(raw):
        return "time-format"
    if equivalence_index and raw in equivalence_index:
        return equivalence_index[raw]
    return raw


def canonicalize_measurement(
    value: Any,
    unit: Any = None,
    equivalence_index: Optional[dict[str, str]] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    """Return canonical (numeric_value, canonical_unit) when parsable."""
    parsed_value = normalize_value(value)
    parsed_unit = canonicalize_unit(unit, equivalence_index)

    if isinstance(parsed_value, (int, float)):
        number = float(parsed_value)
        canonical_number = str(int(number)) if number.is_integer() else str(number)
        return canonical_number, parsed_unit

    if not isinstance(parsed_value, str):
        return None

    raw = parsed_value.strip().lower()
    if not raw:
        return None

    parsed_time = _parse_time_of_day(raw)
    if parsed_time is not None:
        final_unit = parsed_unit or "time-format"
        return f"time:{parsed_time}", final_unit

    raw = raw.replace(",", "")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([a-z%()./-][a-z0-9%()./\s-]*)?", raw)
    if not match:
        return None

    number = float(match.group(1))
    canonical_number = str(int(number)) if number.is_integer() else str(number)
    inline_unit = canonicalize_unit(match.group(2), equivalence_index)
    final_unit = parsed_unit or inline_unit
    return canonical_number, final_unit


def build_unit_equivalence_index(groups: Any) -> dict[str, str]:
    """Build normalized unit -> canonical token map from config groups."""
    index: dict[str, str] = {}
    if not isinstance(groups, list):
        return index
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        normalized_group = []
        for raw in group:
            if not isinstance(raw, str):
                continue
            token = _normalize_unit_text(raw)
            if token:
                normalized_group.append(token)
        if len(normalized_group) < 2:
            continue
        canonical = normalized_group[0]
        for token in normalized_group:
            index[token] = canonical
    return index
