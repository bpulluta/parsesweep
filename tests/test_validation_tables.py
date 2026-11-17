"""Tests for validation/tables.py: diff, consensus, metrics, priority and outputs."""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from pathlib import Path

import pytest

from psweep.utils.evidence import normalize
from psweep.validation.align import Cell, Cluster, Columns, Source
from psweep.validation.judge import Item, Verdict, class_key
from psweep.validation.tables import (
    HIGH,
    LOW,
    MEDIUM,
    NONE,
    SUMMARY_COLUMNS,
    category,
    leaderboard,
    majority,
    pair_priority,
    status,
    validate_tables,
    wilson,
)


def source(label: str, role: str = "challenger") -> Source:
    return Source(label, role, Path(f"{label}.csv"), None if role == "reference" else f"m-{label}")


def make_columns(*fields: str, keys=("id",), unscored=None) -> Columns:
    return Columns(
        scored=fields,
        focus=fields,
        titles={**{k: k.upper() for k in keys}, **{f: f.title() for f in fields}},
        descriptions={f: f"{f} description" for f in fields},
        unscored=unscored or {},
    )


def _cell(value) -> Cell:
    if isinstance(value, Cell):
        return value
    if isinstance(value, tuple):
        return Cell(value[0], value[1], None, False)
    return Cell(value, None, None, False)


def cluster(key, rows, *, match="key", in_scope=(), scope=None) -> Cluster:
    """``rows``: label -> {field: value | (value, quote) | Cell}, or a list of those (ambiguous)."""
    built = {
        label: tuple(
            {f: _cell(v) for f, v in r.items()} for r in (row if isinstance(row, list) else [row])
        )
        for label, row in rows.items()
    }
    key = tuple(key)
    return Cluster(
        key, tuple(scope) if scope is not None else key, match, built, frozenset(in_scope)
    )


class FakeJudge:
    """Verdicts from ``rule(item) -> (support, groups, reason)``; records the items it saw."""

    def __init__(self, rule):
        self.rule, self.items, self.calls = rule, [], 0

    def __call__(self, items):
        items = list(items)
        self.items += items
        self.calls += bool(items)
        verdicts = {i.key: Verdict(*self.rule(i), 0.001, 10, 2) for i in items}
        usage = {
            "calls": int(bool(items)),
            "cache_hits": 0,
            "failed": 0,
            "cost_usd": 0.001 * len(items),
            "input_tokens": 10,
            "output_tokens": 2,
        }
        return verdicts, usage


def by_value(table: dict[str, tuple[str, int]]):
    """Judge rule keyed by candidate display value: value -> (support, group)."""

    def rule(item: Item):
        found = [table.get(c.value, ("unclear", n + 100)) for n, c in enumerate(item.candidates)]
        return tuple(s for s, _ in found), tuple(g for _, g in found), "reason"

    return rule


def run(
    tmp_path,
    clusters,
    sources,
    columns,
    *,
    judge=None,
    covered=frozenset(),
    min_accuracy=None,
    align_stats=None,
    quality=None,
    out="out",
):
    out_dir = tmp_path / out
    staging = sqlite3.connect(tmp_path / f"{out}.sqlite")
    stats = align_stats or {
        "match": {"key": 0, "anchor": 0},
        "ambiguous": {},
        "out_of_scope": {},
        "clusters": len(clusters),
    }
    summary = validate_tables(
        iter(clusters),
        sources=sources,
        columns=columns,
        judge_model={"model": "nano"} if judge else None,
        judge_fn=judge,
        staging=staging,
        out_dir=out_dir,
        quality=quality or {},
        align_stats=stats,
        inputs={"judge": "judge" if judge else None, "fields": None},
        run={"llm_calls": {}, "cost_usd": {}},
        covered=covered,
        min_accuracy=min_accuracy,
    )
    staging.close()
    with (out_dir / "cells.csv").open(encoding="utf-8") as handle:
        cells = list(csv.DictReader(handle))
    return summary, cells, out_dir


def roster_covered(*keys) -> frozenset:
    return frozenset(tuple(normalize(p) for p in k) for k in keys)


# -- reference quality metrics ------------------------------------------------


