"""Nested-value lookup and schema key-field to column mapping."""

import logging
import re
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_field_name_for_matching(value: str) -> str:
    """Normalize schema field names and DataFrame columns to a shared token form."""
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    spaced = re.sub(r"[_\-]+", " ", spaced)

    collapsed_tokens = []
    pending_initialism = []
    for token in spaced.lower().split():
        if len(token) == 1 and token.isalpha():
            pending_initialism.append(token)
            continue

        if pending_initialism:
            collapsed_tokens.append("".join(pending_initialism))
            pending_initialism = []
        collapsed_tokens.append(token)

    if pending_initialism:
        collapsed_tokens.append("".join(pending_initialism))

    return " ".join(collapsed_tokens)


def get_nested_value(obj: Dict[str, Any], dot_path: str) -> Any:
    """
    Get value from nested dictionary using dot-notation path.

    Parameters
    ----------
    obj : Dict[str, Any]
        Dictionary to extract value from
    dot_path : str
        Dot-notation path (e.g., "metadata.jurisdiction.state")

    Returns
    -------
    Any
        Value at the path, or None if path doesn't exist

    Examples
    --------
    >>> obj = {"metadata": {"jurisdiction": {"state": "CA"}}}
    >>> get_nested_value(obj, "metadata.jurisdiction.state")
    "CA"
    >>> get_nested_value(obj, "metadata.missing")
    None
    """
    if not obj or not dot_path:
        return None

    parts = dot_path.split(".")
    current = obj

    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None

        if current is None:
            return None

    return current


def map_key_fields_to_columns(
    df: pd.DataFrame,
    key_fields: List[str],
    *,
    warn_on_missing: bool = True,
) -> List[str]:
    """
    Map schema key_fields to actual DataFrame columns.

    Handles case variations and common naming patterns.
    For nested fields like "jurisdiction.state", looks for "State" column.
    Handles transformations like "specific_subject" -> "Specific Subject".

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame to map columns from
    key_fields : List[str]
        Key field names from schema (dot-notation paths)
    warn_on_missing : bool
        Whether to log a warning for each unmapped key field

    Returns
    -------
    List[str]
        List of actual DataFrame column names that match key_fields

    Examples
    --------
    >>> df = pd.DataFrame(columns=["State", "Rate Name", "Charge Type"])
    >>> map_key_fields_to_columns(df, ["jurisdiction.state", "rate_name"])
    ["State", "Rate Name"]
    """
    mapped_cols = []

    # Normalize each DataFrame column once, not once per key field.
    normalized_columns = [
        (col, _normalize_field_name_for_matching(col)) for col in df.columns
    ]

    for key_field in key_fields:
        if key_field in df.columns:
            mapped_cols.append(key_field)
            continue
        # Handle nested field names (e.g., "jurisdiction.state" -> "state")
        field_name = key_field.split(".")[-1]

        normalized_field = _normalize_field_name_for_matching(field_name)

        # Find matching column (case-insensitive, with/without underscores)
        matching_col = None
        for col, normalized_col in normalized_columns:
            if normalized_col == normalized_field:
                matching_col = col
                break

        if matching_col:
            mapped_cols.append(matching_col)
        elif warn_on_missing:
            logger.warning(
                f"Key field '{key_field}' not found in DataFrame columns"
            )

    return mapped_cols
