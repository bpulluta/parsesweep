"""Completion means exhausted current work and verified published evidence."""

import io
import json
from unittest.mock import Mock

import pytest
import requests

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from discovery_helpers import FakeResponse


DOMAIN_DIR = "outputs/fixture/discovered"


@pytest.fixture(autouse=True)
def _outputs_in_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _request(tmp_path, **changes):
    values = {
        "domain": "fixture",
        "targets": [{"label": "target", "query": "documents"}],
        "dry_run": False,
        "partition_by": ["label"],
        "robots_policy_mode": "ignore",
        "tos_policy_mode": "ignore",
        "selection_primary_per_target": 2,
        "retry_max_attempts": 1,
    }
    values.update(changes)
    return DiscoveryRequest(**values)


def _fake_search(monkeypatch, response):
    monkeypatch.setattr(SerpApiSeeker, "_ensure_prepared", lambda _self: None)
    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(
            lambda _request: ({"provider": "serpapi", "enabled": True, "available": True}, [], [])
        ),
    )
    search = Mock(return_value=response)
    monkeypatch.setattr(
        SerpApiSeeker, "_build_search_callable", lambda _self: search
    )
    return search


def test_empty_exhausted_target_is_completed_without_searching_again(
    monkeypatch, tmp_path
):
    """Empty results finish selection rather than repeating paid searches."""
    search = _fake_search(monkeypatch, {"organic_results": []})
    request = _request(tmp_path)
    first = DiscoveryEngine().run(request)
    assert first.status == "empty"
    assert (tmp_path / DOMAIN_DIR / "checkpoint.json").exists()
    DiscoveryEngine().run(request)
    assert search.call_count == 1


