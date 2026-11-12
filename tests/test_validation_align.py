"""Alignment: columns, reference headers, keys, scope, ambiguity, quote anchor, roster lockstep."""

from __future__ import annotations

import csv
import json
import logging
import sqlite3

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.extraction.roster import load_roster
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.validation.align import (
    ANCHOR,
    MAX_SCOPE_ROWS,
    Cell,
    Source,
    _clean,
    _containment,
    _grams,
    align,
    columns,
)

TEXT = {"type": ["string", "null"]}
ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "jurisdiction": {
            "type": "object",
            "properties": {"state": {"type": "string"}, "county": {"type": "string"}},
        },
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "feature": {"type": "string"},
                    "rule_kind": {"type": "string"},
                    "value": TEXT,
                    "units": TEXT,
                    "summary": {**TEXT, "description": "Plain-language summary"},
                    "notes": TEXT,
                    "source_verbatim": TEXT,
                },
            },
        },
    },
    "$metadata": {
        "extraction": {"main_data_array": "rules", "context_objects": ["jurisdiction"]},
        "identity": {
            "deduplication": {"key_fields": ["state", "county", "feature", "rule_kind", "value"]}
        },
        "compilation": {
            "flattening": {
                "unit_fields": ["units"],
                "unit_normalizations": {"ft": "feet", "ft.": "feet"},
            },
            "normalization": {"state_column": "state"},
            "output": {"exclude_fields": ["notes"]},
        },
    },
}
KEYS = ["state", "county", "feature", "rule_kind"]
HEADER = ["state", "county", "feature", "rule_kind", "value", "units", "summary", "source_verbatim"]
EVIDENCE = {
    "type": "object",
    "properties": {"value": TEXT, "quote": TEXT},
    "required": ["value", "quote"],
}
ROSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "title": "State"},
                    "ahj": {"type": "string", "title": "AHJ Name"},
                    "online": {
                        **EVIDENCE,
                        "title": "Online Permitting",
                        "description": "Online portal",
                    },
                    "fee": {**EVIDENCE, "title": "Permit Fee"},
                    "note": TEXT,
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
    {"state": "AZ", "ahj": "Maricopa County", "geo_id": "4013"},
    {"state": "AZ", "ahj": "Pima County", "geo_id": "4019"},
    {"state": "CO", "ahj": "Boulder County", "geo_id": "8013"},
]
ROSTER_INPUTS = {"targets": TARGETS, "partition_by": ["state", "ahj"]}
ROSTER_HEADER = [
    "State",
    "AHJ Name",
    "Online Permitting",
    "Permit Fee",
    "note",
    "geo_id",
    "sources",
    "confidence",
    "evidence",
]
QUOTES = {
    "noise55": "Noise shall not exceed 55 dBA measured at the nearest property line.",
    "noise50": "Noise shall not exceed 50 dBA measured at the nearest property line.",
    "start": "Construction activity may begin at 7:00 a.m. on weekdays only.",
    "end": "All drilling operations must cease by 7:00 p.m. each evening.",
    "fence": "A security fence is required around the entire well pad.",
    "setback": "Wells shall be set back a minimum of 500 feet from residences.",
}


def _metadata(tmp_path, schema=ROWS_SCHEMA):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return SchemaMetadata(path)


def _table(path, header, rows, *, bom=False):
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _rows_table(tmp_path, label, rows, header=HEADER, role="primary"):
    full = [[row.get(h, "") for h in header] for row in rows]
    return Source(label, role, _table(tmp_path / f"{label}.csv", header, full), None)


def _rule(feature, rule_kind, value="", units="", quote="", state="CA", county="Lake", summary=""):
    return {
        "state": state,
        "county": county,
        "feature": feature,
        "rule_kind": rule_kind,
        "value": value,
        "units": units,
        "summary": summary,
        "source_verbatim": quote,
    }


def _align(
    metadata,
    sources,
    *,
    roster=None,
    key_fields=KEYS,
    quote_field="source_verbatim",
    fields=None,
    staging=None,
):
    references = [s for s in sources if s.role == "reference"]
    cols = columns(
        metadata,
        roster,
        key_fields=key_fields,
        quote_field=quote_field,
        fields=fields,
        references=references,
    )
    stats: dict = {}
    clusters = list(
        align(
            sources,
            metadata=metadata,
            roster=roster,
            key_fields=key_fields,
            quote_field=quote_field,
            columns=cols,
            staging=staging or sqlite3.connect(":memory:"),
            stats=stats,
        )
    )
    return clusters, stats, cols


