"""Regression tests for the audit/pipeline-cleanup refactors of the discovery
engine:

1. Routing dedup — ``_route_distributed_candidates`` /
   ``_route_centralized_candidates`` were consolidated onto shared helpers.
   These tests lock the routing decisions, route-reason strings, per-mode
   routing_state, and the distributed-only non-crawled merge-back.
2. Post-review retry cap — a single opt-in ``_RetryBudget`` bounds the total
   number of retried targets across both retry stages. Unset == unbounded.
3. Incremental checkpointing — checkpoint entries are persisted per target
   (crash-safety) while the final on-disk state is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import psweep.discovery.engine as engine_mod
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.base import DiggerArtifact
from psweep.discovery.constants import (
    ROUTE_REASON_CENTRALIZED,
    ROUTE_REASON_DISTRIBUTED,
)
from psweep.discovery.models import CandidateScore, DiscoveryCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConnector:
    """Digger connector stub returning a fixed artifact list."""

    def __init__(self, artifacts: list[DiggerArtifact]) -> None:
        self._artifacts = artifacts
        self.calls: list[object] = []

    def discover(self, digger_input):  # noqa: ANN001 - test stub
        self.calls.append(digger_input)
        return self._artifacts


def _base_request(**overrides) -> DiscoveryRequest:
    params = dict(
        domain="d",
        seed_urls=[],
        query="q",
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=False,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    params.update(overrides)
    return DiscoveryRequest(**params)


def _candidate(url: str, **overrides) -> DiscoveryCandidate:
    params = dict(
        url=url,
        source="seed",
        score=CandidateScore(url_signal=0.9, trust_signal=0.9),
    )
    params.update(overrides)
    return DiscoveryCandidate(**params)


# ---------------------------------------------------------------------------
# 1. Routing dedup
# ---------------------------------------------------------------------------


def test_distributed_routing_locks_decisions(monkeypatch):
    """Distributed routing: existing candidates re-tagged, new artifacts staged
    at the fixed 0.35/0.55 score, and non-crawled candidates merged back."""
    engine = DiscoveryEngine()

    seed_url = "https://municode.com/tx/keller/page"
    pdf_url = "https://municode.com/tx/keller/ord.pdf"  # non-crawlable, merged back
    child_url = "https://municode.com/tx/keller/section-3"

    candidates = [
        _candidate(seed_url, target_metadata={"label": "keller"}),
        _candidate(pdf_url, target_metadata={"label": "keller"}),
    ]

    artifacts = [
        # Matches an existing candidate -> re-tag with the distributed reason.
        DiggerArtifact(url=seed_url, source="digger", metadata={"discovery_mode": "crawl"}),
        # New crawl child, attributed back to its seed target metadata.
        DiggerArtifact(
            url=child_url,
            source="digger",
            mime_type="text/html",
            extension="html",
            metadata={"discovery_mode": "crawl", "source_seed": seed_url},
        ),
    ]
    fake = _FakeConnector(artifacts)
    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: fake)

    request = _base_request(
        topology_mode="distributed",
        digger_provider="http",
        link_prioritization_domain_scores={"municode.com": 1.0},
    )

    routed, errors, notes, state = engine._route_distributed_candidates(
        request=request, candidates=candidates
    )

    assert errors == []
    assert state["mode"] == "distributed"
    assert state["applied"] is True
    assert state["artifact_count"] == 2
    assert state["discovery_modes"] == ["crawl"]
    # No centralized-only keys leak into distributed state.
    assert "hub_page_count" not in state

    routed_by_url = {c.url: c for c in routed}
    # Existing candidate re-tagged (not duplicated as a new artifact record).
    assert ROUTE_REASON_DISTRIBUTED in routed_by_url[seed_url].reasons
    # New crawl child gets the fixed routing score + inherited metadata.
    child = routed_by_url[child_url]
    assert child.reasons == [ROUTE_REASON_DISTRIBUTED]
    assert child.score.url_signal == 0.35
    assert child.score.trust_signal == 0.55
    assert child.target_metadata == {"label": "keller"}
    # Non-crawlable PDF candidate merged back in unchanged.
    assert pdf_url in routed_by_url
    assert ROUTE_REASON_DISTRIBUTED not in routed_by_url[pdf_url].reasons
    assert any("Distributed routing staged" in n for n in notes)


def test_centralized_routing_locks_decisions(monkeypatch):
    """Centralized routing: hub_page_count / index_link_count present, the
    centralized reason applied, and no non-artifact merge-back."""
    engine = DiscoveryEngine()

    hub = "https://docs.county.gov/index.html"
    child_url = "https://docs.county.gov/ord-1.pdf"
    extra_candidate = "https://docs.county.gov/never-swept.html"

    candidates = [_candidate(extra_candidate, target_metadata={"label": "cty"})]

    artifacts = [
        DiggerArtifact(
            url=child_url,
            source="sweep",
            mime_type="application/pdf",
            extension="pdf",
            metadata={"discovery_mode": "index"},
        ),
    ]
    fake = _FakeConnector(artifacts)
    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: fake)

    request = _base_request(
        topology_mode="centralized",
        digger_provider="http",
        hub_pages=[hub],
        index_links=[{"url": "x"}],
    )

    routed, errors, notes, state = engine._route_centralized_candidates(
        request=request, candidates=candidates
    )

    assert errors == []
    assert state["mode"] == "centralized"
    assert state["applied"] is True
    assert state["artifact_count"] == 1
    assert state["discovery_modes"] == ["index"]
    assert state["hub_page_count"] == 1
    assert state["index_link_count"] == 1

    routed_urls = {c.url for c in routed}
    # Only artifact-derived candidates; the non-swept candidate is NOT merged back.
    assert child_url in routed_urls
    assert extra_candidate not in routed_urls
    child = next(c for c in routed if c.url == child_url)
    assert child.reasons == [ROUTE_REASON_CENTRALIZED]
    assert child.score.url_signal == 0.35
    assert child.score.trust_signal == 0.55
    assert any("through hub sweep" in n for n in notes)


def test_distributed_routing_connector_failure_falls_back(monkeypatch):
    """A connector exception falls back to the unrouted candidates and records
    a routing error (behavior preserved through the shared discover helper)."""
    engine = DiscoveryEngine()

    class _Boom:
        def discover(self, _di):  # noqa: ANN001
            raise RuntimeError("digger exploded")

    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: _Boom())

    seed_url = "https://municode.com/tx/keller/page"
    candidates = [_candidate(seed_url, target_metadata={"label": "keller"})]
    request = _base_request(
        topology_mode="distributed",
        digger_provider="http",
        link_prioritization_domain_scores={"municode.com": 1.0},
    )

    routed, errors, notes, state = engine._route_distributed_candidates(
        request=request, candidates=candidates
    )

    assert [c.url for c in routed] == [seed_url]  # unrouted fallback
    assert state["applied"] is False
    assert len(errors) == 1
    assert any("Distributed routing failed" in n for n in notes)


# ---------------------------------------------------------------------------
# 2. Post-review retry cap
# ---------------------------------------------------------------------------


def _uncurated_record(label: str, url: str) -> dict[str, object]:
    return {
        "url": url,
        "status": "downloaded",
        "review_selected": False,
        "target_metadata": {"label": label},
    }


def _run_code_hosting_retry(cap):
    """Run the code-hosting retry with a fake seeker; return #targets queried."""
    engine = DiscoveryEngine()

    class _FakeSeeker:
        instances: list["_FakeSeeker"] = []

        def __init__(self, **_kwargs):
            self.queries: list[str] = []
            _FakeSeeker.instances.append(self)

        def discover(self, seeker_input):  # noqa: ANN001
            self.queries.append(seeker_input.query)
            return []  # no results -> no downloads, isolates the target-count cap

    import psweep.discovery.connectors.serpapi_seeker as ss_mod

    _FakeSeeker.instances = []
    orig = ss_mod.SerpApiSeeker
    ss_mod.SerpApiSeeker = _FakeSeeker
    try:
        records = [
            _uncurated_record("a", "https://municode.com/a/x"),
            _uncurated_record("b", "https://municode.com/b/x"),
            _uncurated_record("c", "https://municode.com/c/x"),
        ]
        request = _base_request(
            link_prioritization_domain_scores={"municode.com": 1.0},
        )
        budget = engine_mod._RetryBudget(cap)
        engine._retry_failed_targets_on_code_hosting(
            request=request,
            download_records=records,
            notes=[],
            documents_dir=Path("/tmp"),
            review_cfg={},
            classifier_keywords=["oil"],
            budget=budget,
        )
    finally:
        ss_mod.SerpApiSeeker = orig
    # One seeker instance per run; count how many targets it queried.
    return sum(len(s.queries) for s in _FakeSeeker.instances)


