"""Regression tests for discovery engine crawl, retry budget, and checkpointing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import psweep.discovery.engine as engine_mod
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.base import CrawlResult, DiggerArtifact
from psweep.discovery.constants import ROUTE_REASON_CRAWLED
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
        return CrawlResult(self._artifacts)


def _base_request(**overrides) -> DiscoveryRequest:
    params = dict(
        domain="d",
        queries=["q"],
        dry_run=False,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    params.update(overrides)
    return DiscoveryRequest(**params)


def _candidate(url: str, **overrides) -> DiscoveryCandidate:
    params = dict(
        url=url,
        source="configured",
        score=CandidateScore(url_signal=0.9, trust_signal=0.9),
    )
    params.update(overrides)
    return DiscoveryCandidate(**params)


# ---------------------------------------------------------------------------
# 1. Crawl
# ---------------------------------------------------------------------------


def test_crawl_disabled_never_calls_connector(monkeypatch):
    fake = _FakeConnector([])
    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: fake)
    candidates = [_candidate("https://county.gov/hub")]
    routed, errors, _, state, _ = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=0), candidates=candidates
    )
    assert (routed, errors, fake.calls, state["applied"]) == (candidates, [], [], False)


def test_crawl_children_inherit_origin_attribution(monkeypatch):
    """Linked documents keep their source page's target, scope, and URL."""
    hub = "https://county.gov/hub"
    pdf = "https://county.gov/ord.pdf"
    child = "https://county.gov/codes/a.pdf"
    fake = _FakeConnector(
        [DiggerArtifact(url=child, source="http_digger", metadata={"source_url": hub})]
    )
    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: fake)
    candidates = [
        _candidate(hub, target_metadata={"label": "cty"}, target_id="t1",
                   source_scope="target", source_url=hub),
        _candidate(pdf, target_metadata={"label": "cty"}, target_id="t1",
                   source_scope="target", source_url=pdf),
    ]
    routed, errors, _, state, _ = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=1), candidates=candidates
    )
    assert errors == [] and state["applied"] is True
    assert [c.url for c in routed] == [hub, pdf, child]
    assert fake.calls[0].sources == [hub]
    crawled = routed[-1]
    assert crawled.reasons == [ROUTE_REASON_CRAWLED]
    assert (crawled.target_id, crawled.source_scope, crawled.source_url) == ("t1", "target", hub)
    assert crawled.target_metadata == {"label": "cty"}


def test_configured_page_query_variants_stay_configured(monkeypatch):
    """Pagination of a configured listing is that listing; its links are not."""
    hub = "https://county.gov/list"
    page = f"{hub}?page=2#top"
    other = "https://county.gov/list/detail?page=2"
    found = "https://search.example/list"
    fake = _FakeConnector([
        DiggerArtifact(url=url, source="http_digger", metadata={"source_url": src})
        for url, src in ((page, hub), (other, hub), (f"{found}?page=2", found))
    ])
    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: fake)
    candidates = [
        _candidate(hub, source_scope="shared", source_url=hub),
        _candidate(found, source="serpapi", target_id="t1", source_url=found),
    ]
    routed, *_ = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=2), candidates=candidates
    )
    sources = {c.url: c.source for c in routed[2:]}
    assert sources == {
        page: "configured", other: "http_digger", f"{found}?page=2": "http_digger"
    }
    assert {c.source_url for c in routed[2:4]} == {hub}


@pytest.mark.parametrize(
    ("fetched", "cause"),
    [
        (3, "budget reached (crawl.max_pages=3, runtime.max_files=1)"),
        (1, "budget reached (runtime.max_files=1)"),
    ],
)
def test_crawl_names_the_budget_that_cut_it_off(monkeypatch, fetched, cause):
    hub = "https://county.gov/list"
    artifact = DiggerArtifact(url=f"{hub}/a.pdf", source="http_digger",
                              metadata={"source_url": hub})

    class _Cut:
        def discover(self, _di):  # noqa: ANN001
            return CrawlResult([artifact], {hub}, pages_fetched=fetched)

    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: _Cut())
    _, _, notes, _, unfinished = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=2, max_pages=3, max_files=1),
        candidates=[_candidate(hub)],
    )
    assert f"Crawl fetched {fetched} page(s) from 1 source page(s)" in notes[0]
    assert notes[1] == f"Crawl unfinished for 1 source page(s): {cause}; they stay incomplete."
    assert [c.url for c in unfinished] == [hub]


