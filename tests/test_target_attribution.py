"""Tests for target attribution: target_metadata flows selection → download record.

Each downloaded file must know which target row produced it. Metadata is
stamped on the candidate in CandidateSelector.select(), preserved through
LinkPrioritizer re-ranking, and surfaced in the download record / index CSV.
"""

from __future__ import annotations

from psweep.acquisition.candidate_selector import CandidateSelector
from psweep.acquisition.link_prioritizer import LinkPrioritizer
from psweep.acquisition.models import (
    AcquisitionCandidate,
    CandidateScore,
)


def _candidate(url: str) -> AcquisitionCandidate:
    return AcquisitionCandidate(
        url=url,
        source="test",
        score=CandidateScore(),
    )


class TestSelectorStampsMetadata:
    def test_selected_candidates_carry_target_metadata(self):
        sel = CandidateSelector()
        target = {"label": "Aurora CO", "state": "co", "utility_name": "Xcel"}
        candidates_by_target = [[_candidate("https://xcelenergy.com/a.pdf")]]

        selected, _notes = sel.select(
            candidates_by_target,
            primary_per_target=1,
            target_contexts=[target],
        )

        assert len(selected) == 1
        assert selected[0].target_metadata == target
        # A copy, not the same object, so later mutation can't leak back.
        assert selected[0].target_metadata is not target

    def test_metadata_is_per_target(self):
        sel = CandidateSelector()
        t0 = {"label": "Aurora CO"}
        t1 = {"label": "Shaker Heights OH"}
        candidates_by_target = [
            [_candidate("https://a.com/a.pdf")],
            [_candidate("https://b.com/b.pdf")],
        ]

        selected, _ = sel.select(
            candidates_by_target,
            primary_per_target=1,
            target_contexts=[t0, t1],
        )

        by_host = {c.url: c.target_metadata for c in selected}
        assert by_host["https://a.com/a.pdf"]["label"] == "Aurora CO"
        assert by_host["https://b.com/b.pdf"]["label"] == "Shaker Heights OH"

    def test_no_contexts_leaves_metadata_none(self):
        sel = CandidateSelector()
        selected, _ = sel.select(
            [[_candidate("https://a.com/a.pdf")]],
            primary_per_target=1,
            target_contexts=None,
        )
        assert selected[0].target_metadata is None


class TestPrioritizerPreservesMetadata:
    def test_metadata_survives_reranking(self):
        target = {"label": "Aurora CO", "state": "co"}
        c = _candidate("https://xcelenergy.com/rate.pdf")
        c.target_metadata = dict(target)

        prioritizer = LinkPrioritizer(keywords=["rate"])
        ranked, _lineage = prioritizer.prioritize([c])

        assert ranked, "prioritizer should return the candidate"
        assert ranked[0].target_metadata == target


class TestRoutingPreservesMetadata:
    """Target attribution must survive distributed/centralized routing —
    both when a seeker candidate is matched and when a crawled child is
    attributed back to its seed via source_seed."""

    def test_copy_candidate_preserves_metadata(self):
        from psweep.acquisition.engine import AcquisitionEngine

        c = _candidate("https://generac.com/manual.pdf")
        c.target_metadata = {"manufacturer": "Generac", "power_class_kw": "200"}
        copied = AcquisitionEngine._copy_candidate_with_reason(c, "routed")
        assert copied.target_metadata == {
            "manufacturer": "Generac",
            "power_class_kw": "200",
        }
        assert "routed" in copied.reasons

    def test_crawled_child_inherits_seed_metadata(self):
        from types import SimpleNamespace

        from psweep.acquisition.engine import AcquisitionEngine

        seed = _candidate("https://county.gov/ordinances/")
        seed.target_metadata = {"county_name": "Boulder", "state": "CO"}
        by_url = {seed.url: seed}
        # Artifact discovered by crawling the seed carries source_seed.
        artifact = SimpleNamespace(
            metadata={"source_seed": "https://county.gov/ordinances/"}
        )
        inherited = AcquisitionEngine._inherit_target_metadata(
            artifact, by_url
        )
        assert inherited == {"county_name": "Boulder", "state": "CO"}

    def test_inherit_returns_none_without_source_seed(self):
        from types import SimpleNamespace

        from psweep.acquisition.engine import AcquisitionEngine

        artifact = SimpleNamespace(metadata={"discovery_mode": "x"})
        assert AcquisitionEngine._inherit_target_metadata(artifact, {}) is None