def test_retry_cap_bounds_targets_when_set():
    assert _run_code_hosting_retry(cap=1) == 1
    assert _run_code_hosting_retry(cap=2) == 2


def test_retry_cap_unset_is_unbounded():
    # None budget limit -> every failed target is retried (current behavior).
    assert _run_code_hosting_retry(cap=None) == 3


def test_retry_budget_shared_across_both_stages(monkeypatch):
    """The orchestrator threads ONE budget through both retry stages, sized from
    request.retry_max_retry_targets."""
    engine = DiscoveryEngine()
    seen: dict[str, object] = {}

    def _fake_code(*, budget, download_records, notes, **_kw):
        seen["code"] = budget
        return download_records, notes

    def _fake_js(*, budget, download_records, notes, **_kw):
        seen["js"] = budget
        return download_records, notes

    monkeypatch.setattr(
        engine, "_retry_failed_targets_on_code_hosting", _fake_code
    )
    monkeypatch.setattr(engine, "_retry_js_shells_with_digger", _fake_js)

    request = _base_request(
        link_prioritization_domain_scores={"municode.com": 1.0},
        retry_max_retry_targets=1,
    )
    engine._run_post_review_retries(
        request=request,
        download_records=[],
        notes=[],
        documents_dir=Path("/tmp"),
        review_cfg={},
        classifier_keywords=["oil"],
    )

    assert seen["code"] is seen["js"]  # same budget instance
    assert isinstance(seen["code"], engine_mod._RetryBudget)
    assert seen["code"].unbounded is False


