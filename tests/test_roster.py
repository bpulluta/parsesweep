"""Roster-anchored candidate matching (offline)."""

from __future__ import annotations

import json
import time

import pytest

from psweep.config import RuntimeConfigError
from psweep.discovery.targets import identify_target
from psweep.exceptions import SchemaValidationError
from psweep.extraction.llm_client import LLMClient
from psweep.utils.evidence import normalize, quote_present
from psweep.extraction.roster import (
    WINDOW_CHARS,
    QuoteRows,
    Roster,
    batch,
    candidate_hash,
    candidates_block,
    load_roster,
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


@pytest.mark.parametrize("text", ["Pima residents may apply online.", "Boulder is a city."])
def test_scan_ignores_partial_names(roster, text):
    assert _hits(text, roster) == set()


def test_scan_keeps_unique_names_despite_other_states(roster):
    assert _hits("Pima County, Colorado has no such office.", roster) == {PIMA}


WASHINGTON = [
    {"state": "PA", "ahj": "Washington County"},
    {"state": "OR", "ahj": "Washington County"},
]
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


SANTA_CRUZ = [
    {"state": "AZ", "ahj": "Santa Cruz County"},
    {"state": "CA", "ahj": "Santa Cruz County"},
]


@pytest.mark.parametrize(
    "layout",
    [
        (
            "Jurisdiction Name: Santa Clara | State: CA | PV: TRUE\n"
            "Jurisdiction Name: Santa Cruz County | State: CA | PV: TRUE\n"
            "Jurisdiction Name: Santee | State: CA | PV: TRUE"
        ),
        "Jurisdiction: Santa Cruz County | state_abbr: CA",
        "Santa Clara County\n\nCA\n\nSanta Cruz County\n\nCA\n\nSantee\n\nCA",
    ],
)
def test_ambiguous_name_uses_labelled_or_card_state(layout):
    text = "Spokane, WA 99201\n" + "x" * 3000 + "\n" + layout
    assert _hits(text, Roster.build(SANTA_CRUZ, FIELDS)) == _ids(SANTA_CRUZ, "CA")


@pytest.mark.parametrize(
    "text",
    [
        "Washington County solar permits\nSubmit a one-line diagram\n\nOR\n\nsubmit a site plan.",
        "Submit to the Washington County Planning Department\n\nOR\n\napply online.",
        "| ID | Jurisdiction | Status |\n|---|---|---|\n| 12 | Washington County | OK |",
        "Washington County permits are online.\nApplication Status: OK",
    ],
)
def test_uppercase_words_are_not_state_evidence(text):
    assert _hits(text, Roster.build(WASHINGTON, FIELDS)) == _ids(WASHINGTON, "PA", "OR")


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
    assert verify_quotes(data, "All permits are online.", evidence=paths) == [
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
            "items": {
                "type": "array",
                "items": {keyword: [{"type": "object", "properties": {"fee": evidence}}]},
            }
        },
    }
    assert evidence_paths(schema) == [("items", "*", "fee")]


def test_completeness_treats_all_null_evidence_as_empty():
    empty = {"value": None, "quote": None}
    sparse = {
        "items": [{"a": empty, "b": empty, "c": {"value": "x", "quote": "x"}, "d": [empty, {}]}]
    }
    full = {"items": [{key: {"value": "x", "quote": "x"} for key in "abcd"}]}
    assert calculate_completeness(sparse) < calculate_completeness(full)
    assert calculate_completeness(sparse) == calculate_completeness(
        {"items": [{"c": "x", "a": None, "b": None, "d": None}]}
    )


