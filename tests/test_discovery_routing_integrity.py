"""Target identity survives routing and physical URL reuse."""

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.base import DiggerArtifact
from discovery_helpers import make_candidate


def _request(**kwargs):
    return DiscoveryRequest(
        domain="fixture",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=True,
        **kwargs,
    )


def test_post_route_host_limits_are_per_target():
    """Two targets sharing a host retain independent eligible candidates."""
    request = _request(
        selection_max_per_host_per_target=1,
        selection_target_identity_require_any_templates=["{label}"],
    )
    candidates = [
        make_candidate(
            f"https://example.org/{label}.pdf",
            target_metadata={"label": label},
        )
        for label in ("alpha", "beta")
    ]
    selected, _ = DiscoveryEngine._apply_post_routing_selection_filters(
        request=request,
        candidates=candidates,
    )
    assert len(selected) == 2


def test_post_route_identity_uses_originating_context():
    """A wrong-target crawl child does not bypass templated identity rules."""
    candidate = make_candidate(
        "https://example.org/unrelated.pdf",
        target_metadata={"label": "alpha"},
    )
    selected, _ = DiscoveryEngine._apply_post_routing_selection_filters(
        request=_request(
            selection_target_identity_require_any_templates=["{label}"]
        ),
        candidates=[candidate],
    )
    assert selected == []


def test_shared_url_target_associations_are_not_deduplicated():
    """Fetch identity and target provenance are separate concepts."""
    candidates = [
        make_candidate(
            "https://example.org/shared.pdf",
            target_metadata={"label": label},
        )
        for label in ("alpha", "beta")
    ]
    assert len(DiscoveryEngine._dedupe_candidates_by_url(candidates)) == 2
    artifacts = [DiggerArtifact(url=candidates[0].url, source="crawl")]
    routed, _ = DiscoveryEngine()._build_routed_candidates_from_artifacts(
        candidates=candidates,
        artifacts=artifacts,
        route_reason="routed",
    )
    assert {c.target_metadata["label"] for c in routed} == {"alpha", "beta"}


def test_child_inherits_all_seed_contexts_and_link_evidence():
    """One shared seed can lead to a child relevant to several targets."""
    candidates = [
        make_candidate(
            "https://example.org/hub",
            target_metadata={"label": label},
        )
        for label in ("alpha", "beta")
    ]
    artifact = DiggerArtifact(
        url="https://example.org/document.pdf",
        source="crawl",
        metadata={
            "source_seed": "https://example.org/hub",
            "link_text": "Relevant evidence",
        },
    )
    routed, _ = DiscoveryEngine()._build_routed_candidates_from_artifacts(
        candidates=candidates,
        artifacts=[artifact],
        route_reason="routed",
    )
    assert len(routed) == 2
    assert all(c.snippet == "Relevant evidence" for c in routed)


def test_default_partition_uses_explicit_context_not_query(tmp_path):
    """Search text does not become geographic provenance."""
    request = _request()
    request.query = "Fairfax County Virginia regulation"
    mode, _, path = DiscoveryEngine._resolve_partition_dir(
        documents_dir=tmp_path,
        url="https://example.org/document.pdf",
        request=request,
    )
    assert mode == "host"
    assert "by_host" in str(path)