def _by_match(clusters, match):
    return [c for c in clusters if c.match == match]


@pytest.mark.parametrize("roster_mode", [False, True])
def test_reference_assessment_masks_keep_blanks_distinct_from_labelled_absence(
    tmp_path, roster_mode
):
    if roster_mode:
        metadata = _metadata(tmp_path, ROSTER_SCHEMA)
        roster = load_roster(ROSTER_INPUTS, metadata)
        primary = Source(
            "p",
            "primary",
            _table(
                tmp_path / "p.csv",
                ROSTER_HEADER,
                [[t["state"], t["ahj"], "", "", "", t["geo_id"], "", "", ""] for t in TARGETS],
            ),
            None,
        )
        ref = Source(
            "r",
            "reference",
            _table(
                tmp_path / "r.csv",
                [
                    "State",
                    "AHJ Name",
                    "Online Permitting",
                    "Permit Fee",
                    "Online Permitting__assessed",
                ],
                [
                    ["AZ", "Maricopa County", "unknown", "NA", "false"],
                    ["AZ", "Pima County", "", "", "true"],
                ],
            ),
            None,
        )
        clusters, _, _ = _align(
            metadata, [primary, ref], roster=roster, quote_field=None, fields=["online", "fee"]
        )
        cells = [c.rows["r"][0] for c in clusters if "r" in c.rows]
        first, second = cells
        assert not first["online"].assessed and first["fee"].assessed
        assert second["online"].assessed and not second["fee"].assessed
    else:
        metadata = _metadata(tmp_path)
        primary = _rows_table(tmp_path, "p", [_rule("a", "b")])
        ref = _rows_table(
            tmp_path,
            "r",
            [{**_rule("a", "b", "unknown", "NA"), "value__assessed": "false"}],
            header=[*HEADER, "value__assessed"],
            role="reference",
        )
        clusters, _, _ = _align(metadata, [primary, ref], fields=["value", "units", "summary"])
        cells = clusters[0].rows["r"][0]
        assert not cells["value"].assessed
        assert cells["units"].assessed and cells["units"].value is None
        assert not cells["summary"].assessed


def test_invalid_assessment_mask_fails_before_scoring(tmp_path):
    metadata = _metadata(tmp_path)
    primary = _rows_table(tmp_path, "p", [_rule("a", "b")])
    ref = _rows_table(
        tmp_path,
        "r",
        [{**_rule("a", "b", "1"), "value__assessed": "maybe"}],
        header=[*HEADER, "value__assessed"],
        role="reference",
    )
    with pytest.raises(ValueError, match=r"value__assessed.*true or false"):
        _align(metadata, [primary, ref], fields=["value"])


# ── Columns ────────────────────────────────────────────────────────────────


def test_rows_columns_exclude_keys_context_quote_and_excluded_fields(tmp_path):
    metadata = _metadata(tmp_path)
    cols = columns(
        metadata, None, key_fields=KEYS, quote_field="source_verbatim", fields=None, references=[]
    )
    assert cols.scored == ("value", "units", "summary")
    assert cols.focus == cols.scored
    assert cols.titles == {f: f for f in (*KEYS, "value", "units", "summary", "source_verbatim")}
    assert cols.descriptions == {"value": "", "units": "", "summary": "Plain-language summary"}
    assert cols.unscored == {}

    subset = columns(
        metadata,
        None,
        key_fields=["state", "feature"],
        quote_field=None,
        fields=["summary", "rule_kind"],
        references=[],
    )
    assert subset.scored == ("rule_kind", "value", "units", "summary", "source_verbatim")
    assert subset.focus == ("rule_kind", "summary")
    for bad in (["notes"], ["county"], ["feature"], ["Run Id"]):
        with pytest.raises(ValueError, match="not scorable"):
            columns(
                metadata,
                None,
                key_fields=["state", "feature"],
                quote_field=None,
                fields=bad,
                references=[],
            )
    with pytest.raises(ValueError, match="unknown key field"):
        columns(
            metadata,
            None,
            key_fields=["state", "nope"],
            quote_field=None,
            fields=None,
            references=[],
        )


