"""Roster-anchored candidate matching (offline)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from psweep.config import RuntimeConfigError
from psweep.config.runtime_config_loader import (
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.targets import identify_target
from psweep.exceptions import SchemaValidationError
from psweep.extraction.llm_client import LLMClient
from psweep.extraction.roster import (
    Roster,
    batch,
    candidate_hash,
    candidates_block,
    load_roster,
    normalize,
    roster_fields,
    scan_candidates,
    verify_quotes,
)
from psweep.extraction.schema_utils import evidence_paths
from psweep.extraction.text_processor import calculate_completeness
from psweep.utils.schema_metadata import SchemaMetadata

FIELDS = ("state", "ahj")
ROWS = [
    {"state": "AZ", "ahj": "Maricopa County"},
    {"state": "AZ", "ahj": "Pima County"},
    {"state": "CO", "ahj": "Boulder County"},
]
PIMA = identify_target(ROWS[1], FIELDS)


@pytest.fixture
def roster():
    return Roster.build(ROWS, FIELDS)


def _hits(text, roster):
    return set().union(*(ids for _, ids in scan_candidates(text, roster)))


@pytest.mark.parametrize(
    "text",
    ["Permit fees for Pima County, AZ residential PV.", "PIMA COUNTY SOLAR PERMIT"],
)
def test_scan_hits_full_roster_names(roster, text):
    assert _hits(text, roster) == {PIMA}


@pytest.mark.parametrize(
    "text", ["Pima residents may apply online.", "Boulder is a city."]
)
def test_scan_ignores_partial_names(roster, text):
    assert _hits(text, roster) == set()


def test_scan_keeps_unique_names_despite_other_states(roster):
    assert _hits("Pima County, Colorado has no such office.", roster) == {PIMA}


WASHINGTON = [{"state": "PA", "ahj": "Washington County"}, {"state": "OR", "ahj": "Washington County"}]
ADAMS = [{"state": code, "ahj": "Adams County"} for code in ("CO", "ID", "KS")]


def _ids(rows, *codes):
    return {identify_target(row, FIELDS) for row in rows if row["state"] in codes}


def test_ambiguous_name_is_not_its_own_state_evidence():
    text = "Pennsylvania solar guide\n" + "x" * 3000 + "\nWashington County permits"
    assert _hits(text, Roster.build(WASHINGTON, FIELDS)) == _ids(WASHINGTON, "PA")


def test_bare_two_letter_tokens_are_not_state_evidence():
    text = "Kansas permit survey\n" + "x" * 3000 + "\nAdams County Permit ID 2231"
    assert _hits(text, Roster.build(ADAMS, FIELDS)) == _ids(ADAMS, "KS")


def test_ambiguous_name_uses_address_state():
    text = "Washington County, OR 97124: permits online."
    assert _hits(text, Roster.build(WASHINGTON, FIELDS)) == _ids(WASHINGTON, "OR")


def test_ambiguous_name_without_evidence_keeps_all():
    text = "Washington County permits are online."
    assert _hits(text, Roster.build(WASHINGTON, FIELDS)) == _ids(WASHINGTON, "PA", "OR")


def test_normalize_folds_case_accents_and_punctuation():
    assert normalize("  Doña-Ana   COUNTY, N.M. ") == "dona ana county n m"


def test_batch_respects_budget_and_document_order():
    candidates = [("a" * 40, {"1"}), ("b" * 40, {"2"}), ("c" * 40, {"3"})]
    batches = batch(candidates, 90)
    assert [ids for _, ids in batches] == [["1", "2"], ["3"]]
    assert all(len(text) <= 90 for text, _ in batches)
    assert batches[0][0].startswith("a")


def test_candidate_hash_is_order_free_and_set_sensitive(roster):
    ids = list(roster.targets)
    assert candidate_hash(roster, ids) == candidate_hash(roster, reversed(ids))
    assert candidate_hash(roster, ids[:2]) != candidate_hash(roster, ids)


def test_verify_quotes_nulls_unverifiable_evidence():
    data = {
        "items": [
            {
                "kept": {"value": "Yes", "quote": "permits  are ONLINE."},
                "fabricated": {"value": "No", "quote": "never said"},
                "unquoted": {"value": "Yes", "quote": None},
                "empty": {"value": None, "quote": None},
            }
        ]
    }
    paths = [("items", "*", name) for name in ("kept", "fabricated", "unquoted", "empty")]
    assert verify_quotes(data, paths, "All permits are online.") == [
        'unverified quote at items[0].fabricated: "never said"',
        "unverified quote at items[0].unquoted: null",
    ]
    item = data["items"][0]
    assert item["kept"] == {"value": "Yes", "quote": "permits  are ONLINE."}
    assert item["fabricated"] == item["unquoted"] == {"value": None, "quote": None}


def test_roster_fields_activation():
    targets = {"targets": "t.csv", "partition_by": ["state", "ahj"]}
    assert roster_fields(targets, ["ahj", "state"]) == FIELDS
    assert roster_fields(targets, ["permit_id"]) is None
    assert roster_fields({"partition_by": ["state", "ahj"]}, ["state", "ahj"]) is None
    with pytest.raises(RuntimeConfigError, match="partially overlaps"):
        roster_fields(targets, ["state", "permit_id"])


def test_roster_fields_must_be_top_level_item_fields(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "items"},
                    "identity": {"deduplication": {"key_fields": ["state", "ahj"]}},
                },
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"state": {}}},
                    }
                },
            }
        )
    )
    inputs = {"targets": "unused.csv", "partition_by": list(FIELDS)}
    with pytest.raises(RuntimeConfigError, match="top-level 'items'"):
        load_roster(inputs, SchemaMetadata(schema))


@pytest.mark.parametrize("config", sorted(Path("config").glob("*/run.yaml")))
def test_shipped_configs_derive_roster_mode(config):
    inputs = resolve_command_config(
        command="extract", cli_values={}, config_data=load_runtime_config_file(config)
    )
    roster = load_roster(inputs, SchemaMetadata(Path(inputs["schema"])))
    if config.parent.name == "solartrace":
        assert roster is not None and roster.fields == FIELDS
    else:
        assert roster is None


def test_prompt_schema_hides_metadata():
    schema = {
        "$metadata": {"extraction": {"main_data_array": "items"}},
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }
    prompt = LLMClient._build_extraction_prompt("BODY", schema)
    assert "$metadata" not in prompt and '"items"' in prompt


def test_scan_scales_to_large_roster_and_document():
    rows = [{"state": "AZ", "ahj": f"Town{index} County"} for index in range(100_000)]
    roster = Roster.build(rows, FIELDS)
    line = "Ordinary permitting prose about fees, setbacks, and inspections.\n"
    text = line * (1_000_000 // len(line)) + "Town99999 County, Arizona fee schedule\n"
    started = time.perf_counter()
    hits = _hits(text, roster)
    assert time.perf_counter() - started < 2
    assert hits == {identify_target(rows[-1], FIELDS)}


def _evidence_schema(value, quote=None):
    return {
        "type": "object",
        "properties": {
            "fee": {
                "type": "object",
                "required": ["value", "quote"],
                "properties": {"value": value, "quote": quote or {"type": ["string", "null"]}},
            }
        },
    }


@pytest.mark.parametrize(
    "value",
    [{"type": ["number", "null"]}, {"enum": ["Yes", "No", None]}, {"$ref": "#/$defs/nullable"}],
)
def test_evidence_paths_accepts_nullable_value(value):
    schema = {**_evidence_schema(value), "$defs": {"nullable": {"type": ["string", "null"]}}}
    assert evidence_paths(schema) == [("fee",)]


@pytest.mark.parametrize("value", [{"type": "number"}, {"enum": ["Yes", "No"]}])
def test_evidence_paths_rejects_non_nullable_value(value):
    with pytest.raises(SchemaValidationError, match="'value' accepting null"):
        evidence_paths(_evidence_schema(value))


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf", "allOf"])
def test_evidence_paths_walks_combinators(keyword):
    evidence = _evidence_schema({"type": ["string", "null"]})["properties"]["fee"]
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {keyword: [{"type": "object", "properties": {"fee": evidence}}]}}
        },
    }
    assert evidence_paths(schema) == [("items", "*", "fee")]


def test_completeness_treats_all_null_evidence_as_empty():
    empty = {"value": None, "quote": None}
    sparse = {"items": [{"a": empty, "b": empty, "c": {"value": "x", "quote": "x"}, "d": [empty, {}]}]}
    full = {"items": [{key: {"value": "x", "quote": "x"} for key in "abcd"}]}
    assert calculate_completeness(sparse) < calculate_completeness(full)
    assert calculate_completeness(sparse) == calculate_completeness({"items": [{"c": "x", "a": None, "b": None, "d": None}]})


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("the county\u2019s \u201conline\u201d portal", "the county's \"online\" portal"),
        ("solar \u2014 storage \u2013 rooftop", "solar - storage - rooftop"),
        ("online permit\u00adting is available", "online permitting is available"),
        ("online permit-\nting is available", "online permitting is available"),
        ("solar-\nready homes", "solar-ready homes"),
        ("permits\u00a0are online", "permits are online"),
        ("\ufb01nal \ufb02at fee", "final flat fee"),
    ],
)
def test_verify_quotes_tolerates_pdf_text_artifacts(source, quote):
    data = {"fee": {"value": "Yes", "quote": quote}}
    assert verify_quotes(data, [("fee",)], source) == []
    assert data["fee"]["value"] == "Yes"


def test_verify_quotes_still_rejects_fabrication_and_records_it():
    quote = "Applications are approved instantly via SolarAPP+ " * 5
    data = {"rows": [{"fee": {"value": "Yes", "quote": quote}}]}
    (warning,) = verify_quotes(data, [("rows", "*", "fee")], "Permits take 10 business days.")
    assert warning == f"unverified quote at rows[0].fee: {json.dumps(quote[:160])}"
    assert data["rows"][0]["fee"] == {"value": None, "quote": None}


def test_roster_key_normalizes_both_sides(roster):
    for ahj in ("PIMA COUNTY", "Pima County,", "Pima  County ", " pima county"):
        assert roster.key({"state": "AZ", "ahj": ahj}) == roster.key(ROWS[1])
    assert roster.key({"state": "AZ", "ahj": "Pima"}) != roster.key(ROWS[1])
    block = candidates_block(roster, [PIMA])
    assert '"ahj": "Pima County"' in block


def test_dense_table_spans_send_each_line_once():
    rows = [{"state": "TX", "ahj": f"Town {number:03d} County"} for number in range(300)]
    dense = Roster.build(rows, FIELDS)
    lines = [f"| Town {number:03d} County, TX | $1{number:03d} | online |" for number in range(300)]
    text = "Fee table\n" + "\n".join(lines) + "\n"
    assert len(text) > 2000
    candidates = scan_candidates(text, dense)
    (batched,) = batch(candidates, 200_000)
    assert len(batched[0]) <= len(text)
    assert set(batched[1]) == set(dense.targets)


def test_batch_splits_oversized_span_at_line_boundaries():
    span = "\n".join(f"line {number:02d}" for number in range(20))
    pieces = batch([(span, {"1"})], 30)
    assert all(len(text) <= 30 for text, _ in pieces)
    assert [line for text, _ in pieces for line in text.splitlines() if line] == span.splitlines()