def test_populated_precision_coverage_and_partial_labels(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    unknown = Cell("unknown", None, None, False, assessed=False)
    unlabelled = Cell(None, None, None, False, assessed=False)
    clusters = [
        cluster(
            ("a",),
            {"primary": {"x": "1", "y": "9"}, "ref": {"x": "1", "y": unknown}},
            in_scope=("ref",),
        ),
        cluster(
            ("b",),
            {"primary": {"x": "2", "y": None}, "ref": {"x": "3", "y": None}},
            in_scope=("ref",),
        ),
        cluster(
            ("c",),
            {"primary": {"x": None, "y": None}, "ref": {"x": "4", "y": unlabelled}},
            in_scope=("ref",),
        ),
        cluster(("d",), {"primary": {"x": "5", "y": None}}),
    ]
    summary, _, out = run(
        tmp_path,
        clusters,
        sources,
        make_columns("x", "y"),
        covered=roster_covered(("a",), ("b",), ("d",)),
        min_accuracy=0.5,
    )
    model = summary["models"]["primary"]
    metrics = model["overall"]["ref"]
    assert (
        metrics["assessed_populated"],
        metrics["correct_populated"],
        metrics["reference_populated"],
    ) == (2, 1, 3)
    assert metrics["populated_answer_precision"] == 0.5
    assert metrics["answer_coverage"] == 0.5
    assert metrics["reference_supported_recall"] == 0.3333
    assert (metrics["cells"], metrics["correct"], metrics["accuracy"]) == (4, 2, 0.5)
    assert summary["gate"]["passed"]  # assessed all-cell accuracy, not populated precision
    assert (metrics["unassessed_populated"], metrics["contradictions"], metrics["abstentions"]) == (
        2,
        1,
        4,
    )
    assert metrics["ci95"]["populated_answer_precision"] is None
    field = model["columns"]["X"]["ref"]
    assert field["ci95"]["populated_answer_precision"] == wilson(1, 2)
    target = model["targets"]["ref"]
    assert (
        target["targets"],
        target["answered_targets"],
        target["no_answer_targets"],
        target["no_source_targets"],
    ) == (4, 3, 1, 1)
    assert (target["fully_assessed_answered_targets"], target["all_answered_correct_targets"]) == (
        1,
        0,
    )
    assert target["ci95"]["all_answered_correctness"] == wilson(0, 1)
    assert "assessed_populated" in (out / "summary.csv").read_text().splitlines()[0]
    assert summary["metric_semantics"]["reference_blank"] == "unlabelled"


def test_cost_latency_amortized_over_all_roster_targets_and_conflicts(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(
            ("a",),
            {"primary": {"x": Cell("yes", None, None, True)}, "ref": {"x": "yes"}},
            in_scope=("ref",),
        ),
        cluster(("b",), {"primary": {"x": None}, "ref": {"x": "yes"}}, in_scope=("ref",)),
    ]
    summary, _, _ = run(
        tmp_path,
        clusters,
        sources,
        make_columns("x"),
        quality={"primary": {"cost_usd": 0.12, "seconds_per_document": 3, "documents_called": 2}},
    )
    model = summary["models"]["primary"]
    assert model["quality"]["cost_per_target"] == pytest.approx(0.06)
    assert model["quality"]["seconds_per_target"] == 3
    assert model["quality"]["target_denominator"] == 2
    assert model["overall"]["ref"]["conflicts"] == 1


def test_missing_rows_reduce_coverage_and_reference_recall(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(("a",), {"primary": {"x": "1"}, "ref": {"x": "1"}}, in_scope=("ref",)),
        cluster(("b",), {"ref": {"x": "2"}}, in_scope=("ref",)),
    ]
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x"), covered=None)
    model = summary["models"]["primary"]
    metrics = model["overall"]["ref"]
    assert (
        metrics["answer_opportunities"],
        metrics["populated_answers"],
        metrics["abstentions"],
    ) == (2, 1, 1)
    assert (
        metrics["answer_coverage"],
        metrics["reference_supported_recall"],
        metrics["accuracy"],
    ) == (0.5, 0.5, 0.5)
    assert model["targets"]["ref"]["missing_row_targets"] == 1
    assert model["targets"]["ref"]["no_answer_targets"] == 1


def test_unknown_reference_does_not_reward_abstention_or_become_truth(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(
            ("a",),
            {
                "primary": {"x": None},
                "ref": {"x": Cell("unknown", None, None, False, assessed=False)},
            },
            in_scope=("ref",),
        )
    ]
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x"), min_accuracy=0.5)
    metrics = summary["models"]["primary"]["overall"]["ref"]
    assert metrics["accuracy"] is None and metrics["cells"] == 0
    assert metrics["populated_answer_precision"] is None
    assert metrics["reference_supported_recall"] is None
    assert not summary["gate"]["passed"]


def test_entire_no_source_scope_remains_in_new_denominators(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(
            ("a", "1"),
            {"primary": {"x": "yes"}, "ref": {"x": "yes"}},
            scope=("a",),
            in_scope=("ref",),
        ),
        cluster(
            ("b", "1"), {"ref": {"x": "yes"}}, scope=("b",), match="out_of_scope", in_scope=("ref",)
        ),
    ]
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x"), covered=None)
    model = summary["models"]["primary"]
    metrics = model["overall"]["ref"]
    assert metrics["answer_coverage"] == 0.5
    assert metrics["reference_supported_recall"] == 0.5
    assert metrics["accuracy"] == 1.0  # legacy scope exclusion unchanged
    assert model["targets"]["ref"]["targets"] == 2
    assert model["targets"]["ref"]["missing_row_targets"] == 1


def test_target_interval_does_not_treat_dependent_cells_as_targets(tmp_path):
    sources = [source("primary", "primary"), source("a"), source("b"), source("ref", "reference")]
    fields = tuple(f"f{i}" for i in range(12))
    clusters = [
        cluster(
            (str(i),),
            {
                **{label: dict.fromkeys(fields, "yes") for label in ("primary", "a", "b")},
                "ref": dict.fromkeys(fields, "yes" if i == 0 else "no"),
            },
            in_scope=("ref",),
        )
        for i in range(2)
    ]
    summary, _, _ = run(tmp_path, clusters, sources, make_columns(*fields))
    model = summary["models"]["primary"]
    assert model["overall"]["ref"]["populated_answer_precision"] == 0.5
    target = model["targets"]["ref"]
    assert target["ci95"]["all_answered_correctness"] == wilson(1, 2)
    assert target["ci95"]["all_answered_correctness"] != wilson(12, 24)
    assert model["overall"]["consensus"]["metric_kind"] == "model_agreement"
    assert "populated_answer_precision" not in model["overall"]["consensus"]


def test_rows_in_one_scope_share_target_denominator(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(
            ("group", str(i)),
            {"primary": {"x": "yes"}, "ref": {"x": "yes" if i == 0 else "no"}},
            scope=("group",),
            in_scope=("ref",),
        )
        for i in range(2)
    ]
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x"), covered=None)
    model = summary["models"]["primary"]
    assert model["targets"]["ref"]["targets"] == 1
    assert model["targets"]["ref"]["all_answered_correctness"] == 0
    assert model["columns"]["X"]["ref"]["ci95"]["populated_answer_precision"] is None


# -- primitives ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("classes", "expected"),
    [
        ({"p": None, "a": None}, "all_na"),
        ({"p": "n:1", "a": "n:1"}, "unanimous"),
        ({"p": "n:1", "a": None}, "partial"),
        ({"p": "n:1", "a": "n:2"}, "disputed"),
        ({"p": None, "a": None, "h": "n:1"}, "reference_only"),
        ({"p": None, "h": "n:1", "g": "n:2"}, "reference_only"),
    ],
)
def test_status(classes, expected):
    assert status(classes, ("p", "a")) == expected


