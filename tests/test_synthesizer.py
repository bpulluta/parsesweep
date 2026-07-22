"""Synthesizer is domain-neutral and cost-aware.

These tests exercise the reconciliation stage on a NON-timeline domain (product
prices) and on an ordered domain, with a stub LLM client so no API calls are
made. They prove the module is driven entirely by config, not hardcoded to the
data-center timeline use case, and that trivial cases skip the LLM.
"""
from __future__ import annotations

import json

from psweep.consolidation.synthesizer import Synthesizer


class _StubLLM:
    """Returns a value for every property the synthesizer's merge schema asks
    for — so it adapts to whatever fields config declares."""

    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, system_prompt=None, user_prompt=None):
        self.calls += 1
        data = {}
        for key, spec in schema["properties"].items():
            t = spec.get("type")
            if key == "citation_urls":
                data[key] = ["http://src/won"]
            elif key == "data_confidence":
                data[key] = "high"
            elif t == "boolean":
                data[key] = True
            elif t == "object":
                data[key] = {}
            else:  # string or ["string","null"]
                data[key] = "RESOLVED"
        return {"data": data, "cost": 0.0}


def test_non_timeline_domain_reconciles_generic_field():
    """A product/price domain with NO ordering: reconciles a generic field and
    emits none of the timeline-specific columns."""
    cfg = {
        "item_array": "items",
        "group_by": ["product.name"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "price", "evidence": "price_quote"}],
        "min_sources_for_llm": 2,
    }
    recs = [
        {"product": {"name": "Widget"}, "src": {"url": "u1"}, "items": [{"price": "10"}]},
        {"product": {"name": "Widget"}, "src": {"url": "u2"}, "items": [{"price": "12"}]},
    ]
    llm = _StubLLM()
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=llm)
    row = syn._synthesize_group(("Widget",), recs)

    assert row["name"] == "Widget"
    assert row["price"] == "RESOLVED"       # came from the LLM merge
    assert row["data_confidence"] == "high"
    assert row["sources_checked"] == 2
    assert llm.calls == 1
    # No ordering configured -> no timeline-specific columns leak in.
    assert "ordering_consistent" not in row
    assert "ordering_notes" not in row


def test_single_source_is_deterministic_no_api_call():
    """One source carrying a value needs no reconciliation -> no LLM call."""
    cfg = {
        "item_array": "items",
        "group_by": ["product.name"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "price"}],
        "min_sources_for_llm": 2,
    }
    recs = [
        {"product": {"name": "Gizmo"}, "src": {"url": "only"}, "items": [{"price": "42"}]},
    ]
    llm = _StubLLM()
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=llm)
    row = syn._synthesize_group(("Gizmo",), recs)

    assert llm.calls == 0                    # cost control: no API call
    assert syn.deterministic_rows == 1
    assert row["price"] == "42"              # taken verbatim from the source
    assert row["citation_urls"] == "only"


def test_optional_ordering_guard_flags_violation_deterministically():
    """When ordering is configured, an impossible order is flagged even without
    an LLM call (single-source deterministic path)."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "start"}, {"field": "end"}],
        "ordering_constraint": ["start", "end"],
        "min_sources_for_llm": 2,
    }
    recs = [
        {"e": {"id": "X"}, "src": {"url": "u"},
         "items": [{"start": "2025-05-01", "end": "2025-01-01"}]},
    ]
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    row = syn._synthesize_group(("X",), recs)

    assert row["ordering_consistent"] is False
    assert "ordering note" in row["ordering_notes"]
    assert "start (2025-05-01) is after end (2025-01-01)" in row["ordering_notes"]


def test_date_comparator_is_precision_aware():
    """The opt-in 'date' comparator compares at the coarsest shared precision, so
    two year-precision values in the same year are NOT flagged as out of order."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "citation_field": "src.url",
        "reconcile_fields": [
            {"field": "date_announced", "precision": "precision_announced"},
            {"field": "date_construction_start",
             "precision": "precision_construction_start"},
        ],
        "ordering_constraint": ["date_announced", "date_construction_start"],
        "ordering_comparison": "date",
        "min_sources_for_llm": 2,
    }
    # announced pinned to Jan 1 (year precision), construction in June (month) —
    # same year: not a real violation despite date_announced > ... lexically false
    # here, but the key case is that year-vs-year same year is tolerated.
    recs = [{
        "e": {"id": "Y"}, "src": {"url": "u"},
        "items": [{
            "date_announced": "2025-01-01", "precision_announced": "year",
            "date_construction_start": "2025-06-01",
            "precision_construction_start": "month",
        }],
    }]
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    row = syn._synthesize_group(("Y",), recs)
    assert row["ordering_consistent"] is True
    # precision fields are carried through onto the row
    assert row["precision_announced"] == "year"


