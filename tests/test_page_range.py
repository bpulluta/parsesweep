"""Page selections: parsing, normalization, formatting and pages.csv loading."""

from __future__ import annotations

import pytest

from psweep.utils.page_range import (
    count_pages,
    format_page_ranges,
    load_pages_csv,
    normalize_page_ranges,
    page_numbers,
    parse_page_ranges,
    ranges_from_pages,
)


class TestParse:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("615-759", ((615, 759),)),
            ("27", ((27, 27),)),
            ("27,68-73", ((27, 27), (68, 73))),
            (" 27 , 68 - 73 ", ((27, 27), (68, 73))),
            ("70-73,27,68-71", ((27, 27), (68, 73))),
            ("1-3,4-5", ((1, 5),)),
        ],
    )
    def test_sorts_and_merges(self, spec, expected):
        assert parse_page_ranges(spec) == expected

    @pytest.mark.parametrize(
        "spec", ["", " ", "0", "5-3", "a-b", "27,,68", "1:5", "-3", "1-2-3", "3-", "1.5"]
    )
    def test_rejects_malformed_specs(self, spec):
        with pytest.raises(ValueError, match="page"):
            parse_page_ranges(spec)

    def test_round_trips_through_format(self):
        ranges = parse_page_ranges("27,68-73")
        assert format_page_ranges(ranges) == "27,68-73"
        assert parse_page_ranges(format_page_ranges(ranges)) == ranges


class TestNormalize:
    def test_empty_selection_is_empty(self):
        assert normalize_page_ranges([]) == ()
        assert ranges_from_pages([]) == ()

    @pytest.mark.parametrize(
        "ranges", [[(0, 2)], [(4, 3)], [(True, 2)], [(1.5, 2)], [(1,)], [5], [(1, 2, 3)]]
    )
    def test_rejects_invalid_pairs(self, ranges):
        with pytest.raises(ValueError, match="page"):
            normalize_page_ranges(ranges)

    def test_pages_round_trip(self):
        ranges = ranges_from_pages([73, 27, 68, 69, 70, 71, 72, 27])
        assert ranges == ((27, 27), (68, 73))
        assert page_numbers(ranges) == [27, 68, 69, 70, 71, 72, 73]
        assert count_pages(ranges) == 7


class TestPagesCsv:
    def _csv(self, tmp_path, body):
        path = tmp_path / "pages.csv"
        path.write_text("file_path,start_page,end_page\n" + body, encoding="utf-8")
        return path

    def test_rows_for_one_file_combine(self, tmp_path):
        path = self._csv(tmp_path, "tariff.pdf,68,73\ntariff.pdf,27,27\nother.pdf,5,9\n")
        assert load_pages_csv(path) == {
            "tariff.pdf": ((27, 27), (68, 73)),
            "other.pdf": ((5, 9),),
        }

    def test_blank_bounds_select_full_document(self, tmp_path):
        assert load_pages_csv(self._csv(tmp_path, "small.pdf,,\n")) == {"small.pdf": None}

    @pytest.mark.parametrize(
        "body",
        [
            "tariff.pdf,27,27\ntariff.pdf,,\n",
            "tariff.pdf,,\ntariff.pdf,27,27\n",
            "tariff.pdf,9,5\n",
            "tariff.pdf,0,5\n",
            "tariff.pdf,x,5\n",
            "tariff.pdf,5,\n",
        ],
    )
    def test_rejects_invalid_rows(self, tmp_path, body):
        with pytest.raises(ValueError, match="row"):
            load_pages_csv(self._csv(tmp_path, body))

    def test_requires_headers(self, tmp_path):
        path = tmp_path / "pages.csv"
        path.write_text("file,pages\ntariff.pdf,27\n", encoding="utf-8")
        with pytest.raises(ValueError, match="file_path, start_page, end_page"):
            load_pages_csv(path)