@pytest.mark.parametrize(
    ("x", "base", "expected"),
    [
        (None, None, "both_na"),
        ("a", "a", "agree"),
        ("a", "b", "changed"),
        ("a", None, "new"),
        (None, "a", "missing"),
    ],
)
def test_category(x, base, expected):
    assert category(x, base) == expected


def test_majority_strict_with_na_as_class_and_ties_split():
    assert majority(["a", "a", None]) == (True, "a")
    assert majority([None, None, "a"]) == (True, None)
    assert majority(["a", "a", None, None]) == (False, None)
    assert majority(["a"]) == (False, None)
    assert majority(["a", "b", "c"]) == (False, None)


def test_wilson_interval():
    lo, hi = wilson(8, 9)
    assert lo == pytest.approx(0.565, abs=1e-3) and hi == pytest.approx(0.980, abs=1e-3)
    assert wilson(7, 27) == pytest.approx([0.132, 0.447], abs=1e-3)
    assert wilson(0, 0) is None


# One case per §3f table row: (category, S reference, judged, kwargs, expected).
PRIORITY_TABLE = [
    ("agree", False, False, {}, LOW),
    ("both_na", False, False, {}, NONE),
    ("agree", True, True, {}, LOW),
    ("changed", False, False, {}, HIGH),
    ("changed", True, False, {}, HIGH),
    ("changed", False, True, {"same": True}, LOW),
    ("changed", False, True, {"p": "supported", "s": "unsupported"}, LOW),
    ("changed", True, True, {"p": "supported", "s": "unclear"}, MEDIUM),
    ("changed", False, True, {"p": "supported", "s": "supported"}, HIGH),
    ("changed", False, True, {"p": "unclear", "s": "unsupported"}, HIGH),
    ("missing", False, False, {}, MEDIUM),
    ("missing", True, False, {}, LOW),
    ("missing", False, True, {"s": "supported"}, HIGH),
    ("missing", True, True, {"s": "supported"}, HIGH),
    ("missing", False, True, {"s": "unsupported"}, LOW),
    ("missing", False, True, {"s": "unclear"}, MEDIUM),
    ("new", False, False, {}, MEDIUM),
    ("new", False, False, {"corroborated": True}, LOW),
    ("new", True, False, {"corroborated": True}, MEDIUM),
    ("new", False, True, {"p": "supported"}, LOW),
    ("new", False, True, {"p": "unsupported"}, HIGH),
    ("new", True, True, {"p": "unclear"}, MEDIUM),
]


@pytest.mark.parametrize(("cat", "reference", "judged", "kwargs", "expected"), PRIORITY_TABLE)
def test_pair_priority_table(cat, reference, judged, kwargs, expected):
    assert pair_priority(cat, reference=reference, judged=judged, **kwargs) == expected


# -- S2: model comparison, 5 contenders, no reference ----------------------------

S2 = {
    "t1": [10, 10, 10, 10, 10],
    "t2": [10, 10, 10, 12, 10],
    "t3": [None, 5, 5, 5, None],
    "t4": [7, None, None, None, None],
}
S2_LABELS = ("primary", "a", "b", "c", "d")


def s2_setup(labels=S2_LABELS, order=None):
    sources = [source("primary", "primary"), *(source(label) for label in labels[1:])]
    clusters = []
    for target in order or S2:
        values = dict(zip(S2_LABELS, S2[target], strict=True))
        rows = {
            label: {
                "x": (str(values[label]), f"quote says {values[label]}")
                if values[label] is not None
                else None
            }
            for label in labels
        }
        clusters.append(cluster((target,), rows))
    return sources, clusters


S2_VERDICTS = by_value(
    {"10": ("supported", 0), "12": ("unsupported", 1), "5": ("supported", 0), "7": ("supported", 0)}
)


