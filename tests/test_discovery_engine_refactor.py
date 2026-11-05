"""Regression tests for discovery engine crawl, code-hosting retry, and checkpointing."""

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


def _run_code_hosting_retry():
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
            return []  # no results -> no downloads, isolates the target count

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
        engine._retry_failed_targets_on_code_hosting(
            request=request,
            download_records=records,
            notes=[],
            documents_dir=Path("/tmp"),
            review_cfg={},
            classifier_keywords=["oil"],
        )
    finally:
        ss_mod.SerpApiSeeker = orig
    # One seeker instance per run; count how many targets it queried.
    return sum(len(s.queries) for s in _FakeSeeker.instances)


def test_code_hosting_retry_queries_every_failed_target():
    assert _run_code_hosting_retry() == 3


_CODE_HOST_SCORES = {
    "municode.com": 0.2,
    ".gov": 0.15,
    ".us": 0.15,
    "spam-codes.com": -0.3,
    "zeroed.com": 0.0,
}
_CODE_HOST_URLS = {
    "subdomain": "https://library.municode.com/tx/austin/codes",
    "exact": "https://municode.com/ca/fresno/codes",
    "lookalike": "https://notmunicode.com.evil.org/x/y/z",
    "suffix_gov": "https://county.example.gov/zoning/code/ch1",
    "suffix_us": "https://town.example.us/zoning/code/ch1",
    "negative": "https://spam-codes.com/a/b/c",
    "zero": "https://zeroed.com/a/b/c",
}


def _shell_record(label: str, url: str, tmp_path: Path) -> dict[str, object]:
    page = tmp_path / f"{label}.html"
    page.write_text("<html></html>")
    return {**_uncurated_record(label, url), "path": str(page)}


def test_code_host_domains_are_positive_host_entries_only():
    request = _base_request(link_prioritization_domain_scores={
        **_CODE_HOST_SCORES, " MuniCode.com. ": 0.1,
    })
    assert DiscoveryEngine._code_host_domains(request) == ("municode.com",)
    assert DiscoveryEngine._code_host_domains(_base_request()) == ()


def test_code_hosting_retry_targets_only_code_host_entries(monkeypatch, tmp_path):
    import psweep.discovery.connectors.serpapi_seeker as ss_mod

    queries: list[str] = []

    class _Seeker:
        queries_run = 0

        def __init__(self, **_kwargs):
            pass

        def discover(self, seeker_input):  # noqa: ANN001
            queries.extend(seeker_input.queries)
            return []

    monkeypatch.setattr(ss_mod, "SerpApiSeeker", _Seeker)
    DiscoveryEngine()._retry_failed_targets_on_code_hosting(
        request=_base_request(link_prioritization_domain_scores=_CODE_HOST_SCORES),
        download_records=[
            _uncurated_record(label, url) for label, url in _CODE_HOST_URLS.items()
        ],
        notes=[],
        documents_dir=tmp_path,
        review_cfg={},
        classifier_keywords=["zoning"],
    )
    scoped = sorted(q.split()[0] for q in queries)
    assert scoped == [
        "site:library.municode.com/tx/austin/codes",
        "site:municode.com/ca/fresno/codes",
    ]


def test_js_shell_retry_targets_only_code_host_entries(monkeypatch, tmp_path):
    import psweep.discovery.connectors.digger as digger_mod

    crawled: list[str] = []

    class _Digger:
        def discover(self, digger_input):  # noqa: ANN001
            crawled.extend(digger_input.sources)
            return CrawlResult(artifacts=[])

    monkeypatch.setattr(digger_mod, "HttpDiggerConnector", _Digger)
    DiscoveryEngine()._retry_js_shells_with_digger(
        request=_base_request(
            link_prioritization_domain_scores=_CODE_HOST_SCORES,
            browser_escalation=True,
            document_classifier={"nice_to_have_keywords": ["zoning"]},
        ),
        download_records=[
            _shell_record(label, url, tmp_path)
            for label, url in _CODE_HOST_URLS.items()
        ],
        notes=[],
        documents_dir=tmp_path,
        review_cfg={},
    )
    assert sorted(crawled) == sorted(
        [_CODE_HOST_URLS["subdomain"], _CODE_HOST_URLS["exact"]]
    )


def test_suffix_only_domain_scores_skip_paid_retries(monkeypatch, tmp_path):
    engine = DiscoveryEngine()
    calls: list[str] = []
    monkeypatch.setattr(
        engine, "_retry_failed_targets_on_code_hosting",
        lambda **kw: calls.append("serpapi") or (kw["download_records"], kw["notes"]),
    )
    monkeypatch.setattr(
        engine, "_retry_js_shells_with_digger",
        lambda **kw: (kw["download_records"], kw["notes"]),
    )
    request = _base_request(
        link_prioritization_domain_scores={".gov": 0.15, "bad.com": -0.2}
    )
    engine._run_post_review_retries(
        request=request, download_records=[], notes=[], documents_dir=tmp_path,
        review_cfg={}, classifier_keywords=["zoning"],
    )
    assert calls == []


def test_request_has_no_unwired_retry_target_cap():
    from dataclasses import fields

    assert "retry_max_retry_targets" not in {f.name for f in fields(DiscoveryRequest)}
    assert not hasattr(engine_mod, "_RetryBudget")


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