def test_retry_budget_unbounded_when_unset(monkeypatch):
    engine = DiscoveryEngine()
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        engine,
        "_retry_failed_targets_on_code_hosting",
        lambda *, budget, download_records, notes, **_kw: (
            seen.__setitem__("code", budget),
            (download_records, notes),
        )[1],
    )
    monkeypatch.setattr(
        engine,
        "_retry_js_shells_with_digger",
        lambda *, budget, download_records, notes, **_kw: (
            seen.__setitem__("js", budget),
            (download_records, notes),
        )[1],
    )

    request = _base_request(
        link_prioritization_domain_scores={"municode.com": 1.0},
    )
    engine._run_post_review_retries(
        request=request,
        download_records=[],
        notes=[],
        documents_dir=Path("/tmp"),
        review_cfg={},
        classifier_keywords=["oil"],
    )
    assert seen["code"] is seen["js"]
    assert seen["code"].unbounded is True


# ---------------------------------------------------------------------------
# 3. Incremental checkpointing
# ---------------------------------------------------------------------------


def test_checkpoint_persisted_per_target(tmp_path, monkeypatch):
    """Checkpoint is saved once per target (incremental crash-safety) and the
    final on-disk state has one entry per completed target."""
    engine = DiscoveryEngine()

    dom = tmp_path / "dom"
    manifest_path = dom / "runs" / "run1" / "manifest.json"
    documents_dir = dom / "runs" / "run1" / "documents"

    canned = [
        {
            "url": "https://x/a.pdf",
            "status": "downloaded",
            "target_metadata": {"label": "alpha"},
        },
        {
            "url": "https://x/b.pdf",
            "status": "downloaded",
            "target_metadata": {"label": "beta"},
        },
        {
            "url": "https://x/c.pdf",
            "status": "downloaded",
            "target_metadata": {"label": "gamma"},
        },
    ]

    monkeypatch.setattr(
        engine,
        "_filter_candidates_for_download",
        lambda candidates: (candidates, []),
    )
    monkeypatch.setattr(
        engine,
        "_download_candidates",
        lambda *, request, candidates, documents_dir, max_downloads: (
            list(canned),
            [],
            [],
        ),
    )

    # Spy on checkpoint saves: count calls + assert each writes exactly 1 entry.
    save_calls: list[int] = []
    orig_save = DiscoveryEngine._save_checkpoint_entries

    def _spy_save(path, new_entries):
        save_calls.append(len(new_entries))
        return orig_save(path, new_entries)

    monkeypatch.setattr(DiscoveryEngine, "_save_checkpoint_entries", staticmethod(_spy_save))

    request = _base_request(
        seed_urls=["https://x/a.pdf"],
        output_documents=documents_dir,
        output_manifest=manifest_path,
    )
    engine.run(request)

    # One save per target -> incremental, not a single bulk save.
    assert len(save_calls) == 3
    assert all(size == 1 for size in save_calls)

    # Final on-disk state: one entry per completed target.
    checkpoint_path = dom / "checkpoint.json"
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert set(data["entries"].keys()) == {"alpha", "beta", "gamma"}
    for entry in data["entries"].values():
        assert entry["download_count"] == 1
