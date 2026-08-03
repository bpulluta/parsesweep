#!/usr/bin/env python3
"""Unit tests for value_normalizer utilities."""

import pytest

from psweep.utils.value_normalizer import (
    build_unit_equivalence_index,
    canonicalize_measurement,
    canonicalize_unit,
    is_numeric_value,
    normalize_value,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  text  ", "text"),
        ("$5.00", 5.0),
        ("$1,234.56", 1234.56),
        ("$10", 10.0),
        ("123", 123),
        ("123.45", 123.45),
        ("1,234", 1234.0),
        ("Text", "text"),
        ("UPPERCASE", "uppercase"),
        (5, 5),
        (5.5, 5.5),
    ],
)
def test_normalize_value(value, expected):
    assert normalize_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("30", True),
        ("1", True),
        ("100", True),
        (30, True),
        (1.5, True),
        ("$15.00", True),
        ("$1,234.56", True),
        ("100 feet", True),
        ("500 kW", True),
        ("30 days", True),
        ("one year", False),
        ("2.5%", True),
        ("50%", True),
        ("Required", False),
        ("Exempt", False),
        ("Not significantly degrade", False),
        ("Best alternative", False),
        ("Benefits outweigh losses", False),
        ("2013-10-01", True),
        ("October 1, 2013", True),
    ],
)
def test_is_numeric_value(value, expected):
    assert is_numeric_value(value) is expected


def test_canonicalize_unit_with_config_groups():
    index = build_unit_equivalence_index([["hours", "hrs", "hr"]])
    assert canonicalize_unit("hrs", index) == "hours"
    assert canonicalize_unit("hour", index) == "hour"


def test_canonicalize_measurement_with_inline_unit_and_config_groups():
    index = build_unit_equivalence_index([["feet", "ft"]])
    assert canonicalize_measurement("50 feet", equivalence_index=index) == ("50", "feet")
    assert canonicalize_measurement("50", "ft", equivalence_index=index) == ("50", "feet")


def test_canonicalize_measurement_treats_time_formats_as_equivalent():
    index = build_unit_equivalence_index([["HH:MM (24-hour)", "a.m./p.m."]])
    assert canonicalize_measurement("07:00", "HH:MM (24-hour)", index) == (
        "time:420",
        "time-format",
    )
    assert canonicalize_measurement("7 a.m.", "a.m./p.m.", index) == (
        "time:420",
        "time-format",
    )


def test_canonicalize_unit_maps_generic_time_label():
    assert canonicalize_unit("time") == "time-format"