def test_s2_consensus_judge_support_agreement_and_priority(tmp_path):
    sources, clusters = s2_setup()
    judge = FakeJudge(S2_VERDICTS)
    summary, cells, out = run(
        tmp_path,
        clusters,
        sources,
        make_columns("x"),
        judge=judge,
        covered=roster_covered(*[(t,) for t in S2]),
    )
    assert judge.calls == 1 and len(judge.items) == 3
    assert {tuple(c.value for c in i.candidates) for i in judge.items} == {
        ("10", "12"),
        ("5",),
        ("7",),
    }
    m = summary["models"]
    p = m["primary"]["overall"]["consensus"]
    assert (p["tp"], p["fp"], p["fn"]) == (2, 1, 1)
    assert (p["precision"], p["recall"], p["f1"]) == (0.6667, 0.6667, 0.6667)
    assert (p["precision_j"], p["recall_j"], p["f1_j"]) == (1.0, 0.6667, 0.8)
    for label in ("a", "b"):
        o = m[label]["overall"]["consensus"]
        assert (o["tp"], o["split"], o["precision"], o["recall"], o["f1"]) == (2, 1, 1.0, 1.0, 1.0)
    c = m["c"]["overall"]["consensus"]
    assert (c["precision"], c["recall"], c["f1"]) == (0.5, 0.5, 0.5)
    d = m["d"]["overall"]["consensus"]
    assert (d["precision"], d["recall"], d["f1"]) == (1.0, 0.6667, 0.8)
    assert {k: m[k]["overall"]["consensus"]["support_rate"] for k in S2_LABELS} == {
        "primary": 1.0,
        "a": 1.0,
        "b": 1.0,
        "c": 0.5,
        "d": 1.0,
    }
    agreement = summary["agreement"]["overall"]
    assert agreement["primary|c"]["rate"] == 0.5
    assert agreement["primary|a"]["rate"] == 1.0 and agreement["primary|a"]["one_sided"] == 2
    assert len(agreement) == 10
    priorities = {row["ID"]: row["review_priority"] for row in cells}
    assert priorities == {"t1": "low", "t2": "low", "t3": "high", "t4": "low"}
    assert next(row["ID"] for row in cells) == "t3"
    t2 = next(row for row in cells if row["ID"] == "t2")
    assert t2["judge_groups"] == "primary,a,b,d | c"
    assert (t2["primary_support"], t2["c_support"]) == ("supported", "unsupported")
    assert t2["status"] == "disputed" and t2["primary_quote"] == "quote says 10"
    assert summary["judge"]["items"] == 3 and summary["run"]["llm_calls"]["judge"] == 1
    assert summary["review"] == {
        "high": 1,
        "medium": 0,
        "low": 3,
        "none": 0,
        "high_by_column": {"X": 1},
    }
    lines = leaderboard(summary, "demo")
    assert lines[0].startswith("demo · 5 contenders · judge judge · focus X")
    assert lines[-1] == "Next: review — X 1 high"
    assert not any(line.startswith("Judge:") for line in lines)
    failed = {**summary, "judge": {**summary["judge"], "failed": 2, "error": "boom"}}
    assert "Judge: 2 of 3 items unjudged (boom); rerun to retry" in leaderboard(failed, "demo")

    first = {name: (out / name).read_bytes() for name in ("cells.csv", "summary.csv")}
    sources, scrambled = s2_setup(order=["t4", "t2", "t1", "t3"])
    run(
        tmp_path,
        scrambled,
        sources,
        make_columns("x"),
        judge=FakeJudge(S2_VERDICTS),
        covered=roster_covered(*[(t,) for t in S2]),
        out="again",
    )
    assert {name: (tmp_path / "again" / name).read_bytes() for name in first} == first


def test_no_consensus_below_three_contenders(tmp_path):
    sources, clusters = s2_setup(labels=("primary", "a"))
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x"))
    assert list(summary["models"]["primary"]["overall"]) == [""]
    assert "precision" not in summary["models"]["primary"]["overall"][""]
    assert summary["judge"] is None


# -- S1: QA/QC against one human sheet -----------------------------------------


def s1_setup():
    fields = tuple(f"f{i}" for i in range(1, 10))
    plan = {
        "t1": [("v", "v")] * 5 + [("v", None)] + [(None, "r")] * 3,
        "t2": [(None, "r")] * 9,
        "t3": [(None, "r")] * 7 + [(None, None)] * 2,
    }
    clusters = [
        cluster(
            (t,),
            {
                "primary": {
                    f: (p, "quote") if p else None for f, (p, _) in zip(fields, pairs, strict=True)
                },
                "ahj": {f: r for f, (_, r) in zip(fields, pairs, strict=True)},
            },
            in_scope=("ahj",),
        )
        for t, pairs in plan.items()
    ]
    sources = [source("primary", "primary"), source("ahj", "reference")]
    stats = {
        "match": {"key": 3, "anchor": 0},
        "ambiguous": {},
        "out_of_scope": {"ahj": 193},
        "clusters": 196,
    }
    return sources, clusters, make_columns(*fields), stats