def test_file_cap_does_not_checkpoint_truncated_target(monkeypatch, tmp_path):
    """A successful first download does not finish a two-document target."""
    _fake_search(
        monkeypatch,
        {
            "organic_results": [
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/b.pdf"},
            ]
        },
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    result = DiscoveryEngine().run(_request(tmp_path, max_files=1))
    assert result.curated_count == 1
    checkpoint = tmp_path / DOMAIN_DIR / "checkpoint.json"
    if checkpoint.exists():
        assert json.loads(checkpoint.read_text())["entries"] == {}


def test_deleted_curated_file_invalidates_completion(monkeypatch, tmp_path):
    """Resume verifies published bytes rather than trusting a target label."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    DiscoveryEngine().run(request)
    curated = tmp_path / DOMAIN_DIR / "curated/by_label/target/a.pdf"
    assert curated.exists()
    curated.unlink()
    DiscoveryEngine().run(request)
    assert search.call_count == 2


def test_changed_policy_invalidates_completion(monkeypatch, tmp_path):
    """Effective behavior changes cannot reuse a stale completed target."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    DiscoveryEngine().run(request)
    request.selection_exclude_url_patterns = ["archive"]
    DiscoveryEngine().run(request)
    assert search.call_count == 2


def test_cap_preserves_prior_complete_partition(monkeypatch, tmp_path):
    """Capped samples stay local rather than replacing prior evidence."""
    _fake_search(
        monkeypatch,
        {
            "organic_results": [
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/b.pdf"},
            ]
        },
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    previous = tmp_path / DOMAIN_DIR / "curated/by_label/target/prior.pdf"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous complete evidence")
    DiscoveryEngine().run(_request(tmp_path, max_files=1))
    assert previous.read_bytes() == b"previous complete evidence"
    assert not (previous.parent / "a.pdf").exists()


def test_shared_source_failure_preserves_prior_shared_curation(
    monkeypatch, tmp_path
):
    """Shared source errors protect the shared partition's existing curation."""
    prior = tmp_path / DOMAIN_DIR / "curated/_shared/old.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"old complete evidence")

    def get(url, **_kwargs):
        if url.endswith("bad.pdf"):
            raise requests.ConnectionError("connection failed")
        return FakeResponse(url=url)

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    request = _request(
        tmp_path,
        targets=None,
        partition_by=None,
        sources=[
            "https://example.org/good.pdf",
            "https://example.org/bad.pdf",
        ],
    )
    result = DiscoveryEngine().run(request)
    assert result.status == "partial_failure"
    assert prior.read_bytes() == b"old complete evidence"


def test_empty_search_cannot_complete_a_failed_target_url(monkeypatch, tmp_path):
    """Whole-target outcome, not search-only outcome, governs completion."""
    _fake_search(monkeypatch, {"organic_results": []})
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", Mock(side_effect=requests.ConnectionError("failed seed"))
    )
    request = _request(tmp_path, targets=[{
        "label": "target", "query": "documents", "urls": ["https://example.org/target.pdf"],
    }])
    result = DiscoveryEngine().run(request)
    assert result.status == "failed"
    checkpoint = tmp_path / DOMAIN_DIR / "checkpoint.json"
    if checkpoint.exists():
        assert not json.loads(checkpoint.read_text())["entries"]


def test_fixed_empty_reuse_remains_cached_on_third_run(monkeypatch, tmp_path):
    """Immutable outcome evidence survives canonical manifest updates."""
    search = _fake_search(monkeypatch, {"organic_results": []})
    request = _request(tmp_path)
    for _ in range(3):
        assert DiscoveryEngine().run(request).status == "empty"
    assert search.call_count == 1


def test_reused_none_retention_never_deletes_consolidated(
    monkeypatch, tmp_path
):
    """Retention applies to fresh run-local data, not reused corpus files."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(
        tmp_path, selection_primary_per_target=1, retention_documents="none"
    )
    first = DiscoveryEngine().run(request)
    prior = tmp_path / DOMAIN_DIR / "curated/by_label/target/a.pdf"
    assert prior.exists()
    result = DiscoveryEngine().run(request)
    assert result.status == "reused"
    assert prior.exists() and search.call_count == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert (
        manifest["status"] == "reused" and manifest["run_id"] == result.run_id
    )
    editable = json.loads(first.manifest_path.read_text())
    assert editable["status"] == "success" and editable["downloads"]


def test_failed_final_publication_preserves_fixed_local_outputs(
    monkeypatch, tmp_path
):
    """Local artifacts are not written before final coherent publication."""
    _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    first = DiscoveryEngine().run(request)
    before_manifest = first.manifest_path.read_bytes()
    before_index = first.download_index_path.read_bytes()
    before_curated = (first.curated_dir / "by_label/target/a.pdf").read_bytes()
    request.selection_exclude_url_patterns = ["archive"]
    monkeypatch.setattr(
        "psweep.discovery.engine.publish_manual_curation",
        Mock(side_effect=OSError("publication failed")),
    )
    with pytest.raises(OSError, match="publication failed"):
        DiscoveryEngine().run(request)
    assert first.manifest_path.read_bytes() == before_manifest
    assert first.download_index_path.read_bytes() == before_index
    assert (
        first.curated_dir / "by_label/target/a.pdf"
    ).read_bytes() == before_curated


def test_fixed_partial_resume_does_not_invalidate_previous_target(
    monkeypatch, tmp_path
):
    """Each completion references immutable manifest and index evidence."""
    targets = [{"label": label, "query": label} for label in ("alpha", "beta")]
    search = _fake_search(monkeypatch, {})
    search.side_effect = lambda params: {
        "organic_results": [{"link": f"https://example.org/{params['q']}.pdf"}]
    }
    fail_beta = [True]

    def get(url, **_kwargs):
        if url.endswith("beta.pdf") and fail_beta[0]:
            raise requests.ConnectionError("beta unavailable")
        return FakeResponse(url=url)

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    request = _request(tmp_path, targets=targets, retry_max_attempts=1)
    assert DiscoveryEngine().run(request).status == "partial_failure"
    fail_beta[0] = False
    assert DiscoveryEngine().run(request).status == "success"
    assert DiscoveryEngine().run(request).status == "reused"
    assert search.call_count == 3


def test_shared_sources_are_checkpointed_per_url(monkeypatch, tmp_path):
    """Completed sources are skipped; adding one fetches only the new URL."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    fetched = []

    def get(url, **_kwargs):
        fetched.append(url)
        if url.endswith("forbidden.pdf"):
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError("403 Client Error", response=response)
        return FakeResponse(url=url)

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    sources = ["https://other.org/one.pdf", "https://other.org/forbidden.pdf"]
    request = _request(tmp_path, selection_primary_per_target=1, sources=sources)
    DiscoveryEngine().run(request)
    fetched.clear()

    resumed = DiscoveryEngine().run(request)
    assert fetched == []
    assert search.call_count == 1
    assert resumed.status == "reused"
    assert not resumed.errors

    request.sources = [*sources, "https://other.org/two.pdf"]
    added = DiscoveryEngine().run(request)
    assert fetched == ["https://other.org/two.pdf"]
    assert search.call_count == 1
    manifest = json.loads(added.manifest_path.read_text())
    assert any("1 already-completed target(s)" in n for n in manifest["notes"])
    assert any("2 already-completed source(s)" in n for n in manifest["notes"])
    curated = tmp_path / DOMAIN_DIR / "curated/_shared"
    assert {p.name for p in curated.glob("*.pdf")} == {"one.pdf", "two.pdf"}


def test_terminal_failures_do_not_block_publication_or_completion(
    monkeypatch, tmp_path
):
    """A 403 shared source and an expired-cert target URL are final outcomes."""
    search = _fake_search(monkeypatch, {"organic_results": []})

    def get(url, **_kwargs):
        if url.endswith("forbidden.pdf"):
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError("403 Client Error", response=response)
        if url.endswith("expired.pdf"):
            raise requests.exceptions.SSLError("certificate has expired")
        return FakeResponse(url=url)

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    request = _request(
        tmp_path,
        targets=[{
            "label": "target", "query": "documents",
            "urls": ["https://example.org/ok.pdf", "https://example.org/expired.pdf"],
        }],
        sources=["https://example.org/shared.pdf", "https://example.org/forbidden.pdf"],
    )
    DiscoveryEngine().run(request)
    curated = tmp_path / DOMAIN_DIR / "curated"
    assert (curated / "_shared/shared.pdf").exists()
    assert (curated / "by_label/target/ok.pdf").exists()
    manifest = json.loads(
        (tmp_path / DOMAIN_DIR / "latest/manifest.json").read_text()
    )
    assert len(manifest["errors"]) == 2
    assert not any(error["retryable"] for error in manifest["errors"])
    assert {
        error["source"]["document_path"].rsplit("/", 1)[1]: error["target_label"]
        for error in manifest["errors"]
    } == {"forbidden.pdf": "_shared", "expired.pdf": "target"}
    assert manifest["lineage"]["blocked_partitions"] == []
    DiscoveryEngine().run(request)
    assert search.call_count == 1


def test_seeker_progress_reports_totals_across_targets(monkeypatch, tmp_path):
    """The stage summary counts every target, not only the last one."""
    _fake_search(monkeypatch, {"organic_results": [
        {"link": f"https://example.org/{name}.pdf"} for name in ("a", "b", "c")
    ]})
    messages = []
    request = _request(
        tmp_path,
        dry_run=True,
        selection_primary_per_target=1,
        targets=[{"label": "one", "query": "q1"}, {"label": "two", "query": "q2"}],
        progress_callback=messages.append,
    )
    DiscoveryEngine().run(request)
    (summary,) = [m for m in messages if "seeker stage completed" in m]
    assert "6 candidate(s) found across 2 target(s), 2 selected" in summary


def test_configured_url_does_not_drop_another_targets_search_hit(
    monkeypatch, tmp_path
):
    """Dedup against configured URLs is per target, not per URL."""
    _fake_search(monkeypatch, {"organic_results": [{"link": "https://example.org/x.pdf"}]})
    monkeypatch.setattr(
        "psweep.discovery.network.request_get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, targets=[
        {"label": "alpha", "query": "a", "urls": ["https://example.org/x.pdf"]},
        {"label": "beta", "query": "b"},
    ])
    DiscoveryEngine().run(request)
    curated = tmp_path / DOMAIN_DIR / "curated/by_label"
    assert (curated / "alpha/x.pdf").exists()
    assert (curated / "beta/x.pdf").exists()