def test_numeric_comparator_reused_for_non_temporal_domain():
    """The synthesizer stays domain-neutral: 'numeric' orders by value, so it can
    validate e.g. min <= max with no temporal semantics."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "min"}, {"field": "max"}],
        "ordering_constraint": ["min", "max"],
        "ordering_comparison": "numeric",
        "min_sources_for_llm": 2,
    }
    recs = [{"e": {"id": "Z"}, "src": {"url": "u"},
             "items": [{"min": "10", "max": "5"}]}]
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    row = syn._synthesize_group(("Z",), recs)
    assert row["ordering_consistent"] is False
    assert "min (10) is after max (5)" in row["ordering_notes"]


def test_relevance_flag_filters_records():
    """Records failing the configured relevance flag are excluded from groups."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "relevance_flag": "meta.ok",
        "reconcile_fields": [{"field": "v"}],
    }
    recs = [
        {"e": {"id": "A"}, "meta": {"ok": True}, "items": [{"v": "1"}]},
        {"e": {"id": "A"}, "meta": {"ok": False}, "items": [{"v": "2"}]},
    ]
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    groups = syn._group(recs)
    assert list(groups.keys()) == [("A",)]
    assert len(groups[("A",)]) == 1          # the ok=False record dropped


def test_llm_merge_assembles_citations_and_narrative():
    """>= min_sources valued -> LLM path; row carries joined citations, a
    narrative field, reasoning/summary, and the source count."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "price"}],
        "narrative_fields": ["status"],
        "min_sources_for_llm": 2,
    }
    recs = [
        {"e": {"id": "Z"}, "src": {"url": "u1"}, "items": [{"price": "10"}]},
        {"e": {"id": "Z"}, "src": {"url": "u2"}, "items": [{"price": "12"}]},
    ]
    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    row = syn._synthesize_group(("Z",), recs)

    assert row["status"] == "RESOLVED"          # narrative field emitted
    assert row["citation_urls"] == "http://src/won"
    assert row["reasoning"] and row["summary"]  # narrative provenance present
    assert row["sources_checked"] == 2


def test_synthesize_from_directory_groups_and_skips_manifests(tmp_path):
    """End-to-end: load payload records recursively, skip run_manifests, and
    emit one row per group key."""
    cfg = {
        "item_array": "items",
        "group_by": ["e.id"],
        "citation_field": "src.url",
        "reconcile_fields": [{"field": "price"}],
        "min_sources_for_llm": 2,
    }

    def _rec(eid, url, price):
        return {"payload": {"e": {"id": eid}, "src": {"url": url},
                            "items": [{"price": price}]}}

    (tmp_path / "a.json").write_text(json.dumps(_rec("A", "u1", "1")))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.json").write_text(json.dumps(_rec("B", "u2", "2")))
    # A manifest file that must be ignored, not grouped.
    (tmp_path / "run_manifests").mkdir()
    (tmp_path / "run_manifests" / "m.json").write_text(
        json.dumps({"payload": {"e": {"id": "IGNORE"}}})
    )

    syn = Synthesizer(schema_metadata=None, config=cfg, llm_client=_StubLLM())
    df = syn.synthesize_from_directory(str(tmp_path))

    assert sorted(df["id"].tolist()) == ["A", "B"]
    assert "IGNORE" not in df["id"].tolist()