def test_s1_against_reference(tmp_path):
    sources, clusters, columns, stats = s1_setup()
    summary, cells, out = run(
        tmp_path,
        clusters,
        sources,
        columns,
        align_stats=stats,
        covered=roster_covered(("t1",), ("t2",), ("t3",)),
    )
    assert len(cells) == 27
    statuses = [row["status"] for row in cells]
    assert statuses.count("all_na") == 2
    priorities = [row["review_priority"] for row in cells]
    assert (priorities.count("medium"), priorities.count("low"), priorities.count("none")) == (
        1,
        24,
        2,
    )
    assert [row["miss"] for row in cells].count("extraction") == 19
    assert {row["miss"] for row in cells if row["primary"] != "NA"} == {""}
    p = summary["models"]["primary"]["overall"]["ahj"]
    assert (p["filled"], p["tp"], p["fp"], p["fn"], p["both_na"]) == (6, 5, 1, 19, 2)
    assert (p["coverage"], p["accuracy"], p["accuracy_j"]) == (0.2222, 0.2593, 0.2593)
    assert p["ci95"]["accuracy"] is None  # pooled cells within targets are dependent
    assert (p["precision"], p["recall"], p["f1"]) == (0.8333, 0.2083, 0.3333)
    assert (p["correct"], p["cells"], p["missed_extraction"]) == (7, 27, 19)
    rows = list(csv.reader((out / "summary.csv").open(encoding="utf-8")))
    assert tuple(rows[0]) == SUMMARY_COLUMNS
    assert len(rows) == 1 + 10 and {r[4] for r in rows[1:]} == {"ahj"}
    assert summary["rows"]["out_of_scope"]["ahj"] == 193
    assert summary["diagnosis"]["no_docs"]["count"] == 0
    assert "gate" not in summary
    assert not any(line.startswith("Gate") for line in leaderboard(summary, "s1"))


def test_summary_csv_has_no_raw_counters(tmp_path):
    sources, clusters, columns, stats = s1_setup()
    run(tmp_path, clusters, sources, columns, align_stats=stats)
    header = (tmp_path / "out" / "summary.csv").read_text().splitlines()[0].split(",")
    assert not {"tp", "fp", "fn", "filled", "correct", "split"} & set(header)


def test_roster_miss_no_docs_and_diagnosis(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster((f"T{i}",), {"primary": {"x": None}, "ref": {"x": "1"}}, in_scope=("ref",))
        for i in range(12)
    ]
    summary, cells, _ = run(
        tmp_path, clusters, sources, make_columns("x"), covered=roster_covered(("T0",))
    )
    assert {row["ID"]: row["miss"] for row in cells}["T0"] == "extraction"
    assert [row["miss"] for row in cells].count("no_docs") == 11
    no_docs = summary["diagnosis"]["no_docs"]
    assert no_docs["count"] == 11 and len(no_docs["keys"]) == 10
    assert summary["diagnosis"]["columns"]["X"]["missed_no_docs"] == 11
    overall = summary["models"]["primary"]["overall"]["ref"]
    assert (overall["missed_no_docs"], overall["missed_extraction"]) == (11, 1)
    assert "discovery — 11 targets without documents" in leaderboard(summary, "d")[-1]


def test_diagnosis_splits_wrong_from_candidate_updates(tmp_path):
    sources = [source("primary", "primary"), source("ref", "reference")]
    clusters = [
        cluster(("t1",), {"primary": {"x": ("5", "says 5")}, "ref": {"x": "6"}}, in_scope=("ref",)),
        cluster(("t2",), {"primary": {"x": ("7", "says 7")}, "ref": {"x": "8"}}, in_scope=("ref",)),
        cluster(
            ("t3",), {"primary": {"x": ("9", "says 9")}, "ref": {"x": "9.0"}}, in_scope=("ref",)
        ),
    ]
    judge = FakeJudge(
        by_value(
            {
                "5": ("supported", 0),
                "6": ("unsupported", 1),
                "7": ("unsupported", 0),
                "8": ("supported", 1),
            }
        )
    )
    summary, cells, _ = run(
        tmp_path,
        clusters,
        sources,
        make_columns("x"),
        judge=judge,
        covered=roster_covered(("t1",), ("t2",), ("t3",)),
    )
    assert summary["diagnosis"]["columns"]["X"] == {
        "missed_extraction": 0,
        "missed_no_docs": 0,
        "wrong": 1,
        "candidate_updates": 1,
    }
    priority = {row["ID"]: row["review_priority"] for row in cells}
    assert priority == {"t1": "medium", "t2": "high", "t3": "low"}
    p = summary["models"]["primary"]["overall"]["ref"]
    assert (p["fp_j"], p["fn_j"]) == (1, 1)  # t1 clears FP (supported) and FN (ref unsupported)
    assert "1 candidate updates" in leaderboard(summary, "d")[-1]


# -- emission rules and verdict mapping ----------------------------------------