def _fetch_by_url(monkeypatch):
    fetched = []

    def get(url, **_kwargs):
        fetched.append(url)
        return FakeResponse(url=url, chunks=[b"%PDF-1.4\n", url.encode()])

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    return fetched


def test_same_basename_from_another_owner_never_overwrites(monkeypatch, tmp_path):
    """Each consolidated path has one owner; a later owner gets a hashed name."""
    _fake_search(monkeypatch, {"organic_results": []})
    fetched = _fetch_by_url(monkeypatch)
    first, second = "https://a.gov/ordinance.pdf", "https://b.gov/ordinance.pdf"
    request = _request(tmp_path, sources=[first])
    DiscoveryEngine().run(request)
    request.sources = [first, second]
    DiscoveryEngine().run(request)

    curated = tmp_path / DOMAIN_DIR / "curated/_shared"
    plain = curated / "ordinance.pdf"
    (hashed,) = [p for p in curated.glob("ordinance-*.pdf")]
    assert plain.read_bytes().endswith(first.encode())
    assert hashed.read_bytes().endswith(second.encode())
    index = (tmp_path / DOMAIN_DIR / "download_index.csv").read_text()
    assert "a.gov/ordinance.pdf" in index and "b.gov/ordinance.pdf" in index

    fetched.clear()
    resumed = DiscoveryEngine().run(request)
    assert resumed.status == "reused" and fetched == []

    # --fresh replaces the partition, so stale owners no longer reserve names.
    request.fresh = True
    request.sources = [second]
    DiscoveryEngine().run(request)
    assert {p.name for p in curated.glob("*.pdf")} == {"ordinance.pdf"}
    assert plain.read_bytes().endswith(second.encode())


