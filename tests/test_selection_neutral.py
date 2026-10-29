"""Regression tests for config-only semantic candidate exclusions."""

from __future__ import annotations

import pytest

from psweep.discovery.candidate_selector import CandidateSelector

from discovery_helpers import make_candidate


@pytest.mark.parametrize("patterns", [None, []])
@pytest.mark.parametrize(
    "signal",
    [
        "draft",
        "proposed",
        "preliminary",
        "for-review",
        "model-ordinance",
        "model-code",
        "template",
        "superseded",
        "withdrawn",
        "obsolete",
        "archive",
        "archived",
    ],
)
def test_default_and_empty_patterns_do_not_exclude_semantic_terms(
    patterns, signal
):
    """No draft vocabulary is imposed when the domain supplies no patterns."""
    candidate = make_candidate(f"https://example.com/{signal}_2026.pdf")
    selector = CandidateSelector(exclude_draft=True, draft_patterns=patterns)
    selected, notes, metrics = selector.select(
        [[candidate]], include_metrics=True
    )
    assert selected == [candidate]
    assert not any("draft filter excluded" in note for note in notes)
    assert metrics[0]["outcome"] == "selected"


def test_proposed_project_title_remains_eligible_by_default():
    """Project datasets can intentionally include proposed activity."""
    candidate = make_candidate(
        "https://example.com/project-timeline_2026.pdf",
        title="Proposed datacenter project development timeline",
        snippet="Preliminary construction schedule and projected milestones",
    )
    selected, _ = CandidateSelector().select([[candidate]])
    assert selected == [candidate]


@pytest.mark.parametrize("field", ["url", "title", "snippet"])
def test_explicit_draft_pattern_filters_source_evidence(field):
    """Domain-provided patterns apply to URL, title and snippet evidence."""
    fields = {
        "url": "https://example.com/a.pdf",
        "title": None,
        "snippet": None,
    }
    fields[field] = (
        "https://example.com/draft_copy.pdf"
        if field == "url"
        else "Draft copy"
    )
    candidate = make_candidate(**fields)
    selector = CandidateSelector(draft_patterns=[r"\bdraft\b"])
    selected, _, metrics = selector.select([[candidate]], include_metrics=True)
    assert selected == []
    assert metrics[0]["outcome"] == "excluded_draft_or_stale"


def test_explicit_draft_pattern_does_not_filter_reason_only_query():
    """Rendered query metadata is not source evidence for exclusion."""
    candidate = make_candidate(
        "https://example.com/a.pdf", reasons=["query: proposed draft copy"]
    )
    selected, _ = CandidateSelector(draft_patterns=[r"\bdraft\b"]).select(
        [[candidate]]
    )
    assert selected == [candidate]


def test_only_supplied_draft_patterns_apply():
    """A narrow exclusion list never inherits unspecified legal vocabulary."""
    candidate = make_candidate(
        "https://example.com/proposed_model-ordinance.pdf",
        title="Proposed template",
    )
    selected, _ = CandidateSelector(draft_patterns=[r"\bdraft\b"]).select(
        [[candidate]]
    )
    assert selected == [candidate]


def test_exclude_draft_false_disables_explicit_patterns():
    """The existing enable switch still disables a domain-provided policy."""
    candidate = make_candidate("https://example.com/draft_copy.pdf")
    selected, _ = CandidateSelector(
        exclude_draft=False, draft_patterns=[r"\bdraft\b"]
    ).select([[candidate]])
    assert selected == [candidate]


def test_marker_gate_has_no_builtin_legal_vocabulary():
    """The existing optional marker gate works with non-legal domain terms."""
    sensor = make_candidate("https://example.com/sensor.pdf")
    ordinance = make_candidate("https://example.com/ordinance.pdf")
    selected, _ = CandidateSelector(
        relevance_require_legal_marker_terms=["sensor"]
    ).select([[ordinance, sensor]], primary_per_target=2)
    assert selected == [sensor]
