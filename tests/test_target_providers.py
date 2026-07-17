"""Tests for the target-generation layer (csv/inline/dataset/cross_product)."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamline_extract.acquisition.targets import (
    CrossProductTargetProvider,
    CsvTargetProvider,
    DatasetTargetProvider,
    InlineTargetProvider,
    TargetProviderError,
    resolve_target_provider,
)


def _write_csv(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestSimpleProviders:
    def test_csv_provider_loads_rows(self, tmp_path: Path):
        path = _write_csv(
            tmp_path, "t.csv", "label,state\nAurora,co\nShaker,oh\n"
        )
        rows = CsvTargetProvider(path).provide()
        assert rows == [
            {"label": "Aurora", "state": "co"},
            {"label": "Shaker", "state": "oh"},
        ]

    def test_csv_empty_cells_become_none(self, tmp_path: Path):
        path = _write_csv(tmp_path, "t.csv", "label,q\nA,\n")
        rows = CsvTargetProvider(path).provide()
        assert rows[0]["q"] is None

    def test_inline_provider_passes_through(self):
        rows = [{"label": "x"}, {"label": "y"}]
        assert InlineTargetProvider(rows).provide() == rows

    def test_dataset_provider_limit(self, tmp_path: Path):
        path = _write_csv(
            tmp_path, "d.csv", "county\nA\nB\nC\nD\n"
        )
        rows = DatasetTargetProvider(path, limit=2).provide()
        assert rows == [{"county": "A"}, {"county": "B"}]

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(TargetProviderError):
            CsvTargetProvider(tmp_path / "nope.csv").provide()


class TestCrossProduct:
    def test_values_cross_product_cardinality(self, tmp_path: Path):
        prov = CrossProductTargetProvider(
            [
                {"name": "a", "values": ["1", "2"]},
                {"name": "b", "values": ["x", "y", "z"]},
            ],
            config_dir=tmp_path,
        )
        rows = prov.provide()
        assert len(rows) == 6
        assert {"a": "1", "b": "x"} in rows
        assert {"a": "2", "b": "z"} in rows

    def test_dataset_cross_values(self, tmp_path: Path):
        _write_csv(
            tmp_path, "counties.csv", "county_name,state\nAdams,CO\nLucas,OH\n"
        )
        prov = CrossProductTargetProvider(
            [
                {"name": "county", "dataset": "counties.csv"},
                {"name": "doc_type", "values": ["ordinance", "code"]},
            ],
            config_dir=tmp_path,
        )
        rows = prov.provide()
        assert len(rows) == 4  # 2 counties x 2 doc types
        # dataset rows contribute full columns
        assert {
            "county_name": "Adams",
            "state": "CO",
            "doc_type": "ordinance",
        } in rows

    def test_filter_and_limit(self, tmp_path: Path):
        _write_csv(
            tmp_path, "c.csv", "county_name,state\nAdams,CO\nLucas,OH\nBent,CO\n"
        )
        prov = CrossProductTargetProvider(
            [
                {"name": "county", "dataset": "c.csv"},
                {"name": "doc_type", "values": ["ordinance"]},
            ],
            config_dir=tmp_path,
            filters={"state": ["CO"]},
        )
        rows = prov.provide()
        assert len(rows) == 2  # only CO counties
        assert all(r["state"] == "CO" for r in rows)

    def test_dimension_requires_values_or_dataset(self, tmp_path: Path):
        prov = CrossProductTargetProvider(
            [{"name": "bad"}], config_dir=tmp_path
        )
        with pytest.raises(TargetProviderError):
            prov.provide()


class TestResolveFactory:
    def test_resolve_dataset(self, tmp_path: Path):
        _write_csv(tmp_path, "d.csv", "county\nA\nB\n")
        prov = resolve_target_provider(
            {"source": "dataset", "path": "d.csv"}, config_dir=tmp_path
        )
        assert isinstance(prov, DatasetTargetProvider)
        assert len(prov.provide()) == 2

    def test_resolve_cross_product(self, tmp_path: Path):
        prov = resolve_target_provider(
            {
                "source": "cross_product",
                "dimensions": [{"name": "a", "values": ["1"]}],
            },
            config_dir=tmp_path,
        )
        assert isinstance(prov, CrossProductTargetProvider)

    def test_unknown_source_raises(self, tmp_path: Path):
        with pytest.raises(TargetProviderError):
            resolve_target_provider({"source": "bogus"}, config_dir=tmp_path)