def test_document_names_are_stable_and_failures_reserve_nothing(monkeypatch, tmp_path):
    """An owner keeps its indexed name; a failed download never pushes others."""
    _fake_search(monkeypatch, {"organic_results": []})
    x, y = "https://x.gov/report.pdf", "https://y.gov/report.pdf"
    missing = set()

    def get(url, **_kwargs):
        if url in missing:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("404 Client Error", response=response)
        return FakeResponse(url=url, chunks=[b"%PDF-1.4\n", url.encode()])

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    curated = tmp_path / DOMAIN_DIR / "curated/by_label/target"
    index = tmp_path / DOMAIN_DIR / "download_index.csv"

    missing.add(x)
    request = _request(tmp_path, targets=[{"label": "target", "urls": [x, y]}])
    DiscoveryEngine().run(request)
    assert {p.name for p in curated.glob("*.pdf")} == {"report.pdf"}

    # y owns report.pdf; x later succeeds and gets the suffix, y keeps its name.
    missing.clear()
    request.targets = [{"label": "target", "urls": [y, x]}]
    DiscoveryEngine().run(request)
    assert (curated / "report.pdf").read_bytes().endswith(y.encode())
    (suffixed,) = curated.glob("report-*.pdf")
    assert suffixed.read_bytes().endswith(x.encode())

    # Re-running both maps each owner to the same path with one index row each.
    missing.add(x)
    request.targets = [{"label": "target", "urls": [x, y]}]
    DiscoveryEngine().run(request)
    assert (curated / "report.pdf").read_bytes().endswith(y.encode())
    assert sorted(p.name for p in curated.glob("*.pdf")) == sorted(["report.pdf", suffixed.name])
    rows = index.read_text().splitlines()[1:]
    assert sum("y.gov/report.pdf" in row for row in rows) == 1
    assert sum("x.gov/report.pdf" in row for row in rows) == 1


@pytest.mark.parametrize("scope", ["source", "target"])
def test_unfinished_crawl_keeps_origin_incomplete(monkeypatch, tmp_path, scope):
    """A retryable page failure or budget cutoff re-crawls the origin on resume."""
    from psweep.discovery import engine as engine_mod
    from psweep.discovery.connectors import CrawlResult

    search = _fake_search(monkeypatch, {"organic_results": []})
    _fetch_by_url(monkeypatch)
    hub = "https://county.gov/hub"
    crawls = []

    class Connector:
        def discover(self, digger_input):
            crawls.append(digger_input.sources)
            return CrawlResult([], {hub} if len(crawls) == 1 else set())

    monkeypatch.setattr(engine_mod, "resolve_digger_connector", lambda _p: Connector())
    target = {"label": "target", "query": "documents"}
    inputs = (
        {"sources": [hub], "targets": [target]}
        if scope == "source"
        else {"targets": [{**target, "urls": [hub]}]}
    )
    request = _request(tmp_path, max_depth=1, **inputs)
    first = DiscoveryEngine().run(request)
    assert crawls == [[hub]]
    manifest = json.loads(first.manifest_path.read_text())
    assert manifest["lineage"]["blocked_partitions"]
    DiscoveryEngine().run(request)
    assert crawls == [[hub], [hub]]
    DiscoveryEngine().run(request)
    assert len(crawls) == 2
    assert search.call_count == (1 if scope == "source" else 2)