def test_emission_rules_and_class_mapping(tmp_path):
    sources = [source("primary", "primary"), source("a"), source("ref", "reference")]
    clusters = [
        cluster(("unanimous",), {"primary": {"x": ("1", "q")}, "a": {"x": ("1.0", "q")}}),
        cluster(("partial_no_quote",), {"primary": {"x": "2"}, "a": {"x": None}}),
        cluster(("partial_quoted",), {"primary": {"x": ("3", "q3")}, "a": {"x": None}}),
        cluster(
            ("disputed",),
            {"primary": {"x": ("300", "q")}, "a": {"x": ("300.0", None)}, "ref": {"x": "400"}},
            in_scope=("ref",),
        ),
        cluster(
            ("reference_only",),
            {"primary": {"x": None}, "a": {"x": None}, "ref": {"x": "5"}},
            in_scope=("ref",),
        ),
        cluster(("all_na",), {"primary": {"x": None}, "a": {"x": None}}),
    ]
    judge = FakeJudge(
        by_value({"300": ("supported", 0), "400": ("unsupported", 1), "3": ("supported", 0)})
    )
    _, cells, _ = run(tmp_path, clusters, sources, make_columns("x"), judge=judge)
    judged = sorted(tuple(c.value for c in i.candidates) for i in judge.items)
    assert judged == [("3",), ("300", "400")]
    disputed = next(i for i in judge.items if len(i.candidates) == 2)
    assert disputed.candidates[1].quote is None  # reference-only class carries no quote
    row = {r["ID"]: r for r in cells}["disputed"]
    assert row["primary_support"] == row["a_support"] == "supported"
    assert row["ref_support"] == "unsupported"
    assert row["judge_groups"] == "primary,a | ref"
    assert {r["ID"]: r["status"] for r in cells} == {
        "unanimous": "unanimous",
        "partial_no_quote": "partial",
        "partial_quoted": "partial",
        "disputed": "disputed",
        "reference_only": "reference_only",
        "all_na": "all_na",
    }
    assert class_key("300") == class_key("300.0")


def test_conflict_raises_low_and_no_source_rules(tmp_path):
    sources = [source("primary", "primary"), source("a")]
    clusters = [
        cluster(("c",), {"primary": {"x": Cell("1", None, None, True)}, "a": {"x": "1"}}),
        cluster(("n",), {"primary": {"x": Cell("1", None, None, False)}, "a": {"x": "1"}}),
    ]
    _, cells, _ = run(tmp_path, clusters, sources, make_columns("x"))
    assert {r["ID"]: (r["review_priority"], r["primary_conflict"]) for r in cells} == {
        "c": ("medium", "true"),
        "n": ("low", ""),
    }
    solo = [source("primary", "primary"), source("ref", "reference")]
    alone = [cluster(("v",), {"primary": {"x": "1"}}), cluster(("e",), {"primary": {"x": None}})]
    _, cells, _ = run(tmp_path, alone, solo, make_columns("x"), out="solo")
    assert {r["ID"]: r["review_priority"] for r in cells} == {"v": "low", "e": "none"}


def test_new_vs_contender_corroborated_is_low(tmp_path):
    sources = [source("primary", "primary"), source("a"), source("b")]
    clusters = [cluster(("t",), {"primary": {"x": "1"}, "a": {"x": None}, "b": {"x": "1"}})]
    _, cells, _ = run(tmp_path, clusters, sources, make_columns("x"))
    assert cells[0]["review_priority"] == "low"


# -- rows mode ----------------------------------------------------------------


def rows_setup(extra=()):
    keys = ("state", "county", "feature")
    columns = make_columns("value", "units", "summary", keys=keys, unscored={"h": ("summary",)})
    sources = [source("primary", "primary"), source("a"), source("h", "reference")]
    lake = ("CA", "Lake")

    def c(feature, rows, match="key", in_scope=("h",)):
        return cluster((*lake, feature), rows, match=match, in_scope=in_scope, scope=lake)

    clusters = [
        c(
            "setback",
            {
                "primary": {
                    "value": ("500", "q1"),
                    "units": ("feet", "q1"),
                    "summary": ("500 ft setback", "q1"),
                },
                "a": {
                    "value": ("500", "q1"),
                    "units": ("feet", "q1"),
                    "summary": ("Minimum 500-foot setback", "q1"),
                },
                "h": {"value": "500", "units": "feet"},
            },
        ),
        c(
            "lot",
            {
                "primary": {"value": "1,500", "units": "acres", "summary": None},
                "a": {"value": "1500", "units": "acres", "summary": None},
                "h": {"value": "1500", "units": "acres"},
            },
        ),
        c(
            "noise",
            {
                "primary": {"value": ("55", "q3"), "units": "dBA", "summary": None},
                "a": {"value": ("50", "q3"), "units": "dBA", "summary": None},
            },
            match="anchor",
        ),
        c(
            "hours-start",
            {
                "primary": {"value": "7", "units": "h", "summary": None},
                "a": {"value": "7", "units": "h", "summary": None},
            },
        ),
        c(
            "hours-end",
            {
                "primary": {"value": "19", "units": "h", "summary": None},
                "a": {"value": "19", "units": "h", "summary": None},
            },
        ),
        c("fencing", {"a": {"value": "1", "units": None, "summary": None}}),
        cluster(
            ("CA", "Napa", "x"),
            {"h": {"value": "1", "units": None}},
            match="out_of_scope",
            in_scope=("h",),
            scope=("CA", "Napa"),
        ),
        *extra,
    ]
    stats = {
        "match": {"key": 4, "anchor": 1},
        "ambiguous": {"primary": 0},
        "out_of_scope": {"h": 1},
        "clusters": len(clusters),
    }
    return sources, clusters, columns, stats


SAME_SUMMARY = by_value(
    {
        "500 ft setback": ("supported", 0),
        "Minimum 500-foot setback": ("supported", 0),
        "55": ("supported", 0),
        "50": ("unsupported", 1),
    }
)


