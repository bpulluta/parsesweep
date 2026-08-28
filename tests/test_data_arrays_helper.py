"""Tests for the shared data-array discovery helpers in text_processor.

These helpers are the single source of truth for "which top-level fields hold
the extracted item arrays", consumed by item counting (record_writer),
completeness/sanity checks (TextProcessor), and total-item logging (LLMClient).
"""

from __future__ import annotations

from psweep.extraction.text_processor import (
    find_data_arrays,
    find_main_data_array,
)


def test_find_data_arrays_returns_non_empty_lists_in_order():
    data = {
        "metadata": {"id": "X"},  # dict -> excluded
        "requirements": [{"a": 1}, {"a": 2}],
        "empty": [],  # empty list -> excluded
        "rates": [{"b": 1}],
        "name": "doc",  # scalar -> excluded
    }
    assert find_data_arrays(data) == [
        ("requirements", [{"a": 1}, {"a": 2}]),
        ("rates", [{"b": 1}]),
    ]


def test_find_data_arrays_empty_when_no_lists():
    assert find_data_arrays({"a": 1, "b": {"c": 2}}) == []
    assert find_data_arrays({}) == []


def test_find_data_arrays_includes_lists_of_non_dicts():
    data = {"tags": ["x", "y", "z"]}
    assert find_data_arrays(data) == [("tags", ["x", "y", "z"])]


def test_find_main_data_array_picks_largest():
    data = {
        "small": [1, 2],
        "big": [1, 2, 3, 4],
        "mid": [1, 2, 3],
    }
    assert find_main_data_array(data) == ("big", 4)


def test_find_main_data_array_tie_breaks_to_first_in_order():
    data = {
        "first": [1, 2, 3],
        "second": [4, 5, 6],
    }
    assert find_main_data_array(data) == ("first", 3)


def test_find_main_data_array_none_when_no_arrays():
    assert find_main_data_array({"a": 1, "b": {}}) == (None, 0)
    assert find_main_data_array({"empty": []}) == (None, 0)


def test_total_items_matches_sum_of_arrays():
    """The llm_client total-item count == sum of all non-empty array lengths."""
    data = {"a": [1, 2], "b": [], "c": [3, 4, 5], "meta": {"x": 1}}
    assert sum(len(v) for _, v in find_data_arrays(data)) == 5
