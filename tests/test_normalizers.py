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