def test_rows_mode_metrics_record_lines_and_gate(tmp_path):
    sources, clusters, columns, stats = rows_setup()
    summary, cells, _ = run(
        tmp_path,
        clusters,
        sources,
        columns,
        judge=FakeJudge(SAME_SUMMARY),
        covered=None,
        align_stats=stats,
        min_accuracy=0.5,
    )
    p_h = summary["models"]["primary"]["overall"]["h"]
    assert (p_h["row_tp"], p_h["row_fp"], p_h["row_fn"]) == (2, 3, 0)
    assert (p_h["accuracy"], p_h["cells"]) == (1.0, 4)
    assert summary["models"]["primary"]["columns"]["Summary"]["h"]["unscored"] == 2
    pa = summary["agreement"]["overall"]["primary|a"]
    assert (pa["matched"], pa["only_a"], pa["only_b"], pa["jaccard"]) == (5, 0, 1, 0.8333)
    records = [r for r in cells if r["column"] == "*"]
    assert [(r["FEATURE"], r["status"], r["review_priority"]) for r in records] == [
        ("fencing", "missing_row", "medium")
    ]
    assert json.loads(records[0]["a"]) == [{"Value": "1", "Units": "NA", "Summary": "NA"}]
    setback = {r["column"]: r for r in cells if r["FEATURE"] == "setback"}
    assert setback["Summary"]["review_priority"] == "low"
    assert setback["Summary"]["judge_groups"] == "primary,a"
    assert setback["Units"]["status"] == "unanimous"
    lot = {r["column"]: r for r in cells if r["FEATURE"] == "lot"}
    assert lot["Value"]["status"] == "unanimous"
    noise = {r["column"]: r for r in cells if r["FEATURE"] == "noise"}["Value"]
    assert (noise["status"], noise["review_priority"]) == ("disputed", "low")
    gate = summary["gate"]
    assert gate["passed"] is False
    assert gate["references"]["h"] == {"accuracy_j": 1.0, "out_of_scope": 1, "passed": False}
    assert summary["diagnosis"]["no_docs"] == {
        "kind": "scope",
        "count": 1,
        "keys": [["CA", "Napa"]],
    }
    assert "h 1.00 (1 rows out of scope)" in next(
        line for line in leaderboard(summary, "geo") if line.startswith("Gate: FAIL")
    )


def test_rows_reference_row_missing_from_contender_counts_wrong(tmp_path):
    lake = ("CA", "Lake")
    missing = cluster(
        (*lake, "height"),
        {"a": {"value": "9", "units": None, "summary": None}, "h": {"value": "9", "units": None}},
        in_scope=("h",),
        scope=lake,
    )
    sources, clusters, columns, stats = rows_setup(extra=(missing,))
    summary, cells, _ = run(tmp_path, clusters, sources, columns, covered=None, align_stats=stats)
    p_h = summary["models"]["primary"]["overall"]["h"]
    assert (p_h["cells"], p_h["correct"], p_h["missed_extraction"]) == (6, 4, 1)
    assert p_h["row_fn"] == 1 and summary["diagnosis"]["rows_missed"] == 1
    record = next(r for r in cells if r["FEATURE"] == "height")
    assert (record["status"], record["miss"], record["review_priority"]) == (
        "missing_row",
        "extraction",
        "medium",
    )


def _record(tmp_path, rows, contenders, *, in_scope=("h",), out="r"):
    sources = [
        source("primary", "primary"),
        *(source(c) for c in contenders),
        source("h", "reference"),
    ]
    lake = ("CA", "Lake")
    columns = make_columns("value", keys=("state", "county", "feature"))
    built = cluster(
        (*lake, "f"),
        rows,
        in_scope=in_scope,
        scope=lake,
        match="ambiguous" if any(isinstance(r, list) for r in rows.values()) else "key",
    )
    _, cells, _ = run(tmp_path, [built], sources, columns, covered=None, out=out)
    return [(r["status"], r["review_priority"]) for r in cells if r["column"] == "*"]


@pytest.mark.parametrize(
    ("rows", "contenders", "in_scope", "expected"),
    [
        (
            {"a": {"value": "1"}, "b": {"value": "1"}},
            ("a", "b", "c"),
            ("h",),
            [("missing_row", "high")],
        ),
        ({"a": {"value": "1"}}, ("a", "b", "c"), ("h",), [("missing_row", "medium")]),
        ({"h": {"value": "1"}}, ("a",), ("h",), [("missing_row", "low")]),
        ({"primary": {"value": "1"}}, ("a", "b"), (), [("extra_row", "high")]),
        ({"primary": {"value": "1"}}, ("a",), (), [("extra_row", "medium")]),
        ({"primary": {"value": "1"}}, (), ("h",), [("extra_row", "medium")]),
        ({"primary": {"value": "1"}}, (), (), []),
        ({"primary": {"value": "1"}, "a": {"value": "1"}}, ("a",), (), []),
        ({"a": [{"value": "1"}, {"value": "2"}]}, ("a",), (), [("ambiguous", "medium")]),
    ],
)
def test_record_line_rules(tmp_path, rows, contenders, in_scope, expected):
    assert _record(tmp_path, rows, contenders, in_scope=in_scope) == expected


