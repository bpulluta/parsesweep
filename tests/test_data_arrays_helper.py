"""Tests for the shared data-array discovery helpers in text_processor.

These helpers are the single source of truth for "which top-level fields hold
the extracted item arrays", consumed by item counting (record_writer),
completeness/sanity checks (TextProcessor), and total-item logging (LLMClient).
"""

from __future__ import annotations

from psweep.extraction import text_processor
from psweep.extraction.text_processor import (
    find_data_arrays,
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


def test_domain_item_count_has_no_largest_array_fallback():
    """Record counting belongs to schema metadata, not arbitrary array size."""
    assert not hasattr(text_processor, "find_main_data_array")


def test_total_items_matches_sum_of_arrays():
    """The llm_client total-item count == sum of all non-empty array lengths."""
    data = {"a": [1, 2], "b": [], "c": [3, 4, 5], "meta": {"x": 1}}
    assert sum(len(v) for _, v in find_data_arrays(data)) == 5
