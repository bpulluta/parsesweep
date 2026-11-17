"""Grounding cannot bridge sampled windows or accept invented quotation text."""

import pytest

from psweep.utils.evidence import normalize, quote_in_segments, quote_present


@pytest.mark.parametrize(
    "quote,present",
    [
        ("Alpha opened in 2024", True),
        ("Alpha ... 2024", True),
        ("Alpha opened in 1900", False),
        ("", False),
        (None, False),
    ],
)
def test_source_quote_normalization(quote, present):
    assert quote_present(quote, "Alpha opened in 2024.") is present


def test_quotes_do_not_join_unrelated_sample_segments():
    segments = ["Alpha was commissioned", "A different site opened in 2024"]
    assert not quote_in_segments("Alpha was commissioned ... opened in 2024", segments)
    assert quote_in_segments("A different site opened in 2024", segments)


def test_source_normalization_preserves_shared_roster_semantics():
    assert normalize("Álpha­, County") == "alpha county"
    assert quote_present("metering charges", "meter-\ning charges")


def test_numeric_range_linebreak_never_creates_a_new_number():
    source = "Capacity 5-\n10 MW"
    assert not quote_present("Capacity 510 MW", source)
    assert not quote_in_segments("Capacity 510 MW", [source])
    assert quote_present("Capacity 5-10 MW", source)
    assert quote_present("multi-year target", "multi-\nyear target")
