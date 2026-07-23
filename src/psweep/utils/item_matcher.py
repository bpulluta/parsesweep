#!/usr/bin/env python3
"""
Item matching utilities for comparing and indexing data items.

Provides reusable logic for matching items across different sources
based on identifier fields. Used by both compilation deduplication
and QA/QC comparison.
"""

import pandas as pd
import re
from typing import Dict, List, Tuple, Any, Optional, Set
import logging

logger = logging.getLogger(__name__)

# Common stop words to ignore during token matching
STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "for",
        "in",
        "on",
        "to",
        "and",
        "or",
        "with",
        "by",
        "at",
        "from",
        "is",
        "are",
        "be",
        "been",
        "being",
        "was",
        "were",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "work",
        "works",
    ]
)


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


def extract_key_tokens(text: str) -> Set[str]:
    """
    Extract key tokens from text for fuzzy matching.

    Normalizes text by:
    - Converting to lowercase
    - Removing punctuation
    - Splitting into words
    - Removing stop words
    - Keeping only meaningful content words

    Args:
        text: Text string to tokenize

    Returns:
        Set of normalized key tokens

    Examples:
        >>> extract_key_tokens("site preparation for drilling")
        {'site', 'preparation', 'drilling'}
        >>> extract_key_tokens("work in preparation of the site for drilling")
        {'preparation', 'site', 'drilling'}
    """
    if not text:
        return set()

    # Normalize: lowercase, remove punctuation, split on whitespace
    text_clean = re.sub(r"[^\w\s]", " ", text.lower())
    words = text_clean.split()

    # Filter out stop words and short words
    tokens = {w for w in words if w not in STOP_WORDS and len(w) > 1}

    return tokens


def normalize_for_matching(value: str) -> str:
    """
    Normalize a string value for fuzzy matching.

    Extracts key tokens and joins them sorted, enabling matching
    despite verbosity differences like:
    - "site preparation for drilling"
    - "work in preparation of the site for drilling"

    Both normalize to: "drilling preparation site"

    Args:
        value: String value to normalize

    Returns:
        Normalized string with sorted key tokens

    Examples:
        >>> normalize_for_matching("site preparation for drilling")
        'drilling preparation site'
        >>> normalize_for_matching("work in preparation of the site for drilling")
        'drilling preparation site'
    """
    tokens = extract_key_tokens(value)
    return " ".join(sorted(tokens))


def get_nested_value(obj: Dict[str, Any], dot_path: str) -> Any:
    """
    Get value from nested dictionary using dot-notation path.

    Args:
        obj: Dictionary to extract value from
        dot_path: Dot-notation path (e.g., "metadata.jurisdiction.state")

    Returns:
        Value at the path, or None if path doesn't exist

    Examples:
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


def create_item_key(
    item: Dict[str, Any],
    identifier_fields: List[str],
    fuzzy_fields: Optional[List[str]] = None,
) -> Tuple:
    """
    Create a unique key tuple for an item based on identifier fields.

    Values are normalized for consistent matching:
    - Strings: lowercase, stripped whitespace
    - Fuzzy fields: token-normalized (removes stop words, sorts keywords)
    - None values preserved

    Args:
        item: Data item (dict)
        identifier_fields: List of dot-notation field paths (e.g., ["metadata.id", "name"])
        fuzzy_fields: Optional list of fields to apply token normalization for fuzzy matching

    Returns:
        Tuple of normalized identifier values

    Examples:
        >>> item = {"metadata": {"id": "123"}, "name": "Test Item"}
        >>> create_item_key(item, ["metadata.id", "name"])
        ("123", "test item")

        >>> item = {"applies_to": "work in preparation of the site for drilling"}
        >>> create_item_key(item, ["applies_to"], fuzzy_fields=["applies_to"])
        ("drilling preparation site",)
    """
    values = []
    fuzzy_set = set(fuzzy_fields) if fuzzy_fields else set()

    for field_path in identifier_fields:
        value = get_nested_value(item, field_path)

        # Normalize value for key creation
        if value is None:
            values.append(None)
        elif isinstance(value, str):
            # Apply token normalization for fuzzy fields
            if field_path in fuzzy_set:
                values.append(normalize_for_matching(value))
            else:
                # Standard normalization: lowercase and strip
                values.append(value.strip().lower())
        else:
            values.append(value)

    return tuple(values)


def create_item_index(
    items: List[Dict[str, Any]],
    identifier_fields: List[str],
    fuzzy_fields: Optional[List[str]] = None,
) -> Dict[Tuple, Dict[str, Any]]:
    """
    Create an index of items by their identifier fields.

    Args:
        items: List of data items (dicts)
        identifier_fields: List of dot-notation field paths to use as keys
        fuzzy_fields: Optional list of fields to apply token normalization

    Returns:
        Dictionary mapping identifier tuples to items

    Examples:
        >>> items = [
        ...     {"id": "1", "name": "Item 1"},
        ...     {"id": "2", "name": "Item 2"}
        ... ]
        >>> index = create_item_index(items, ["id"])
        >>> index[("1",)]["name"]
        "Item 1"
    """
    index = {}

    for item in items:
        key = create_item_key(item, identifier_fields, fuzzy_fields)
        index[key] = item

    return index


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

    This function is extracted from deduplicator for reuse in QA/QC.

    Args:
        df: DataFrame to map columns from
        key_fields: Key field names from schema (dot-notation paths)
        warn_on_missing: Whether to log a warning for each unmapped key field

    Returns:
        List of actual DataFrame column names that match key_fields

    Examples:
        >>> df = pd.DataFrame(columns=["State", "Rate Name", "Charge Type"])
        >>> map_key_fields_to_columns(df, ["jurisdiction.state", "rate_name"])
        ["State", "Rate Name"]
    """
    mapped_cols = []

    for key_field in key_fields:
        # Handle nested field names (e.g., "jurisdiction.state" -> "state")
        field_name = key_field.split(".")[-1]

        normalized_field = _normalize_field_name_for_matching(field_name)

        # Find matching column (case-insensitive, with/without underscores)
        matching_col = None
        for col in df.columns:
            normalized_col = _normalize_field_name_for_matching(col)
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


def match_items_across_sources(
    source_a_items: List[Dict[str, Any]],
    source_b_items: List[Dict[str, Any]],
    identifier_fields: List[str],
) -> Dict[Tuple, Dict[str, Optional[Dict[str, Any]]]]:
    """
    Match items across two sources based on identifier fields.

    Args:
        source_a_items: Items from first source
        source_b_items: Items from second source
        identifier_fields: Fields to use for matching

    Returns:
        Dictionary mapping identifier tuples to:
        {
            "source_a": item_from_a or None,
            "source_b": item_from_b or None
        }

    Examples:
        >>> items_a = [{"id": "1", "value": "A"}]
        >>> items_b = [{"id": "1", "value": "B"}, {"id": "2", "value": "C"}]
        >>> matches = match_items_across_sources(items_a, items_b, ["id"])
        >>> matches[("1",)]["source_a"]["value"]
        "A"
        >>> matches[("2",)]["source_a"]  # Missing from source_a
        None
    """
    # Create indexes
    index_a = create_item_index(source_a_items, identifier_fields)
    index_b = create_item_index(source_b_items, identifier_fields)

    # Find all unique identifiers
    all_keys = set(index_a.keys()) | set(index_b.keys())

    # Build matched result
    matches = {}
    for key in all_keys:
        matches[key] = {
            "source_a": index_a.get(key),
            "source_b": index_b.get(key),
        }

    return matches
