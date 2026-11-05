"""Roster compile: one evidence-resolved row per discovery target."""

from __future__ import annotations

import json

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.compilation.roster import NA, value_key
from psweep.utils.schema_metadata import SchemaMetadata

EVIDENCE = {
    "type": "object",
    "properties": {"value": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["value", "quote"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "title": "State"},
                    "ahj": {"type": "string", "title": "AHJ Name"},
                    "online": {**EVIDENCE, "title": "Online Permitting"},
                    "stamp": EVIDENCE,
                    "note": {"type": ["string", "null"]},
                },
            },
        }
    },
    "$metadata": {
        "extraction": {"main_data_array": "rows"},
        "identity": {"deduplication": {"key_fields": ["state", "ahj"]}},
    },
}
TARGETS = [
    {"state": "AZ", "ahj": "Maricopa County", "geo_id": "4013", "urls": ["https://m.gov"]},
    {"state": "AZ", "ahj": "Pima County", "geo_id": "4019"},
    {"state": "CO", "ahj": "Boulder County", "geo_id": "8013"},
]
INPUTS = {"targets": TARGETS, "partition_by": ["state", "ahj"]}


def _item(state, ahj, **fields):
    return {"state": state, "ahj": ahj, **fields}


def _ev(value, quote="quoted"):
    return {"value": value, "quote": quote}


def _metadata(tmp_path, schema=SCHEMA):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return SchemaMetadata(path)


def _write(directory, name, url, scope, items):
    record = {
        "document": {"source_url": url, "source_scope": scope},
        "payload": {"rows": items},
    }
    (directory / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def compiled(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    _write(
        records,
        "a",
        "https://m.gov/a",
        "target",
        [
            _item("AZ", "Maricopa County", online=_ev("Yes", "Apply online"), stamp=_ev("No")),
        ],
    )
    _write(
        records,
        "b",
        "https://m.gov/b",
        "target",
        [
            _item(
                "az", "maricopa county", online=_ev("yes", "Online portal"), stamp=_ev(None, None)
            ),
        ],
    )
    _write(
        records,
        "c",
        "https://p.gov",
        "target",
        [
            _item("AZ", "Pima County", online=_ev("No"), note="local"),
        ],
    )
    _write(
        records,
        "shared",
        "https://shared.org",
        "shared",
        [
            _item("AZ", "Pima County", online=_ev("Yes"), stamp=_ev("Yes")),
            _item("TX", "Travis County", online=_ev("Yes")),
        ],
    )
    compiler = DataCompiler(_metadata(tmp_path), inputs=INPUTS, verbose=False)
    frame, _ = compiler.compile_from_directory(records)
    return compiler, frame


def _row(frame, ahj):
    return frame.set_index("ahj").loc[ahj]


def test_one_row_per_target_in_roster_order(compiled):
    compiler, frame = compiled
    assert compiler.mode == "roster"
    assert frame["ahj"].tolist() == ["Maricopa County", "Pima County", "Boulder County"]
    assert frame.columns.tolist() == [
        "state",
        "ahj",
        "online",
        "stamp",
        "note",
        "geo_id",
        "sources",
        "confidence",
        "evidence",
    ]


def test_target_without_records_is_all_na_with_no_confidence(compiled):
    row = _row(compiled[1], "Boulder County")
    assert [row[c] for c in ("online", "stamp", "note", "sources")] == [NA] * 4
    assert row["confidence"] == "none"
    assert json.loads(row["evidence"]) == {}


def test_agreement_across_two_documents_is_high(compiled):
    row = _row(compiled[1], "Maricopa County")
    evidence = json.loads(row["evidence"])
    assert row["online"] == "Yes"
    assert evidence["online"] == {"value": "Yes", "quote": "Apply online", "url": "https://m.gov/a"}
    assert evidence["stamp"]["url"] == "https://m.gov/a"
    assert row["confidence"] == "medium"  # stamp has a single source
    assert row["sources"] == "https://m.gov/a; https://m.gov/b"


def test_target_scope_wins_conflict_with_low_confidence(compiled):
    row = _row(compiled[1], "Pima County")
    evidence = json.loads(row["evidence"])
    assert row["online"] == "No"
    assert evidence["online"] == {
        "value": "No",
        "quote": "quoted",
        "url": "https://p.gov",
        "conflict": True,
    }
    assert row["stamp"] == "Yes"
    assert row["note"] == "local"
    assert "quote" not in evidence["note"]
    assert row["confidence"] == "low"
    assert row["sources"] == "https://p.gov; https://shared.org"


def test_passthrough_is_verbatim_string_and_target_inputs_are_dropped(compiled):
    frame = compiled[1]
    assert frame["geo_id"].tolist() == ["4013", "4019", "8013"]
    assert "urls" not in frame.columns


def test_off_roster_items_are_counted_not_written(compiled):
    compiler, frame = compiled
    assert "Travis County" not in frame["ahj"].tolist()
    assert compiler.roster_stats == {
        "roster_targets": 3,
        "targets_with_evidence": 2,
        "unmatched_items": 1,
        "conflicts": 1,
    }


def test_headers_use_titles_and_metadata_columns_stay_last(compiled):
    compiler, frame = compiled
    output = compiler._prepare_output_dataframe(frame)
    assert output.columns.tolist() == [
        "State",
        "AHJ Name",
        "Online Permitting",
        "stamp",
        "note",
        "geo_id",
        "sources",
        "confidence",
        "evidence",
    ]


def test_passthrough_clashing_with_schema_field_is_rejected(tmp_path):
    targets = [{"state": "AZ", "ahj": "Pima County", "confidence": "x"}]
    compiler = DataCompiler(
        _metadata(tmp_path),
        inputs={"targets": targets, "partition_by": ["state", "ahj"]},
        verbose=False,
    )
    (tmp_path / "records").mkdir()
    _write(tmp_path / "records", "a", "https://p.gov", "target", [])
    with pytest.raises(ValueError, match="collide"):
        compiler.compile_from_directory(tmp_path / "records")


@pytest.mark.parametrize(
    ("left", "right"),
    [("> 100", "< 100"), ("-5", "5"), ("1.5", "1-5"), ("Yes", "Yes."), ("1,5", "15")],
)
def test_punctuation_keeps_values_distinct(left, right):
    assert value_key(left) != value_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("300", "300.0"),
        (300, "300.0"),
        (300.0, 300),
        ("1,000", 1000),
        ("-5", -5.0),
        ("Yes", "  yes "),
        ("Yes (SolarAPP+)", "yes  (solarapp+)"),
    ],
)
def test_numbers_by_value_and_text_by_casefold(left, right):
    assert value_key(left) == value_key(right)


