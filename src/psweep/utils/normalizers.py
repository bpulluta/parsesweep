#!/usr/bin/env python3
"""
Data normalization utilities for consistent outputs.

Provides standardization functions for common fields like US states.
"""

from typing import Optional
import csv
import functools
import re
from pathlib import Path
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
    # Already abbreviated (identity mapping). normalize_state() looks up
    # cleaned.lower(), so only the lowercase keys below are ever reached;
    # uppercase inputs like "TX" are matched via their lowercase form.
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

    Parameters
    ----------
    state : Optional[str]
        State name or abbreviation

    Returns
    -------
    Optional[str]
        2-letter state abbreviation, or original value if not recognized

    Examples
    --------
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

    Parameters
    ----------
    df
        pandas DataFrame
    column_name : str
        Name of the state column (default: "State")

    Examples
    --------
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


# County-name suffixes to strip for suffix-insensitive matching (Census
# spellings). Multi-word first so "City and Borough" beats "Borough".
_COUNTY_SUFFIXES = (
    "city and borough",
    "census area",
    "municipality",
    "municipio",
    "county",
    "parish",
    "borough",
)

_COUNTY_FIPS_CSV = Path(__file__).parent / "data" / "us_county_fips.csv"


def _county_key(name: str) -> str:
    """Canonical, suffix-insensitive key for county matching.

    Lowercases, drops punctuation, collapses whitespace, and strips a trailing
    county-equivalent suffix so ``"Imperial"`` and ``"Imperial County"`` match.
    """
    key = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    key = re.sub(r"\s+", " ", key).strip()
    for suffix in _COUNTY_SUFFIXES:
        if key.endswith(" " + suffix):
            return key[: -len(suffix) - 1].strip()
    return key


@functools.lru_cache(maxsize=1)
def _county_fips_lookup() -> dict:
    """Load the bundled Census county→FIPS table once: {(state, key): fips}."""
    lookup: dict = {}
    try:
        with _COUNTY_FIPS_CSV.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                state = (row.get("state") or "").strip().upper()
                fips = (row.get("fips") or "").strip()
                key = _county_key(row.get("county") or "")
                if state and fips and key:
                    lookup[(state, key)] = fips
    except OSError:
        logger.warning("County FIPS table not found at %s", _COUNTY_FIPS_CSV)
    return lookup


def county_to_fips(
    state: Optional[str], county: Optional[str]
) -> Optional[str]:
    """Return the 5-digit county FIPS (GEOID) for a state + county, or None.

    Domain-agnostic geographic enrichment: matching is suffix-insensitive and
    state-scoped, so ``("Nevada", "Churchill")`` and ``("NV", "Churchill
    County")`` both resolve to ``"32001"``. Returns None when either input is
    missing or no county matches (never raises).
    """
    if not state or not county:
        return None
    state_abbr = normalize_state(state)
    if not isinstance(state_abbr, str):
        return None
    return _county_fips_lookup().get(
        (state_abbr.strip().upper(), _county_key(county))
    )


def add_county_fips_column(
    df,
    state_column: str,
    county_column: str,
    fips_column: str = "county_fips",
) -> None:
    """Add a ``fips_column`` derived from state + county columns, in place.

    No-op when either source column is absent. Rows that don't resolve get a
    null FIPS. Universal — any domain with clean county-level geography gets a
    GIS/DS join key with zero per-domain code.
    """
    if state_column not in df.columns or county_column not in df.columns:
        return
    df[fips_column] = [
        county_to_fips(s, c)
        for s, c in zip(df[state_column], df[county_column])
    ]


def _split_camel_case(name: str) -> str:
    """Insert spaces at camelCase boundaries, leaving all other characters intact.

    Shared splitting core for :func:`humanize_field_name` and
    :func:`camel_to_title`; each applies its own separator/casing policy on top.
    """
    return "".join(" " + c if c.isupper() else c for c in name).strip()


def humanize_field_name(field_name: str) -> str:
    """
    Convert any naming style to clean Title Case.

    Handles snake_case, camelCase, and mixed formats, replacing underscores
    with spaces and splitting camelCase boundaries before capitalizing each
    word. This is the canonical column-label humanizer.

    Parameters
    ----------
    field_name : str
        Raw field name

    Returns
    -------
    str
        Clean Title Case column name

    Examples
    --------
    >>> humanize_field_name("charge_type")
    'Charge Type'
    >>> humanize_field_name("annualConsumption")
    'Annual Consumption'
    """
    name = _split_camel_case(field_name.replace("_", " "))
    return " ".join(word.capitalize() for word in name.split())


def camel_to_title(name: str) -> str:
    """
    Split camelCase boundaries and Title Case the result, preserving any
    existing separators (e.g. underscores) untouched.

    Unlike :func:`humanize_field_name`, this does not rewrite underscores;
    it is used where established display keys must keep their literal
    separators.

    Parameters
    ----------
    name : str
        Raw field name

    Returns
    -------
    str
        Title-cased name with spaces inserted at camelCase boundaries

    Examples
    --------
    >>> camel_to_title("facilityName")
    'Facility Name'
    >>> camel_to_title("utility_name")
    'Utility_Name'
    """
    return _split_camel_case(name).title()