def test_roster_columns_score_attributes_only(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    roster = load_roster(ROSTER_INPUTS, metadata)
    cols = columns(metadata, roster, key_fields=(), quote_field=None, fields=None, references=[])
    assert cols.scored == ("online", "fee", "note")
    assert cols.titles == {
        "state": "State",
        "ahj": "AHJ Name",
        "online": "Online Permitting",
        "fee": "Permit Fee",
        "note": "note",
    }
    assert cols.descriptions["online"] == "Online portal"


def test_reference_headers(tmp_path, caplog):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    roster = load_roster(ROSTER_INPUTS, metadata)

    def check(header, rows=(), fields=None):
        source = Source("h", "reference", _table(tmp_path / "h.csv", header, rows, bom=True), None)
        return columns(
            metadata, roster, key_fields=(), quote_field=None, fields=fields, references=[source]
        )

    with caplog.at_level(logging.WARNING):
        cols = check(["State", "AHJ Name", "Online Permitting", "fee", "note", "extra"])
    assert cols.unscored == {"h": ("fee",)}
    assert "Permit Fee" in caplog.text
    with pytest.raises(ValueError, match=r"missing key column.*AHJ Name"):
        check(["State", "ahj", "Online Permitting"])
    with pytest.raises(ValueError, match="no focus column"):
        check(["State", "AHJ Name", "Permit Fee"], fields=["online"])
    with pytest.raises(ValueError, match=r"repeated header.*Permit Fee"):
        check(["State", "AHJ Name", "Permit Fee", "Permit Fee"])
    with pytest.raises(ValueError, match="duplicate roster target"):
        check(
            ["State", "AHJ Name", "Permit Fee"],
            [["AZ", "Pima County", "1"], ["az", "pima  county.", "2"]],
        )
    assert check(["State", "AHJ Name", "Permit Fee"], [["AZ", "Pima County", "1"]]).unscored == {
        "h": ("online", "note"),
    }


# ── Keys, scope, ambiguity ─────────────────────────────────────────────────


def test_keys_fold_numbers_case_punctuation_and_blanks(tmp_path):
    metadata = _metadata(tmp_path)
    keys = ["state", "county", "feature", "value"]
    p = _rows_table(
        tmp_path,
        "P",
        [
            _rule("Set-Back", "", value="500.0", units="feet"),
            _rule("lot", "", value="", units="acres", county=""),
        ],
    )
    a = _rows_table(
        tmp_path,
        "A",
        [
            _rule("set  back", "", value="500", units="ft"),
            _rule("LOT.", "", value=" ", units="acres", county="NA"),
        ],
        role="challenger",
    )
    clusters, stats, _ = _align(
        metadata, [p, a], key_fields=keys, quote_field=None, fields=["units"]
    )
    assert stats["match"] == {"key": 2, "anchor": 0}
    assert stats["clusters"] == 2
    assert [c.scope for c in clusters] == [("CA", ""), ("CA", "Lake")]
    assert [c.key for c in clusters] == [("CA", "", "lot", ""), ("CA", "Lake", "Set-Back", "500.0")]
    assert clusters[1].rows["A"][0]["units"] == Cell("ft", None, None, False)
    assert clusters[0].rows["P"][0] == {"units": Cell("acres", None, None, False)}


def test_ambiguous_keys_are_reported_not_paired(tmp_path):
    metadata = _metadata(tmp_path)
    rows = [_rule("hours", "operating", value="7:00"), _rule("hours", "operating", value="19:00")]
    p = _rows_table(tmp_path, "P", rows)
    a = _rows_table(tmp_path, "A", [rows[1], rows[0]], role="challenger")
    b = _rows_table(tmp_path, "B", [rows[0]], role="challenger")
    clusters, stats, _ = _align(metadata, [p, a, b], quote_field=None)
    ambiguous, partial = clusters
    assert ambiguous.match == "ambiguous"
    assert {label: len(rows) for label, rows in ambiguous.rows.items()} == {"P": 2, "A": 2}
    assert [r["value"].value for r in ambiguous.rows["A"]] == ["19:00", "7:00"]
    assert partial.match == "key" and set(partial.rows) == {"B"}
    assert stats["ambiguous"] == {"P": 2, "A": 2, "B": 0}
    assert stats["match"] == {"key": 0, "anchor": 0}
    assert stats["clusters"] == 2


# ── Quote anchor ───────────────────────────────────────────────────────────


def test_containment_and_minimum_chars():
    assert _grams("short quote") == frozenset()
    full = _grams("Wells shall be set back 500 feet")
    part = _grams("  SET   BACK 500 feet ")
    assert _containment(full, part) == 1.0
    assert _containment(_grams("abcdefghijklmnop"), _grams("abcdefghijkl zzzz")) == pytest.approx(
        9 / 13
    )
    assert ANCHOR == 0.65


def test_anchor_merges_paraphrased_keys_greedily_with_tie_break(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(tmp_path, "P", [_rule("noise", "maximum", "55", quote=QUOTES["noise55"])])
    a = _rows_table(
        tmp_path,
        "A",
        [
            _rule("noise", "limit", "50", quote=QUOTES["noise55"]),
            _rule("noise", "cap", "50", quote=QUOTES["noise55"]),
        ],
        role="challenger",
    )
    clusters, stats, _ = _align(metadata, [p, a])
    anchored = _by_match(clusters, "anchor")
    assert len(anchored) == 1 and anchored[0].key[3] == "maximum"
    assert anchored[0].rows["A"][0]["value"].value == "50"
    leftover = [c for c in clusters if c.match == "key"]
    assert [c.key[3] for c in leftover] == ["cap"]
    assert stats["match"] == {"key": 0, "anchor": 1}


def test_anchor_keeps_one_row_per_source_and_merges_three_ways(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(
        tmp_path,
        "P",
        [
            _rule("noise", "maximum", "55", quote=QUOTES["noise55"]),
            _rule("noise", "max", "55", quote=QUOTES["noise55"]),
        ],
    )
    a = _rows_table(
        tmp_path, "A", [_rule("noise", "limit", "50", quote=QUOTES["noise50"])], role="challenger"
    )
    b = _rows_table(
        tmp_path, "B", [_rule("Noise", "ceiling", "55", quote=QUOTES["noise55"])], role="challenger"
    )
    clusters, stats, _ = _align(metadata, [p, a, b])
    (merged,) = _by_match(clusters, "anchor")
    assert set(merged.rows) == {"P", "A", "B"}
    assert merged.rows["P"][0]["value"].quote == QUOTES["noise55"]
    assert merged.key[3] == "maximum"
    (single,) = _by_match(clusters, "key")
    assert set(single.rows) == {"P"} and single.key[3] == "max"
    assert stats["match"] == {"key": 0, "anchor": 1}


def test_reference_takes_part_in_anchor_only_with_quote_column(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(tmp_path, "P", [_rule("noise", "maximum", "55", quote=QUOTES["noise55"])])
    h_rows = [_rule("noise", "limit", "55", quote=QUOTES["noise55"])]
    with_quote = _rows_table(tmp_path, "H", h_rows, role="reference")
    clusters, _, _ = _align(metadata, [p, with_quote])
    (cluster,) = clusters
    assert cluster.match == "anchor" and cluster.in_scope == frozenset({"H"})
    assert cluster.rows["H"][0]["value"] == Cell("55", None, None, False)

    no_quote = _rows_table(tmp_path, "H", h_rows, header=HEADER[:-1], role="reference")
    clusters, stats, cols = _align(metadata, [p, no_quote])
    assert [c.match for c in clusters] == ["key", "key"]
    assert stats["match"] == {"key": 0, "anchor": 0}
    assert cols.unscored == {"H": ()}


def test_rows_scenario_s5_from_compiled_tables(tmp_path):
    metadata = _metadata(tmp_path)
    compiler = DataCompiler(metadata, {}, verbose=False)

    def contender(label, role, items):
        records = tmp_path / f"{label}-records"
        records.mkdir()
        payload = {"jurisdiction": {"state": "California", "county": "Lake"}, "rules": items}
        (records / "doc.json").write_text(json.dumps({"document": {}, "payload": payload}))
        frame, _ = compiler.compile_from_directory(records)
        compiler.export(frame, tmp_path / label, "table", ["csv"])
        return Source(label, role, tmp_path / label / "table.csv", label.lower())

    def item(feature, rule_kind, value, units, summary, quote):
        return {
            "feature": feature,
            "rule_kind": rule_kind,
            "value": value,
            "units": units,
            "summary": summary,
            "notes": None,
            "source_verbatim": quote,
        }

    p = contender(
        "P",
        "primary",
        [
            item(
                "setback", "minimum", "500", "feet", "500 ft setback from homes", QUOTES["setback"]
            ),
            item(
                "lot",
                "minimum",
                "1,500",
                "acres",
                None,
                "Minimum lot size is 1,500 acres per site.",
            ),
            item("noise", "maximum", "55", "dBA", None, QUOTES["noise55"]),
            item("hours", "operating", "7:00", None, None, QUOTES["start"]),
            item("hours", "operating", "19:00", None, None, QUOTES["end"]),
        ],
    )
    a = contender(
        "A",
        "challenger",
        [
            item(
                "setback",
                "minimum",
                "500",
                "ft",
                "Minimum 500-foot setback from residences",
                QUOTES["setback"],
            ),
            item(
                "lot", "minimum", "1500", "acres", None, "Minimum lot size is 1,500 acres per site."
            ),
            item("noise", "limit", "50", "dBA", None, QUOTES["noise50"]),
            item("hours", "operating", "19:00", None, None, QUOTES["end"]),
            item("hours", "operating", "7:00", None, None, QUOTES["start"]),
            item("fencing", "required", None, None, None, QUOTES["fence"]),
        ],
    )
    h = _rows_table(
        tmp_path,
        "H",
        [
            _rule("Setback", "Minimum", "500", "ft.", state="California"),
            _rule("lot", "minimum", "1500", "acres"),
            _rule("pad", "minimum", "2", "acres", county="Napa"),
        ],
        header=HEADER[:6],
        role="reference",
    )

    clusters, stats, cols = _align(metadata, [p, a, h])
    assert stats == {
        "match": {"key": 2, "anchor": 3},
        "ambiguous": {"P": 0, "A": 0, "H": 0},
        "out_of_scope": {"H": 1},
        "clusters": 6,
    }
    assert cols.unscored == {"H": ("summary",)}
    setback = next(c for c in clusters if c.key[2] == "setback")
    assert {label: rows[0]["units"].value for label, rows in setback.rows.items()} == {
        "P": "feet",
        "A": "feet",
        "H": "feet",
    }
    assert setback.rows["P"][0]["units"].quote == QUOTES["setback"]
    assert setback.rows["H"][0]["units"].quote is None
    assert "summary" not in setback.rows["H"][0]
    (fence,) = [c for c in clusters if c.key[2] == "fencing"]
    assert fence.match == "key" and set(fence.rows) == {"A"} and fence.in_scope == {"H"}
    (outside,) = _by_match(clusters, "out_of_scope")
    assert outside.scope == ("CA", "Napa") and set(outside.rows) == {"H"}
    hours = [c for c in clusters if c.key[2] == "hours"]
    assert {c.rows["P"][0]["value"].value: c.rows["A"][0]["value"].value for c in hours} == {
        "7:00": "7:00",
        "19:00": "19:00",
    }

    clusters, stats, _ = _align(metadata, [p, a, h], quote_field=None)
    assert stats["match"] == {"key": 2, "anchor": 0}
    (ambiguous,) = _by_match(clusters, "ambiguous")
    assert {label: len(rows) for label, rows in ambiguous.rows.items()} == {"P": 2, "A": 2}
    assert len([c for c in clusters if c.key[2] == "noise"]) == 2


# ── Scope limit, reference scope, staging ──────────────────────────────────


def test_scope_limit_applies_only_with_quote_field(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(
        tmp_path,
        "P",
        [
            _rule(f"f{i}", "minimum", quote=f"distinct quote number {i}")
            for i in range(MAX_SCOPE_ROWS + 1)
        ],
    )
    references = []
    cols = columns(
        metadata,
        None,
        key_fields=KEYS,
        quote_field="source_verbatim",
        fields=None,
        references=references,
    )
    with pytest.raises(
        ValueError, match=r"scope \(CA, Lake\) has 5001 rows \(> 5000\): scope too broad"
    ):
        align(
            [p],
            metadata=metadata,
            roster=None,
            key_fields=KEYS,
            quote_field="source_verbatim",
            columns=cols,
            staging=sqlite3.connect(":memory:"),
            stats={},
        )
    clusters, stats, _ = _align(metadata, [p], quote_field=None)
    assert len(clusters) == stats["clusters"] == MAX_SCOPE_ROWS + 1


def test_reference_only_scope_is_out_of_scope(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(
        tmp_path, "P", [_rule("lot", "minimum", "2"), _rule("lot", "minimum", "3", county="Napa")]
    )
    h = _rows_table(
        tmp_path,
        "H",
        [
            _rule("lot", "minimum", "2"),
            _rule("lot", "minimum", "4", county="Yolo"),
            _rule("pad", "minimum", "1", county="Yolo"),
        ],
        role="reference",
    )
    clusters, stats, _ = _align(metadata, [p, h])
    assert stats["out_of_scope"] == {"H": 2}
    assert [(c.scope, c.match, c.in_scope) for c in clusters] == [
        (("CA", "Lake"), "key", frozenset({"H"})),
        (("CA", "Napa"), "key", frozenset()),
        (("CA", "Yolo"), "out_of_scope", frozenset({"H"})),
        (("CA", "Yolo"), "out_of_scope", frozenset({"H"})),
    ]


def test_staging_connection_accepts_writes_while_streaming(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(
        tmp_path, "P", [_rule(f"f{i}", "minimum", county=f"c{i % 7}") for i in range(300)]
    )
    a = _rows_table(
        tmp_path,
        "A",
        [_rule(f"f{i}", "minimum", county=f"c{i % 7}") for i in range(300)],
        role="challenger",
    )
    staging = sqlite3.connect(":memory:")
    staging.execute("CREATE TABLE clusters (n INTEGER)")
    cols = columns(
        metadata, None, key_fields=KEYS, quote_field="source_verbatim", fields=None, references=[]
    )
    seen = 0
    for seen, _ in enumerate(
        align(
            [p, a],
            metadata=metadata,
            roster=None,
            key_fields=KEYS,
            quote_field="source_verbatim",
            columns=cols,
            staging=staging,
            stats={},
        ),
        start=1,
    ):
        staging.execute("INSERT INTO clusters VALUES (?)", (seen,))
        staging.commit()
    assert seen == 300
    assert staging.execute("SELECT COUNT(*) FROM clusters").fetchone() == (300,)


def test_missing_contender_column_is_an_error(tmp_path):
    metadata = _metadata(tmp_path)
    p = _rows_table(tmp_path, "P", [_rule("lot", "minimum")], header=HEADER[:-1])
    with pytest.raises(ValueError, match=r"P: missing column.*source_verbatim"):
        _align(metadata, [p])


def test_values_unescape_csv_literal_and_map_na():
    assert _clean("'-5 inspections") == "-5 inspections"
    assert _clean("'=SUM(A1)") == "=SUM(A1)"
    assert _clean("'quoted") == "'quoted"
    assert _clean(" NA ") == _clean("   ") == ""


# ── Roster mode ────────────────────────────────────────────────────────────


def _roster_row(state, ahj, online="NA", fee="NA", evidence=None):
    geo = {t["ahj"]: t["geo_id"] for t in TARGETS}.get(ahj, "NA")
    return [state, ahj, online, fee, "NA", geo, "NA", "none", json.dumps(evidence or {})]


def test_roster_cells_come_from_evidence_and_references_by_key(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    roster = load_roster(ROSTER_INPUTS, metadata)
    ev = {
        "online": {
            "value": "Yes",
            "quote": "Apply online",
            "url": "https://m.gov",
            "conflict": True,
        }
    }
    p = Source(
        "P",
        "primary",
        _table(
            tmp_path / "P.csv",
            ROSTER_HEADER,
            [
                _roster_row("AZ", "Maricopa County", online="Yes", evidence=ev),
                _roster_row("AZ", "Pima County", fee="'-5"),
                _roster_row("CO", "Boulder County"),
            ],
        ),
        "m",
    )
    h = Source(
        "h",
        "reference",
        _table(
            tmp_path / "h.csv",
            ["State", "AHJ Name", "Online Permitting"],
            [
                ["CO", "Boulder County", "No"],
                ["az", "maricopa county", "yes"],
                ["UT", "Salt Lake", "No"],
            ],
            bom=True,
        ),
        None,
    )
    clusters, stats, _ = _align(metadata, [p, h], roster=roster, key_fields=(), quote_field=None)
    assert [c.match for c in clusters] == ["key", "key", "key", "out_of_scope"]
    first = clusters[0]
    assert first.key == first.scope == ("AZ", "Maricopa County")
    assert first.rows["P"][0]["online"] == Cell("Yes", "Apply online", "https://m.gov", True)
    assert first.rows["P"][0]["fee"] == Cell(None, None, None, False)
    assert first.rows["h"][0] == {"online": Cell("yes", None, None, False)}
    assert first.in_scope == {"h"}
    assert clusters[1].in_scope == frozenset() and set(clusters[1].rows) == {"P"}
    assert clusters[1].rows["P"][0]["fee"].value == "-5"
    assert clusters[3].key == ("UT", "Salt Lake") and clusters[3].rows == {
        "h": ({"online": Cell("No", None, None, False)},),
    }
    assert stats == {
        "match": {"key": 2, "anchor": 0},
        "ambiguous": {"P": 0, "h": 0},
        "out_of_scope": {"h": 1},
        "clusters": 3,
    }


def test_roster_contenders_must_align_in_lockstep(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    roster = load_roster(ROSTER_INPUTS, metadata)
    rows = [_roster_row(t["state"], t["ahj"]) for t in TARGETS]
    p = Source("P", "primary", _table(tmp_path / "P.csv", ROSTER_HEADER, rows), None)
    swapped = Source(
        "A",
        "challenger",
        _table(tmp_path / "A.csv", ROSTER_HEADER, [rows[1], rows[0], rows[2]]),
        None,
    )
    with pytest.raises(ValueError, match=r"A: row 1 is .*Pima County.*recompile"):
        _align(metadata, [p, swapped], roster=roster, key_fields=(), quote_field=None)
    short = Source("B", "challenger", _table(tmp_path / "B.csv", ROSTER_HEADER, rows[:2]), None)
    with pytest.raises(ValueError, match="B: fewer rows than the roster's 3 targets"):
        _align(metadata, [p, short], roster=roster, key_fields=(), quote_field=None)
    extra = Source(
        "P",
        "primary",
        _table(tmp_path / "X.csv", ROSTER_HEADER, [*rows, _roster_row("UT", "Salt Lake")]),
        None,
    )
    with pytest.raises(ValueError, match="P: more rows than the roster's 3 targets"):
        _align(metadata, [extra], roster=roster, key_fields=(), quote_field=None)


def test_roster_compiled_table_round_trip(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    records = tmp_path / "records"
    records.mkdir()
    item = {
        "state": "AZ",
        "ahj": "Pima County",
        "online": {"value": "-Yes", "quote": "Portal live"},
        "fee": {"value": None, "quote": None},
        "note": None,
    }
    (records / "a.json").write_text(
        json.dumps(
            {
                "document": {"source_url": "https://p.gov", "source_scope": "target"},
                "payload": {"rows": [item]},
            }
        )
    )
    compiler = DataCompiler(metadata, ROSTER_INPUTS, verbose=False)
    frame, _ = compiler.compile_from_directory(records)
    compiler.export(frame, tmp_path / "out", "table", ["csv"])
    p = Source("P", "primary", tmp_path / "out" / "table.csv", None)
    clusters, stats, _ = _align(
        metadata, [p], roster=compiler.roster, key_fields=(), quote_field=None
    )
    assert [c.key for c in clusters] == [(t["state"], t["ahj"]) for t in TARGETS]
    assert clusters[1].rows["P"][0]["online"] == Cell("-Yes", "Portal live", "https://p.gov", False)
    assert clusters[0].rows["P"][0]["online"].value is None
    assert stats["match"]["key"] == 0