# -- accuracy, gate, determinism -------------------------------------------------


def gate_setup(primary_values, ref_values):
    sources = [source("primary", "primary"), source("ex", "reference")]
    clusters = [
        cluster(
            (f"t{i}",),
            {"primary": {"x": (p, f"q{p}") if p else None}, "ex": {"x": r}},
            in_scope=("ex",),
        )
        for i, (p, r) in enumerate(zip(primary_values, ref_values, strict=True))
    ]
    return sources, clusters


@pytest.mark.parametrize(
    ("threshold", "out_of_scope", "passed"),
    [
        (0.75, 0, True),
        (0.8, 0, False),
        (1.0, 0, False),
        (0.5, 1, False),
    ],
)
def test_gate(tmp_path, threshold, out_of_scope, passed):
    sources, clusters = gate_setup(["1", "2", "3", None], ["1", "2", "3", None])
    clusters[2] = cluster(("t2",), {"primary": {"x": "3"}, "ex": {"x": "4"}}, in_scope=("ex",))
    stats = {
        "match": {"key": 4, "anchor": 0},
        "ambiguous": {},
        "out_of_scope": {"ex": out_of_scope},
        "clusters": 4,
    }
    summary, _, _ = run(
        tmp_path, clusters, sources, make_columns("x"), min_accuracy=threshold, align_stats=stats
    )
    assert summary["gate"]["references"]["ex"]["accuracy_j"] == 0.75
    assert summary["gate"]["passed"] is passed


def test_gate_fails_on_out_of_scope_at_full_accuracy_and_uses_judged(tmp_path):
    sources, clusters = gate_setup(["1", "2"], ["1", "2"])
    stats = {"match": {}, "ambiguous": {}, "out_of_scope": {"ex": 2}, "clusters": 4}
    summary, _, _ = run(
        tmp_path, clusters, sources, make_columns("x"), min_accuracy=1, align_stats=stats
    )
    assert summary["gate"]["references"]["ex"] == {
        "accuracy_j": 1.0,
        "out_of_scope": 2,
        "passed": False,
    }

    sources, clusters = gate_setup(["5 ft", "2"], ["five feet", "2"])
    judge = FakeJudge(by_value({"5 ft": ("supported", 0), "five feet": ("unclear", 0)}))
    summary, _, _ = run(
        tmp_path, clusters, sources, make_columns("x"), judge=judge, min_accuracy=1, out="judged"
    )
    overall = summary["models"]["primary"]["overall"]["ex"]
    assert (overall["accuracy"], overall["accuracy_j"]) == (0.5, 1.0)
    assert summary["gate"]["passed"] is True
    assert "Gate: PASS" in "\n".join(leaderboard(summary, "g"))


def test_accuracy_sample_interval(tmp_path):
    sources = [source("primary", "primary"), source("blind_3", "reference")]
    values = [("1", "1"), ("2", "2"), ("3", "3")]
    clusters = [
        cluster(
            (f"t{i}",),
            {"primary": dict.fromkeys("xyz", v[0]), "blind_3": dict.fromkeys("xyz", v[1])},
            in_scope=("blind_3",),
        )
        for i, v in enumerate(values)
    ]
    clusters[0].rows["blind_3"][0]["z"] = Cell("9", None, None, False)
    summary, _, _ = run(tmp_path, clusters, sources, make_columns("x", "y", "z"))
    overall = summary["models"]["primary"]["overall"]["blind_3"]
    assert overall["accuracy"] == 0.8889
    assert overall["ci95"]["accuracy"] is None
    field = summary["models"]["primary"]["columns"]["X"]["blind_3"]
    assert field["ci95"]["accuracy"] == wilson(3, 3)
    assert "0.89" in leaderboard(summary, "s")[2]
    assert "±" not in leaderboard(summary, "s")[2]


def test_outputs_deterministic_under_random_cluster_order(tmp_path):
    sources, clusters, columns, stats = rows_setup()
    outputs = []
    for n in range(3):
        shuffled = clusters[:]
        random.Random(n).shuffle(shuffled)  # noqa: S311 - deterministic test ordering
        run(
            tmp_path,
            shuffled,
            sources,
            columns,
            judge=FakeJudge(SAME_SUMMARY),
            covered=None,
            align_stats=stats,
            out=f"o{n}",
        )
        outputs.append(
            tuple((tmp_path / f"o{n}" / name).read_bytes() for name in ("cells.csv", "summary.csv"))
        )
    assert len(set(outputs)) == 1
    priorities = [
        r["review_priority"]
        for r in csv.DictReader((tmp_path / "o0" / "cells.csv").open(encoding="utf-8"))
    ]
    assert priorities == sorted(priorities, key=["high", "medium", "low", "none"].index)


def test_summary_json_written_and_csv_escapes_formulas(tmp_path):
    sources = [source("primary", "primary"), source("a")]
    clusters = [cluster(("=cmd",), {"primary": {"x": "=1+1"}, "a": {"x": "2"}})]
    summary, _, out = run(tmp_path, clusters, sources, make_columns("x"))
    assert json.loads((out / "summary.json").read_text()) == json.loads(json.dumps(summary))
    text = (out / "cells.csv").read_text()
    assert "'=cmd" in text and "'=1+1" in text