@pytest.mark.parametrize(
    ("source", "quote"),
    [
        ("the county\u2019s \u201conline\u201d portal", 'the county\'s "online" portal'),
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
    assert verify_quotes(data, source, evidence=[("fee",)]) == []
    assert data["fee"]["value"] == "Yes"


def test_verify_quotes_still_rejects_fabrication_and_records_it():
    quote = "Applications are approved instantly via SolarAPP+ " * 5
    data = {"rows": [{"fee": {"value": "Yes", "quote": quote}}]}
    (warning,) = verify_quotes(
        data, "Permits take 10 business days.", evidence=[("rows", "*", "fee")]
    )
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


def test_card_layout_window_keeps_the_label_value_lines_after_a_name(roster):
    filler = "\n\n".join(f"Town {number} Program\nhexagon SolSmart" for number in range(200))
    card = "Pima County, AZ\n\nProgram\nhexagon SolSmart\n\nDesignation Level\nGold\n\n"
    text = f"{filler}\n\n{card}Designation Year\n2017\n\n{filler}"
    assert len(text) > WINDOW_CHARS
    ((window, ids),) = scan_candidates(text, roster)
    assert ids == {PIMA}
    assert "Designation Level\nGold" in window and "2017" in window
    assert len(window) < 2 * 400 + len(card) + 40


def test_labelled_table_row_window_keeps_its_neighbours(roster):
    def row(name):
        return f"Jurisdiction Name: {name} | State: AZ | Solar + Storage: TRUE"

    text = "\n".join(row(f"Town {number}") for number in range(100))
    text += "\n" + row("Pima County") + "\n" + text
    ((window, _),) = scan_candidates(text, roster)
    lines = window.splitlines()
    assert row("Pima County") in lines and 3 <= len(lines) <= 15


ROWS_TEXT = "Wells shall be set back 500 feet. Noise is limited to 55 dBA."


def test_rows_mode_drops_items_whose_quote_is_absent():
    data = {
        "place": {"state": "CA"},
        "rules": [
            {"feature": "setback", "verbatim": "set back 500 FEET"},
            {"feature": "lot", "verbatim": "minimum lot of 40 acres"},
            {"feature": "fence", "verbatim": None},
        ],
    }
    warnings = verify_quotes(data, ROWS_TEXT, rows=QuoteRows("rules", None, "verbatim"))
    assert [rule["feature"] for rule in data["rules"]] == ["setback"]
    assert data["place"] == {"state": "CA"}
    assert warnings == [
        'unverified quote at rules[1].verbatim: "minimum lot of 40 acres"',
        "unverified quote at rules[2].verbatim: null",
    ]


def test_rows_mode_drops_child_rows_and_items_left_without_rows():
    data = {
        "tariffs": [
            {
                "name": "R-1",
                "charges": [
                    {"amount": "500", "text": "set back 500 feet"},
                    {"amount": "9", "text": "fabricated charge"},
                ],
            },
            {"name": "R-2", "charges": [{"amount": "1", "text": "also fabricated"}]},
        ]
    }
    warnings = verify_quotes(data, ROWS_TEXT, rows=QuoteRows("tariffs", "charges", "text"))
    assert data["tariffs"] == [
        {"name": "R-1", "charges": [{"amount": "500", "text": "set back 500 feet"}]}
    ]
    assert warnings == [
        'unverified quote at tariffs[0].charges[1].text: "fabricated charge"',
        'unverified quote at tariffs[1].charges[0].text: "also fabricated"',
    ]


def test_rows_mode_reads_a_parent_quote_for_child_rows():
    data = {
        "tariffs": [
            {"text": "Noise is limited to 55 dBA", "charges": [{"amount": "55"}, {"amount": "56"}]},
            {"text": "Noise is limited to 60 dBA", "charges": [{"amount": "60"}]},
        ]
    }
    warnings = verify_quotes(data, ROWS_TEXT, rows=QuoteRows("tariffs", "charges", "text"))
    assert [item["text"] for item in data["tariffs"]] == ["Noise is limited to 55 dBA"]
    assert len(data["tariffs"][0]["charges"]) == 2 and len(warnings) == 1


def test_without_rows_or_evidence_nothing_is_checked():
    data = {"rules": [{"verbatim": "fabricated"}]}
    assert verify_quotes(data, ROWS_TEXT) == []
    assert data == {"rules": [{"verbatim": "fabricated"}]}


@pytest.mark.parametrize(
    ("quote", "present"),
    [
        ("Wells shall be set back \u2026 Noise is limited to 55 dBA.", True),
        ("Wells shall be set back... 55 dBA", True),
        ("Wells shall be set back 500 feet.", True),
        ("Noise is limited \u2026 Wells shall be set back", False),  # parts out of order
        ("Wells shall be set back \u2026 60 dBA", False),
        ("Wells shall be set \u2026 ack 500 feet", False),  # parts keep word boundaries
        ("\u2026", False),
        (None, False),
    ],
)
def test_elided_quotes_match_their_parts_in_order(quote, present):
    assert quote_present(quote, ROWS_TEXT) is present
