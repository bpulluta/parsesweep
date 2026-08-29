"""Tests for state normalization in ``utils.normalizers``.

Locks the behavior after removing the dead uppercase identity block from
US_STATES: normalize_state looks up ``cleaned.lower()``, so uppercase
abbreviations resolve via their lowercase key.
"""

import pandas as pd
import pytest

from psweep.utils.normalizers import (
    US_STATES,
    normalize_state,
    normalize_state_column,
)


class TestNormalizeState:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("TX", "TX"),            # uppercase abbreviation
            ("tx", "TX"),            # lowercase abbreviation
            ("texas", "TX"),         # lowercase full name
            ("Texas", "TX"),         # title-case full name
            ("California", "CA"),
            ("california", "CA"),
            ("New York", "NY"),
            ("  ut  ", "UT"),        # whitespace stripped
            ("UT", "UT"),
            ("washington, d.c.", "DC"),
        ],
    )
    def test_known_states(self, value, expected):
        assert normalize_state(value) == expected

    def test_unknown_value_returned_as_is(self):
        assert normalize_state("Narnia") == "Narnia"

    def test_none_and_empty(self):
        assert normalize_state(None) is None
        assert normalize_state("") == ""
        assert normalize_state("   ") == "   "

    def test_no_uppercase_identity_keys_remain(self):
        # Dead uppercase identity entries (e.g. "TX": "TX") were removed;
        # every key is stored lowercase now.
        assert all(key == key.lower() for key in US_STATES)


class TestNormalizeStateColumn:
    def test_normalizes_in_place(self):
        df = pd.DataFrame({"State": ["Utah", "California", "NY", "tx"]})
        normalize_state_column(df)
        assert df["State"].tolist() == ["UT", "CA", "NY", "TX"]

    def test_missing_column_is_noop(self):
        df = pd.DataFrame({"Other": ["x"]})
        normalize_state_column(df)  # should not raise
        assert df["Other"].tolist() == ["x"]


# --- County FIPS derivation (domain-agnostic GIS/DS join key) -----------------

from psweep.utils.normalizers import (  # noqa: E402
    county_to_fips,
    add_county_fips_column,
)


class TestCountyFips:
    def test_suffix_and_abbrev_insensitive(self):
        # Same county resolves whether the suffix/state form varies.
        assert county_to_fips("Nevada", "Churchill") == "32001"
        assert county_to_fips("NV", "Churchill County") == "32001"
        assert county_to_fips("California", "Imperial County") == "06025"

    def test_parish_and_census_area_suffixes(self):
        assert county_to_fips("LA", "Orleans Parish") == "22071"
        assert county_to_fips("LA", "Orleans") == "22071"
        assert county_to_fips("AK", "Nome Census Area") == "02180"

    def test_state_scoped_no_cross_state_collision(self):
        # "Lincoln County" exists in many states; each maps to its own FIPS.
        a = county_to_fips("NV", "Lincoln")
        b = county_to_fips("OR", "Lincoln")
        assert a and b and a != b

    def test_unknown_or_missing_returns_none(self):
        assert county_to_fips("NV", "Nowhere County") is None
        assert county_to_fips(None, "Churchill") is None
        assert county_to_fips("NV", None) is None
        assert county_to_fips("NV", "") is None

    def test_add_column_derives_and_is_noop_without_source(self):
        df = pd.DataFrame(
            {"state": ["NV", "CA"], "county": ["Churchill", "Imperial County"]}
        )
        add_county_fips_column(df, "state", "county", "county_fips")
        assert df["county_fips"].tolist() == ["32001", "06025"]

        # Missing source column -> no column added, no error.
        df2 = pd.DataFrame({"state": ["NV"]})
        add_county_fips_column(df2, "state", "county", "county_fips")
        assert "county_fips" not in df2.columns