def test_skipped_only_outcomes_complete_target_and_source(monkeypatch, tmp_path):
    """Final skips (unsupported type, filtered hit) never re-search or re-fetch."""
    search = _fake_search(
        monkeypatch, {"organic_results": [{"link": "https://example.org/archive.pdf"}]}
    )
    fetched = []

    def get(url, **_kwargs):
        fetched.append(url)
        return FakeResponse(url=url, content_type="image/png", chunks=[b"\x89PNG"])

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    request = _request(
        tmp_path,
        targets=[{
            "label": "target", "query": "documents",
            "urls": ["https://example.org/picture"],
        }],
        sources=["https://other.org/logo"],
        selection_exclude_url_patterns=["archive"],
    )
    first = DiscoveryEngine().run(request)
    statuses = {
        record["url"]: record["status"]
        for record in json.loads(first.manifest_path.read_text())["downloads"]
    }
    assert set(statuses.values()) == {"skipped_unsupported_type"}
    fetched.clear()
    DiscoveryEngine().run(request)
    assert fetched == []
    assert search.call_count == 1


def test_resume_reruns_only_targets_whose_inputs_changed(monkeypatch, tmp_path):
    """Passthrough edits re-run nothing; query or URL edits re-run one target."""
    search = _fake_search(monkeypatch, {"organic_results": []})
    fetched = _fetch_by_url(monkeypatch)
    alpha = {"label": "alpha", "county": "Pima", "urls": ["https://a.gov/a.pdf"]}
    beta = {"label": "beta", "county": "Pinal"}
    request = _request(
        tmp_path, queries=["{label} {county} permits"], targets=[alpha, beta]
    )
    DiscoveryEngine().run(request)
    assert search.call_count == 2
    fetched.clear()

    request.targets = [{**alpha, "cost": "300"}, {**beta, "cost": "150"}]
    assert DiscoveryEngine().run(request).status == "reused"
    assert search.call_count == 2 and fetched == []

    request.targets = [{**alpha, "cost": "300"}, {**beta, "county": "Maricopa"}]
    DiscoveryEngine().run(request)
    assert search.call_count == 3
    assert search.call_args.args[0]["q"] == "beta Maricopa permits"
    assert fetched == []

    request.targets[0] = {**alpha, "urls": ["https://a.gov/b.pdf"]}
    DiscoveryEngine().run(request)
    assert search.call_count == 4
    assert search.call_args.args[0]["q"] == "alpha Pima permits"
    assert fetched == ["https://a.gov/b.pdf"]


@pytest.mark.parametrize("robots_failure, retried", [
    (requests.ReadTimeout("read timed out"), True),
    (403, False),
])
def test_robots_unavailability_completes_only_when_deterministic(
    monkeypatch, tmp_path, robots_failure, retried
):
    """A timed-out robots.txt keeps the target open; a 403 is a final outcome."""
    _fake_search(monkeypatch, {"organic_results": []})
    fetched, failures = [], [robots_failure]

    def get(url, **_kwargs):
        fetched.append(url)
        if url.endswith("/robots.txt") and failures:
            failure = failures.pop()
            if isinstance(failure, Exception):
                raise failure
            response = requests.Response()
            response.status_code, response.raw = failure, io.BytesIO()
            return response
        if url.endswith("/robots.txt"):
            return FakeResponse(url=url, status_code=404)
        return FakeResponse(url=url)

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    request = _request(
        tmp_path, robots_policy_mode="enforce",
        targets=[{"label": "target", "urls": ["https://a.gov/x.pdf"]}],
    )
    first = DiscoveryEngine().run(request)
    (record,) = json.loads(first.manifest_path.read_text())["downloads"]
    assert record["status"] == ("failed" if retried else "skipped_robots_unavailable")
    checkpoint = tmp_path / DOMAIN_DIR / "checkpoint.json"
    entries = json.loads(checkpoint.read_text())["entries"] if checkpoint.exists() else {}
    assert bool(entries) is not retried

    fetched.clear()
    DiscoveryEngine().run(request)
    assert ("https://a.gov/x.pdf" in fetched) is retried


def test_crawl_page_with_transient_policy_failure_stays_unfinished(monkeypatch, tmp_path):
    """A page skipped because robots.txt timed out is re-crawled on resume."""
    from discovery_helpers import make_candidate

    from psweep.discovery.policies import PolicyCheckResult

    engine = DiscoveryEngine()
    monkeypatch.setattr(engine, "_evaluate_request_policy", lambda **_kw: PolicyCheckResult(
        allowed=False, blocking_code="robots_unavailable", retryable=True,
    ))
    page = make_candidate("https://county.gov/hub")
    *_, unfinished = engine._crawl_candidates(
        request=_request(tmp_path, max_depth=1), candidates=[page],
    )
    assert unfinished == [page]