def test_number_text_does_not_equal_its_string_lookalike():
    assert value_key("1e3") != value_key(1000)
    assert value_key(True) != value_key(1)


def test_numeric_agreement_and_punctuation_conflict(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    _write(
        records,
        "a",
        "https://a.gov",
        "target",
        [
            _item("AZ", "Maricopa County", online=_ev("300"), note="> 100"),
        ],
    )
    _write(
        records,
        "b",
        "https://b.gov",
        "target",
        [
            _item("AZ", "Maricopa County", online=_ev("300.0"), note="< 100"),
        ],
    )
    compiler = DataCompiler(_metadata(tmp_path), inputs=INPUTS, verbose=False)
    frame, _ = compiler.compile_from_directory(records)
    evidence = json.loads(_row(frame, "Maricopa County")["evidence"])
    assert "conflict" not in evidence["online"]
    assert evidence["note"]["conflict"] is True
    assert compiler.roster_stats["conflicts"] == 1
    assert _row(frame, "Maricopa County")["confidence"] == "low"


def test_agreeing_cell_cites_the_targets_own_page_over_a_shared_listing(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    _write(
        records,
        "a_listing",
        "https://shared.org/list",
        "shared",
        [
            _item("AZ", "Maricopa County", online=_ev("Yes", "Maricopa County AZ")),
        ],
    )
    _write(
        records,
        "b_own",
        "https://m.gov/permits",
        "target",
        [
            _item("AZ", "Maricopa County", online=_ev("Yes", "Apply for permits online")),
        ],
    )
    frame, _ = DataCompiler(
        _metadata(tmp_path),
        inputs=INPUTS,
        verbose=False,
    ).compile_from_directory(records)
    row = _row(frame, "Maricopa County")
    assert json.loads(row["evidence"])["online"] == {
        "value": "Yes",
        "quote": "Apply for permits online",
        "url": "https://m.gov/permits",
    }
    assert row["confidence"] == "high"