def test_crawl_without_exhausted_budget_reports_a_retryable_failure(monkeypatch):
    hub = "https://county.gov/list"

    class _Failed:
        def discover(self, _di):  # noqa: ANN001
            return CrawlResult([], {hub}, pages_fetched=0)

    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: _Failed())
    _, _, notes, _, _ = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=2), candidates=[_candidate(hub)]
    )
    assert "retryable page failure" in notes[-1]


def test_crawl_connector_failure_keeps_candidates(monkeypatch):
    class _Boom:
        def discover(self, _di):  # noqa: ANN001
            raise RuntimeError("crawl exploded")

    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: _Boom())
    candidates = [_candidate("https://county.gov/hub")]
    routed, errors, notes, state, unfinished = DiscoveryEngine()._crawl_candidates(
        request=_base_request(max_depth=1), candidates=candidates
    )
    assert routed == candidates
    assert state["applied"] is False
    assert len(errors) == 1 and unfinished == []
    assert any("Crawl failed" in n for n in notes)


# ---------------------------------------------------------------------------
# 2. Post-review retry cap
# ---------------------------------------------------------------------------


def _uncurated_record(label: str, url: str) -> dict[str, object]:
    return {
        "url": url,
        "status": "downloaded",
        "review_selected": False,
        "target_id": label,
        "target_metadata": {"label": label},
    }


def _run_code_hosting_retry(cap):
    """Run the code-hosting retry with a fake seeker; return #targets queried."""
    engine = DiscoveryEngine()

    class _FakeSeeker:
        instances: list["_FakeSeeker"] = []

        def __init__(self, **_kwargs):
            self.queries: list[str] = []
            self.queries_run = 0
            _FakeSeeker.instances.append(self)

        def discover(self, seeker_input):  # noqa: ANN001
            self.queries_run += 1
            self.queries.extend(seeker_input.queries)
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


def test_checkpoint_persisted_once_after_curation(tmp_path, monkeypatch):
    """Completed target entries share one atomic snapshot after publication."""
    engine = DiscoveryEngine()

    monkeypatch.chdir(tmp_path)
    dom = tmp_path / "outputs" / "d" / "discovered"
    labels = ("alpha", "beta", "gamma")

    def _download(*, request, candidates, documents_dir, max_downloads, reusable):
        records = []
        for label in labels:
            path = documents_dir / f"{label}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic document")
            records.append({
                "url": f"https://x/{label}.pdf",
                "status": "downloaded",
                "target_id": label,
                "target_metadata": {"label": label},
                "path": str(path),
                "relative_path": path.name,
            })
        return records, [], []

    monkeypatch.setattr(
        engine,
        "_filter_candidates_for_download",
        lambda candidates: (candidates, []),
    )
    monkeypatch.setattr(engine, "_download_candidates", _download)

    # Spy on checkpoint saves: avoid repeated whole-file rewrites per target.
    save_calls: list[int] = []
    orig_save = DiscoveryEngine._save_checkpoint_entries

    def _spy_save(path, new_entries, **kwargs):
        save_calls.append(len(new_entries))
        return orig_save(path, new_entries, **kwargs)

    monkeypatch.setattr(DiscoveryEngine, "_save_checkpoint_entries", staticmethod(_spy_save))

    request = _base_request(sources=["https://x/a.pdf"], queries=[])
    engine.run(request)

    assert save_calls == [3]

    # Final on-disk state: one entry per completed target.
    checkpoint_path = dom / "checkpoint.json"
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert set(data["entries"].keys()) == {"alpha", "beta", "gamma"}
    for entry in data["entries"].values():
        assert entry["download_count"] == 1
