"""Target specs, request-field resolution, and the shipped example config."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from psweep.config import RuntimeConfigError, load_runtime_config_file, resolve_command_config
from psweep.discovery.request_inputs import discovery_request_fields
from psweep.discovery.targets import TargetSpecError, iter_targets, resolve_targets_spec

from _helpers import EXAMPLE_CONFIG


def test_csv_bom_header_and_pipe_split_urls(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text(
        "\ufeffname,urls,query\n"
        "Alpha, https://a.gov/x.pdf | https://a.gov/y ,\n"
        "Beta,,beta manual\n",
        encoding="utf-8",
    )
    rows = list(iter_targets(resolve_targets_spec("targets.csv", tmp_path)))
    assert rows == [
        {"name": "Alpha", "urls": ["https://a.gov/x.pdf", "https://a.gov/y"], "query": None},
        {"name": "Beta", "query": "beta manual"},
    ]


@pytest.mark.parametrize(
    "spec,match",
    [
        ([{"name": "a", "urls": ["ftp://a.gov/x"]}], r"targets\[0\]: invalid url"),
        ([{"name": "a", "urls": "https://a.gov/x"}], r"'urls' must be a list"),
        ("targets.txt", r"must be a \.csv or \.json path"),
        ({"cross_product": {"dimensions": [{"name": "x"}]}}, r"dimensions\[0\] must be"),
    ],
)
def test_invalid_specs_fail(tmp_path: Path, spec, match):
    with pytest.raises(TargetSpecError, match=match):
        resolve_targets_spec(spec, tmp_path)


def test_invalid_csv_url_names_file_and_line(tmp_path: Path):
    (tmp_path / "t.csv").write_text("name,urls\na,https://a.gov|not-a-url\n", encoding="utf-8")
    with pytest.raises(TargetSpecError, match=r"t\.csv:2: invalid url 'not-a-url'"):
        resolve_targets_spec("t.csv", tmp_path)


def test_csv_row_with_extra_cells_names_file_and_line(tmp_path: Path):
    (tmp_path / "t.csv").write_text("name,query\nA,a\nB,b,extra\n", encoding="utf-8")
    with pytest.raises(
        TargetSpecError, match=r"t\.csv:3: row has more cells than the 2-column header"
    ):
        resolve_targets_spec("t.csv", tmp_path)


def test_cross_product_unknown_dataset_column_suggests_match(tmp_path: Path):
    (tmp_path / "counties.csv").write_text("county,state\nA,CO\n", encoding="utf-8")
    spec = {
        "cross_product": {
            "dimensions": [
                {"name": "county", "dataset": "counties.csv", "column": "countyy"},
            ]
        }
    }
    with pytest.raises(
        TargetSpecError,
        match=r"dimensions\[0\]\.column: unknown column 'countyy' .* did you mean 'county'\?",
    ):
        resolve_targets_spec(spec, tmp_path)


def test_duplicate_partition_identity_names_both_csv_lines(tmp_path: Path):
    (tmp_path / "t.csv").write_text(
        "state,county,cost\nAZ,Pima,1\nAZ,Pinal,2\nAZ,Pima,3\n", encoding="utf-8"
    )
    with pytest.raises(
        TargetSpecError,
        match=r"t\.csv:4: duplicate target identity \(state=AZ, county=Pima\) "
        r"already used by .*t\.csv:2; discovery\.partition_by must identify",
    ):
        resolve_targets_spec("t.csv", tmp_path, ["state", "county"])
    resolve_targets_spec("t.csv", tmp_path)


@pytest.mark.parametrize(
    "spec,match",
    [
        (
            [{"name": "a", "query": "x"}, {"name": "a", "query": "y"}],
            (
                r"targets\[1\]: duplicate target identity \(name=a\) already used by targets\[0\]; "
                r"the non-input target fields"
            ),
        ),
        (
            {
                "cross_product": {
                    "dimensions": [
                        {"name": "state", "values": ["AZ", "CO"]},
                        {"name": "kind", "values": ["a", "b"]},
                    ]
                }
            },
            (
                r"cross_product row 2: duplicate target identity \(state=AZ\) already used by "
                r"targets\.cross_product row 1"
            ),
        ),
    ],
)
def test_duplicate_identity_rows_fail(tmp_path: Path, spec, match):
    fields = ["state"] if isinstance(spec, dict) else None
    with pytest.raises(TargetSpecError, match=match):
        resolve_targets_spec(spec, tmp_path, fields)


def test_json_targets_file(tmp_path: Path):
    (tmp_path / "t.json").write_text(
        json.dumps([{"name": "a", "urls": ["https://a.gov/x.pdf"]}]), encoding="utf-8"
    )
    spec = resolve_targets_spec("t.json", tmp_path)
    assert list(iter_targets(spec)) == [{"name": "a", "urls": ["https://a.gov/x.pdf"]}]


def test_cross_product_with_limit_stays_lazy(tmp_path: Path):
    (tmp_path / "states.csv").write_text(
        "state\n" + "\n".join(f"S{i}" for i in range(1000)) + "\n", encoding="utf-8"
    )
    spec = resolve_targets_spec(
        {
            "cross_product": {
                "dimensions": [
                    {"name": "state", "dataset": "states.csv", "column": "state"},
                    {"name": "year", "values": list(range(100))},
                ]
            }
        },
        tmp_path,
    )
    started = time.perf_counter()
    rows = iter_targets(spec, limit=3)
    assert not isinstance(rows, list)
    assert list(rows) == [
        {"state": "S0", "year": 0},
        {"state": "S0", "year": 1},
        {"state": "S0", "year": 2},
    ]
    assert time.perf_counter() - started < 1
    fields = discovery_request_fields(
        {"domain": "d", "queries": ["{state}"], "targets": spec, "target_limit": 2}
    )
    assert len(fields["targets"]) == 2


def test_config_errors_name_the_file(tmp_path: Path):
    config = tmp_path / "run.yaml"
    config.write_text("discovery:\n  targets: [{name: a, urls: [nope]}]\n", encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match=rf"^{config}: targets\[0\]: invalid url"):
        load_runtime_config_file(config)


def test_example_config_resolves_to_request_fields():
    fields = discovery_request_fields(
        resolve_command_config(
            command="discover", cli_values={}, config_data=load_runtime_config_file(EXAMPLE_CONFIG)
        )
    )
    assert fields["domain"] == "example"
    assert fields["targets"]
