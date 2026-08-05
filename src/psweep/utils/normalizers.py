#!/usr/bin/env python3
"""
Data normalization utilities for consistent outputs.

Provides standardization functions for common fields like US states.
"""

from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Comprehensive US state name to abbreviation mapping
US_STATES = {
    # Standard full names
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    # Territories
    "puerto rico": "PR",
    "guam": "GU",
    "u.s. virgin islands": "VI",
    "american samoa": "AS",
    "northern mariana islands": "MP",
    "district of columbia": "DC",
    "washington, d.c.": "DC",
    "washington d.c.": "DC",
    # Already abbreviated (identity mapping - uppercase)
    "AL": "AL",
    "AK": "AK",
    "AZ": "AZ",
    "AR": "AR",
    "CA": "CA",
    "CO": "CO",
    "CT": "CT",
    "DE": "DE",
    "FL": "FL",
    "GA": "GA",
    "HI": "HI",
    "ID": "ID",
    "IL": "IL",
    "IN": "IN",
    "IA": "IA",
    "KS": "KS",
    "KY": "KY",
    "LA": "LA",
    "ME": "ME",
    "MD": "MD",
    "MA": "MA",
    "MI": "MI",
    "MN": "MN",
    "MS": "MS",
    "MO": "MO",
    "MT": "MT",
    "NE": "NE",
    "NV": "NV",
    "NH": "NH",
    "NJ": "NJ",
    "NM": "NM",
    "NY": "NY",
    "NC": "NC",
    "ND": "ND",
    "OH": "OH",
    "OK": "OK",
    "OR": "OR",
    "PA": "PA",
    "RI": "RI",
    "SC": "SC",
    "SD": "SD",
    "TN": "TN",
    "TX": "TX",
    "UT": "UT",
    "VT": "VT",
    "VA": "VA",
    "WA": "WA",
    "WV": "WV",
    "WI": "WI",
    "WY": "WY",
    "PR": "PR",
    "GU": "GU",
    "VI": "VI",
    "AS": "AS",
    "MP": "MP",
    "DC": "DC",
    # Already abbreviated (identity mapping - lowercase)
    "al": "AL",
    "ak": "AK",
    "az": "AZ",
    "ar": "AR",
    "ca": "CA",
    "co": "CO",
    "ct": "CT",
    "de": "DE",
    "fl": "FL",
    "ga": "GA",
    "hi": "HI",
    "id": "ID",
    "il": "IL",
    "in": "IN",
    "ia": "IA",
    "ks": "KS",
    "ky": "KY",
    "la": "LA",
    "me": "ME",
    "md": "MD",
    "ma": "MA",
    "mi": "MI",
    "mn": "MN",
    "ms": "MS",
    "mo": "MO",
    "mt": "MT",
    "ne": "NE",
    "nv": "NV",
    "nh": "NH",
    "nj": "NJ",
    "nm": "NM",
    "ny": "NY",
    "nc": "NC",
    "nd": "ND",
    "oh": "OH",
    "ok": "OK",
    "or": "OR",
    "pa": "PA",
    "ri": "RI",
    "sc": "SC",
    "sd": "SD",
    "tn": "TN",
    "tx": "TX",
    "ut": "UT",
    "vt": "VT",
    "va": "VA",
    "wa": "WA",
    "wv": "WV",
    "wi": "WI",
    "wy": "WY",
    "pr": "PR",
    "gu": "GU",
    "vi": "VI",
    "as": "AS",
    "mp": "MP",
    "dc": "DC",
}


def normalize_state(state: Optional[str]) -> Optional[str]:
    """
    Normalize US state name to 2-letter abbreviation.

    Handles:
    - Full state names (case-insensitive): "Utah" → "UT"
    - Already abbreviated: "UT" → "UT"
    - Leading/trailing whitespace
    - Invalid/unknown states → returns original value with warning
    - None/empty → returns original value

    Args:
        state: State name or abbreviation

    Returns:
        2-letter state abbreviation, or original value if not recognized

    Examples:
        >>> normalize_state("Utah")
        "UT"
        >>> normalize_state("UT")
        "UT"
        >>> normalize_state("New York")
        "NY"
        >>> normalize_state("california")
        "CA"
        >>> normalize_state(None)
        None
    """
    if not state or not isinstance(state, str):
        return state

    # Clean input
    cleaned = state.strip()

    if not cleaned:
        return state

    # Look up in mapping (case-insensitive)
    normalized = US_STATES.get(cleaned.lower())

    if normalized:
        return normalized

    # Not found - return original with warning
    logger.warning(f"Unknown state value '{state}' - keeping as-is")
    return state


def normalize_state_column(df, column_name: str = "State") -> None:
    """
    Normalize state column in-place in a DataFrame.

    Modifies the DataFrame to replace full state names with abbreviations.
    Only processes columns that exist in the DataFrame.

    Args:
        df: pandas DataFrame
        column_name: Name of the state column (default: "State")

    Examples:
        >>> import pandas as pd
        >>> df = pd.DataFrame({"State": ["Utah", "California", "NY"]})
        >>> normalize_state_column(df)
        >>> df["State"].tolist()
        ["UT", "CA", "NY"]
    """

    if column_name not in df.columns:
        return

    # Apply normalization
    original_count = len(df)
    df[column_name] = df[column_name].apply(normalize_state)

    # Log stats
    unique_states = df[column_name].nunique()
    logger.debug(
        f"Normalized {column_name}: {unique_states} unique states across {original_count} rows"
    )


def humanize_field_name(field_name: str) -> str:
    """
    Convert any naming style to clean Title Case.

    Handles snake_case, camelCase, and mixed formats, replacing underscores
    with spaces and splitting camelCase boundaries before capitalizing each
    word. This is the canonical column-label humanizer.

    Args:
        field_name: Raw field name

    Returns:
        Clean Title Case column name

    Examples:
        >>> humanize_field_name("charge_type")
        'Charge Type'
        >>> humanize_field_name("annualConsumption")
        'Annual Consumption'
    """
    name = field_name.replace("_", " ")
    name = "".join([" " + c if c.isupper() else c for c in name]).strip()
    return " ".join(word.capitalize() for word in name.split())


def camel_to_title(name: str) -> str:
    """
    Split camelCase boundaries and Title Case the result, preserving any
    existing separators (e.g. underscores) untouched.

    Unlike :func:`humanize_field_name`, this does not rewrite underscores;
    it is used where established display keys must keep their literal
    separators.

    Args:
        name: Raw field name

    Returns:
        Title-cased name with spaces inserted at camelCase boundaries

    Examples:
        >>> camel_to_title("facilityName")
        'Facility Name'
        >>> camel_to_title("utility_name")
        'Utility_Name'
    """
    return "".join([" " + c if c.isupper() else c for c in name]).strip().title()
